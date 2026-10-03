"""LinkedIn hunt (logged out): search cards → Jev triage → company ATS page → Jev fit → Excel.

Chrome reads public pages only. No login, no Easy Apply, no forms. An authwall stops
the hunt and whatever was scored so far is still written out.
"""

from __future__ import annotations

import html
import json
import random
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit

from jev_job_hunter import board
from jev_job_hunter.chrome import BrowserSession, ChromeError, ensure_chrome, load_extract_js
from jev_job_hunter.excel import write_xlsx
from jev_job_hunter.jev import ask, build_card_questions, build_fit_questions, card_state, fit_state
from jev_job_hunter.profile import load_profile, normalize_profile
from jev_job_hunter.questions import (
    FIT_SAVE_THRESHOLD, LINKEDIN_CARD_BATCH, LINKEDIN_MIN_ATS_TEXT, LINKEDIN_OPEN_THRESHOLD,
    MAX_SCROLLS,
)
from jev_job_hunter.store import append_log

SEARCH_URL = "https://www.linkedin.com/jobs/search/"
POSTED = {"day": "r86400", "week": "r604800", "month": "r2592000", "any": ""}
REMOTE = {"onsite": "1", "remote": "2", "hybrid": "3", "any": ""}
AUTHWALL_LIMIT = 2  # consecutive login walls before the hunt stops
_AUTHWALL = re.compile(r"/(authwall|login|checkpoint|signup|uas/)", re.I)

CARDS_JS = r"""() => {
  const tidy = (s) => String(s || "").replace(/\s+/g, " ").trim();
  const pick = (el, sel) => { const n = el.querySelector(sel); return n ? tidy(n.innerText) : ""; };
  const cards = [], seen = new Set();
  const add = (el, a) => {
    const href = String(a.href || "").split("?")[0];
    const m = href.match(/(\d{6,})\/?$/);
    const id = m ? m[1] : href;
    if (!href || seen.has(id)) return;
    seen.add(id);
    const t = el.querySelector("time");
    cards.push({
      id, href,
      title: pick(el, ".base-search-card__title, .job-card-list__title, h3") || tidy(a.innerText),
      company: pick(el, ".base-search-card__subtitle, .job-card-container__primary-description, h4"),
      location: pick(el, ".job-search-card__location, .job-card-container__metadata-item"),
      posted: t ? (t.getAttribute("datetime") || "") : "",
      posted_text: t ? tidy(t.innerText) : "",
    });
  };
  for (const el of document.querySelectorAll(".base-search-card, .job-search-card, .job-card-container")) {
    const a = el.querySelector("a[href*='/jobs/view/']") || el.closest("a[href*='/jobs/view/']");
    if (a) add(el, a);
  }
  if (!cards.length) for (const a of document.querySelectorAll("a[href*='/jobs/view/']")) add(a.closest("li") || a, a);
  const b = document.body;
  return { url: location.href, title: document.title, cards,
           scroll: { y: window.scrollY, h: b ? b.scrollHeight : 0, inner: window.innerHeight } };
}"""

VIEW_JS = r"""() => {
  const tidy = (s) => String(s || "").replace(/\s+/g, " ").trim();
  let apply = "";
  const code = document.querySelector("code#applyUrl");
  if (code) { const m = code.innerHTML.match(/https?:[^"'<>\s]+/); if (m) apply = m[0]; }
  if (!apply) {
    for (const a of document.querySelectorAll("a[href]")) {
      const h = a.href || "", host = (a.hostname || "").toLowerCase();
      if (/externalApply|[?&]url=http/.test(h) || (/apply/i.test(tidy(a.innerText)) && host && !host.endsWith("linkedin.com"))) { apply = h; break; }
    }
  }
  const d = document.querySelector(".show-more-less-html__markup, .description__text, .jobs-description__content, #job-details");
  const criteria = {};
  for (const li of document.querySelectorAll(".description__job-criteria-item")) {
    const k = li.querySelector("h3"), v = li.querySelector("span");
    if (k && v) criteria[tidy(k.innerText)] = tidy(v.innerText);
  }
  // Logged out, LinkedIn hides the company apply URL behind a sign-in modal; the offsite icon
  // still tells "apply on company site" apart from Easy Apply.
  const offsite = !!document.querySelector("[data-svg-class-name*='offsite'], [data-tracking-control-name*='apply-link-offsite'], .apply-button__offsite-apply-icon-svg");
  const anyApply = !!document.querySelector(".apply-button, [data-tracking-control-name*='public_jobs_apply']");
  return { url: location.href, title: document.title, apply, criteria,
           apply_type: offsite ? "Company site" : anyApply ? "Easy Apply" : "",
           text: ((d && d.innerText) || "").replace(/\n{3,}/g, "\n\n").slice(0, 6000) };
}"""

GUEST_POSTING = "https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/{id}"


class AuthWall(Exception):
    pass


class Stopped(Exception):
    pass


def search_url(query: str, location: str = "", posted: str = "week", remote: str = "any") -> str:
    params = {"keywords": query.strip()}
    if location:
        params["location"] = location.strip()
    if POSTED.get(posted or "any"):
        params["f_TPR"] = POSTED[posted]
    if REMOTE.get(remote or "any"):
        params["f_WT"] = REMOTE[remote]
    return SEARCH_URL + "?" + urlencode(params)


def is_authwall(url: str) -> bool:
    p = urlsplit(url or "")
    return (p.hostname or "").lower().endswith("linkedin.com") and bool(_AUTHWALL.search(p.path or ""))


def apply_target(raw: str) -> str:
    """Company apply URL from LinkedIn's applyUrl / externalApply redirect. '' when none."""
    s = html.unescape((raw or "").strip()).strip("\"'")
    p = urlsplit(s)
    if p.scheme not in ("http", "https"):
        return ""
    if (p.hostname or "").lower().endswith("linkedin.com"):
        inner = parse_qs(p.query).get("url")
        return apply_target(inner[0]) if inner else ""
    return s


def _pause(delay: float) -> None:
    if delay > 0:
        time.sleep(delay * random.uniform(0.6, 1.4))


def _eval(session, js: str) -> dict:
    got = session.eval_value("(" + js + ")()")
    return got if isinstance(got, dict) else {}


def _noop(kind: str, data: dict) -> None:
    pass


def _check(stop) -> None:
    if stop is not None and stop.is_set():
        raise Stopped()


def collect_cards(session, query: str, location: str, posted: str, remote: str,
                  limit: int, delay: float, seen: set[str] | None = None,
                  emit=_noop, stop=None) -> list[dict]:
    """Cards for one search, deduped against `seen`. Each new card is emitted as soon as it is read."""
    seen = set() if seen is None else seen
    url = search_url(query, location, posted, remote)
    board.print_li_search(query, url)
    emit("search", {"query": query, "url": url})
    session.navigate(url)
    _pause(delay)
    new: list[dict] = []

    def take(data: dict) -> int:
        cards = [c for c in (data.get("cards") or []) if c.get("title")][:limit]
        for c in cards:
            if c["id"] not in seen:
                seen.add(c["id"])
                card = {**c, "query": query}
                new.append(card)
                emit("card", card)
        return len(cards)

    data = _eval(session, CARDS_JS)
    if is_authwall(data.get("url") or ""):
        raise AuthWall(data.get("url"))
    total = take(data)
    scrolls = 0
    for _ in range(MAX_SCROLLS):
        _check(stop)
        if total >= limit:
            break
        session.scroll_to_bottom()
        _pause(delay / 2)
        session.wait_ready(cap=1.5)
        data = _eval(session, CARDS_JS)
        if len(data.get("cards") or []) <= total and session.click_text("See more jobs"):
            _pause(delay / 2)
            session.wait_ready(cap=1.5)
            data = _eval(session, CARDS_JS)
        if is_authwall(data.get("url") or ""):
            break
        got = take(data)
        if got <= total:
            break
        total, scrolls = got, scrolls + 1
    print(f"  collected {total} cards, {len(new)} new ({scrolls} scrolls)", flush=True)
    return new


def triage(profile: dict, query: str, cards: list[dict], mock: bool) -> int:
    """Jev scores every card title for 'worth opening'. Mutates cards[*]['open']."""
    jev_ms = 0
    for s in range(0, len(cards), LINKEDIN_CARD_BATCH):
        chunk = cards[s:s + LINKEDIN_CARD_BATCH]
        res = ask(card_state(profile, query, chunk), build_card_questions(chunk), mock=mock)
        jev_ms += res.latency_ms
        for i, c in enumerate(chunk):
            a = res.answers.get(f"open_{i}")
            c["open"] = round(float(a.noul), 3) if a is not None else 0.0
    return jev_ms


def _view(session, url: str, delay: float) -> dict:
    session.navigate(url)
    _pause(delay)
    view = _eval(session, VIEW_JS)
    if is_authwall(view.get("url") or ""):
        raise AuthWall(view.get("url"))
    return view


def read_posting(session, js: str, card: dict, delay: float) -> dict:
    """LinkedIn posting (guest endpoint first: no authwall on logged-out reads) → company ATS
    text when an apply URL is exposed, else LinkedIn's full description + criteria."""
    view = {}
    if str(card.get("id") or "").isdigit():
        for attempt in range(2):  # right after searches the guest endpoint can wall or answer empty once
            try:
                view = _view(session, GUEST_POSTING.format(id=card["id"]), delay * (1 + 2 * attempt))
            except AuthWall:
                if attempt:
                    raise
                _pause(delay * 4)
                continue
            if len(str(view.get("text") or "")) >= LINKEDIN_MIN_ATS_TEXT:
                break
    if len(str(view.get("text") or "")) < LINKEDIN_MIN_ATS_TEXT:
        view = _view(session, card["href"], delay)
    apply = apply_target(view.get("apply") or "")
    criteria = view.get("criteria") or {}
    extra = {"apply_type": view.get("apply_type") or "", "level": criteria.get("Seniority level", ""),
             "employment": criteria.get("Employment type", "")}
    head = "\n".join(f"{k}: {v}" for k, v in criteria.items())
    li_text = (head + "\n\n" if head else "") + str(view.get("text") or "")
    if apply:
        try:
            session.navigate(apply)
            _pause(delay / 2)
            page = session.extract(js, need_text=True) or {}
            text = str(page.get("text") or "")
            if len(text) >= LINKEDIN_MIN_ATS_TEXT:
                return {"source": "ATS", "url": str(page.get("url") or apply), "apply_url": apply,
                        "text": text, **extra}
        except ChromeError:
            pass
    return {"source": "LinkedIn", "url": card["href"], "apply_url": apply, "text": li_text, **extra}


def score_fit(profile: dict, card: dict, post: dict, mock: bool) -> tuple[dict, int]:
    res = ask(fit_state(profile, card, post["url"], post["text"]), build_fit_questions(), mock=mock)
    return {k: round(float(a.noul), 3) for k, a in res.answers.items() if a.type == "noul"}, res.latency_ms


def hunt_meta(prof: dict, queries: list[str], location: str, posted: str, remote: str, mock: bool) -> dict:
    meta = {"generated_at": datetime.now().strftime("%Y-%m-%d %H:%M"), "mock": "yes" if mock else "no",
            "profile": prof.get("path") or "", "queries": queries, "location": location,
            "posted": posted, "remote": remote}
    for k, v in prof["candidate"].items():
        meta[f"candidate.{k}"] = v if isinstance(v, (str, int, float, list)) else json.dumps(v, ensure_ascii=False)
    return meta


def save_results(out_dir: Path, matches: list[dict], listings: list[dict], meta: dict,
                 stamp: str | None = None) -> Path:
    matches = sorted(matches, key=lambda m: m.get("fit") or 0, reverse=True)
    listings = sorted(listings, key=lambda c: c.get("open") or 0, reverse=True)
    stamp = stamp or datetime.now().strftime("%Y%m%d-%H%M%S")
    path = write_xlsx(out_dir / f"linkedin-{stamp}.xlsx", matches, listings, meta)
    path.with_suffix(".json").write_text(json.dumps(
        {"meta": meta, "matches": matches, "listings": listings}, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8")
    return path


def run_linkedin(root: Path, *, profile: dict | None = None, profile_path: str | None = None,
                 queries: list[str] | None = None, location: str | None = None,
                 posted: str | None = None, remote: str | None = None,
                 max_cards: int = 60, max_open: int = 10, delay: float = 2.0, mock: bool = False,
                 tab: str = "new", out_dir: Path | None = None, session=None,
                 on_event=None, stop=None) -> Path | None:
    """`profile` (raw {candidate, search}) wins over `profile_path`. `on_event(kind, data)` streams
    status / search / card / triage / fit / done for the web UI; `stop` is a threading.Event."""
    try:
        sys.stdout.reconfigure(line_buffering=True, errors="replace")
    except Exception:
        pass
    emit = on_event or _noop
    prof = normalize_profile(profile, "(web)") if profile is not None else load_profile(root, profile_path)
    cand, search = prof["candidate"], prof["search"]
    queries = [q for q in (queries or search["queries"]) if q.strip()]
    location = search["location"] if location is None else location
    posted = posted or search["posted"]
    remote = remote or search["remote"]
    hunt_query = " | ".join(queries)
    board.print_li_start(cand, queries, location, posted, remote)

    own = session is None
    t0 = time.perf_counter()
    listings: list[dict] = []
    matches: list[dict] = []
    stopped = ""
    try:
        if own:
            emit("status", {"msg": "Connecting to Chrome"})
            how = ensure_chrome(root)
            print(f"  Chrome  {how}", flush=True)
            session = BrowserSession.open()
            session.reuse_tab(SEARCH_URL) if tab == "reuse" else session.new_tab()
        js = load_extract_js(root)
        seen: set[str] = set()
        for q in queries:
            _check(stop)
            emit("status", {"msg": f"Searching LinkedIn: {q}"})
            listings += collect_cards(session, q, location, posted, remote, max_cards, delay,
                                      seen=seen, emit=emit, stop=stop)
        if not listings:
            stopped = "no LinkedIn cards found"
        else:
            emit("status", {"msg": f"Jev is triaging {len(listings)} titles"})
            jev_ms = triage(cand, hunt_query, listings, mock)
            picked = sorted((c for c in listings if c["open"] >= LINKEDIN_OPEN_THRESHOLD),
                            key=lambda c: c["open"], reverse=True)[:max_open]
            for c in picked:
                c["opened"] = True
            emit("triage", {"scores": {c["id"]: c["open"] for c in listings},
                            "picked": [c["id"] for c in picked], "jev_ms": jev_ms})
            board.print_li_triage(listings, picked, jev_ms)
            append_log(root, {"cmd": "linkedin", "stage": "triage", "links": len(listings),
                              "jev_ms": jev_ms, "action": f"OPEN {len(picked)}"})
            walls = 0
            for n, c in enumerate(picked, 1):
                _check(stop)
                emit("status", {"msg": f"Reading {n}/{len(picked)}: {c['title']} · {c.get('company', '')}"})
                p0 = time.perf_counter()
                try:
                    post = read_posting(session, js, c, delay)
                except AuthWall as e:
                    walls += 1
                    if walls >= AUTHWALL_LIMIT:
                        raise
                    print(f"  login wall reading {c['id']} ({str(e)[:80]}) — skipping, cooling down", flush=True)
                    emit("status", {"msg": "LinkedIn showed a login wall; skipping one posting and slowing down"})
                    _pause(delay * 8)
                    continue
                walls = 0
                scores, ms = score_fit(cand, c, post, mock)
                fit = scores.get("overall_fit", 0.0)
                m = {**c, **scores, "fit": fit, "source": post["source"], "apply_url": post["apply_url"],
                     "read_url": post["url"], "apply_type": post.get("apply_type", ""),
                     "level": post.get("level", ""), "employment": post.get("employment", "")}
                matches.append(m)
                emit("fit", {"id": c["id"], **{k: m[k] for k in (*scores, "fit", "source", "apply_url", "read_url",
                                                                 "apply_type", "level", "employment")}})
                board.print_li_fit(n, len(picked), c, post, scores, fit >= FIT_SAVE_THRESHOLD, ms)
                append_log(root, {"cmd": "linkedin", "stage": "fit", "url": post["url"], "jev_ms": ms,
                                  "total_ms": int((time.perf_counter() - p0) * 1000),
                                  "action": "SAVE" if fit >= FIT_SAVE_THRESHOLD else "SKIP"})
    except AuthWall as e:
        stopped = f"LinkedIn login wall ({e}) — stopped, results so far are saved"
    except ChromeError as e:
        stopped = f"Chrome error ({e}) — results so far are saved"
    except (KeyboardInterrupt, Stopped):
        stopped = "stopped — results so far are saved"
    finally:
        if own and session is not None:
            session.close()
    if stopped:
        print(f"\n  {stopped}", flush=True)
    if not listings:
        emit("done", {"xlsx": "", "cards": 0, "matches": 0, "stopped": stopped})
        print("ACTION END", flush=True)
        return None

    meta = hunt_meta(prof, queries, location, posted, remote, mock)
    path = save_results(out_dir or root / "results", matches, listings, meta)
    matches.sort(key=lambda m: m.get("fit") or 0, reverse=True)
    board.print_li_report(matches, len(listings), path, time.perf_counter() - t0)
    emit("done", {"xlsx": str(path), "cards": len(listings), "matches": len(matches), "stopped": stopped})
    print("ACTION END", flush=True)
    return path


