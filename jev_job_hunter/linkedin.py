"""LinkedIn hunt (logged out): search cards → Jev triage → company ATS page → Jev fit → Excel.

Plain HTTP against LinkedIn's public guest endpoints, 10 cards per request, with irregular
pauses between requests and a long back-off on 429. No login, no Easy Apply, no forms.
A login wall or a persistent 429 stops the hunt and whatever was scored so far is still written out.
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

import httpx

from jev_job_hunter import board
from jev_job_hunter.excel import write_xlsx
from jev_job_hunter.jev import ask, build_card_questions, build_fit_questions, card_state, fit_state
from jev_job_hunter.profile import load_profile, normalize_profile
from jev_job_hunter.questions import (
    FIT_SAVE_THRESHOLD, LINKEDIN_CARD_BATCH, LINKEDIN_DELAY, LINKEDIN_MAX_CARDS, LINKEDIN_MAX_OPEN,
    LINKEDIN_MIN_ATS_TEXT, LINKEDIN_OPEN_THRESHOLD,
)
from jev_job_hunter.store import append_log

SEARCH_URL = "https://www.linkedin.com/jobs/search/"
GUEST_SEARCH = "https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search"
GUEST_POSTING = "https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/{id}"
POSTED = {"day": "r86400", "week": "r604800", "month": "r2592000", "any": ""}
REMOTE = {"onsite": "1", "remote": "2", "hybrid": "3", "any": ""}
PAGE_SIZE = 10           # cards per guest search request
AUTHWALL_LIMIT = 2       # consecutive login walls before the hunt stops
RATE_LIMIT_RETRIES = 2   # 429s per request before giving up
RATE_LIMIT_BACKOFF = 60.0  # seconds; doubles per retry, jittered
_AUTHWALL = re.compile(r"/(authwall|login|checkpoint|signup|uas/)", re.I)
# Without Accept, LinkedIn answers 429 even to a first request (docs/evidence/2026-10-12-*).
HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36"),
    "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9,ko;q=0.8",
}

_CARD = re.compile(r'data-entity-urn="urn:li:jobPosting:(\d+)"')
_TAG = re.compile(r"<[^>]+>")


class AuthWall(Exception):
    pass


class RateLimited(Exception):
    pass


class Stopped(Exception):
    pass


def search_params(query: str, location: str = "", posted: str = "week", remote: str = "any") -> dict:
    params = {"keywords": query.strip()}
    if location:
        params["location"] = location.strip()
    if POSTED.get(posted or "any"):
        params["f_TPR"] = POSTED[posted]
    if REMOTE.get(remote or "any"):
        params["f_WT"] = REMOTE[remote]
    return params


def search_url(query: str, location: str = "", posted: str = "week", remote: str = "any") -> str:
    """The human-facing search page for the same query (shown in logs and the web UI)."""
    return SEARCH_URL + "?" + urlencode(search_params(query, location, posted, remote))


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


def html_text(fragment: str, limit: int = 6000) -> str:
    """Readable text from an HTML fragment: block tags become line breaks, scripts and buttons dropped."""
    s = re.sub(r"<(script|style|button|noscript)\b.*?</\1>", " ", fragment or "", flags=re.S | re.I)
    s = re.sub(r"<li\b[^>]*>", "\n- ", s, flags=re.I)
    s = re.sub(r"<br\s*/?>|</(p|div|li|ul|ol|h\d|section|tr)>", "\n", s, flags=re.I)
    s = html.unescape(_TAG.sub(" ", s))
    lines = (" ".join(line.split()) for line in s.splitlines())
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()[:limit]


def _between(chunk: str, cls: str, end: str) -> str:
    m = re.search(rf'class="{cls}[^"]*"[^>]*>(.*?){end}', chunk, re.S)
    return " ".join(html.unescape(_TAG.sub(" ", m.group(1))).split()) if m else ""


def parse_cards(page: str) -> list[dict]:
    """Cards from one guest search response, in page order."""
    cards = []
    starts = [m.start() for m in _CARD.finditer(page or "")] + [len(page or "")]
    for a, b in zip(starts, starts[1:]):
        chunk = page[a:b]
        href = re.search(r'href="([^"]*/jobs/view/[^"]*)"', chunk)
        posted = re.search(r'<time[^>]*datetime="([^"]*)"[^>]*>(.*?)</time>', chunk, re.S)
        cards.append({
            "id": _CARD.match(chunk).group(1),
            "href": html.unescape(href.group(1)).split("?")[0] if href else "",
            "title": _between(chunk, "base-search-card__title", "</h3>"),
            "company": _between(chunk, "base-search-card__subtitle", "</h4>"),
            "location": _between(chunk, "job-search-card__location", "</span>"),
            "posted": posted.group(1) if posted else "",
            "posted_text": " ".join(posted.group(2).split()) if posted else "",
        })
    return [c for c in cards if c["title"]]


def parse_posting(page: str) -> dict:
    """Description, criteria and apply info from a guest jobPosting (or public /jobs/view/) page."""
    page = page or ""
    apply = ""
    code = re.search(r'<code[^>]*id="applyUrl"[^>]*>(.*?)</code>', page, re.S)
    if code:
        m = re.search(r"https?:[^\"'<>\s]+", code.group(1))
        apply = m.group(0) if m else ""
    if not apply:
        m = re.search(r'href="([^"]*(?:externalApply|[?&](?:amp;)?url=http)[^"]*)"', page)
        apply = html.unescape(m.group(1)) if m else ""
    criteria = {" ".join(html.unescape(k).split()): " ".join(html.unescape(v).split())
                for k, v in re.findall(r'job-criteria-subheader[^>]*>(.*?)</h3>\s*<span[^>]*>(.*?)</span>', page, re.S)}
    text = ""
    i = page.find('class="show-more-less-html__markup')
    if i < 0:
        i = page.find('class="description__text')
    if i >= 0:
        start = page.find(">", i) + 1
        end = page.find("</section>", start)
        text = html_text(page[start:end if end > 0 else None])
    offsite = "offsite-apply-icon" in page or "apply-link-offsite" in page
    any_apply = "apply-button" in page or "public_jobs_apply" in page
    return {"apply": apply, "criteria": criteria, "text": text,
            "apply_type": "Company site" if offsite else "Easy Apply" if any_apply else ""}


def _noop(kind: str, data: dict) -> None:
    pass


def _check(stop) -> None:
    if stop is not None and stop.is_set():
        raise Stopped()


class GuestClient:
    """HTTP client that paces itself like a person reading: an irregular gap before every LinkedIn
    request (mean `delay` s, sometimes a longer pause), cookies kept for the whole hunt, and
    60 s → 120 s back-off on 429 before giving up."""

    def __init__(self, delay: float, stop=None, http: httpx.Client | None = None):
        self.delay, self.stop = delay, stop
        self.http = http or httpx.Client(headers=HEADERS, follow_redirects=True, timeout=20.0)
        self._last: float | None = None

    def close(self) -> None:
        self.http.close()

    def sleep(self, seconds: float) -> None:
        if seconds <= 0:
            return
        if self.stop is None:
            time.sleep(seconds)
        elif self.stop.wait(seconds):
            raise Stopped()

    def _pace(self) -> None:
        if self._last is None:
            return
        gap = self.delay * random.uniform(0.6, 1.6)
        if random.random() < 0.15:  # now and then linger, like reading a posting
            gap += self.delay * random.uniform(1.0, 3.0)
        self.sleep(gap - (time.monotonic() - self._last))

    def get(self, url: str) -> str:
        """LinkedIn page body. Raises AuthWall / RateLimited; '' for other HTTP errors."""
        for attempt in range(RATE_LIMIT_RETRIES + 1):
            self._pace()
            try:
                r = self.http.get(url)
            except httpx.HTTPError as e:
                self._last = time.monotonic()
                print(f"  HTTP error {type(e).__name__} for {url[:100]}", flush=True)
                return ""
            self._last = time.monotonic()
            if r.status_code in (429, 999):
                if attempt == RATE_LIMIT_RETRIES:
                    raise RateLimited(f"HTTP {r.status_code} after {attempt + 1} tries")
                wait = RATE_LIMIT_BACKOFF * (2 ** attempt) * random.uniform(0.8, 1.3)
                print(f"  LinkedIn {r.status_code} (rate limited) — waiting {wait:.0f}s", flush=True)
                self.sleep(wait)
                continue
            if is_authwall(str(r.url)):
                raise AuthWall(str(r.url))
            return r.text if r.status_code == 200 else ""
        return ""

    def get_external(self, url: str) -> tuple[str, str]:
        """(final url, text) of a company ATS page; ('', '') on any failure. Not LinkedIn, so unpaced."""
        try:
            r = self.http.get(url)
        except httpx.HTTPError:
            return "", ""
        return (str(r.url), html_text(r.text)) if r.status_code == 200 else ("", "")


def collect_cards(client: GuestClient, query: str, location: str, posted: str, remote: str,
                  limit: int, seen: set[str] | None = None, emit=_noop, stop=None) -> list[dict]:
    """Cards for one search, 10 per request, deduped against `seen`. Each new card is emitted as read."""
    seen = set() if seen is None else seen
    params = search_params(query, location, posted, remote)
    url = search_url(query, location, posted, remote)
    board.print_li_search(query, url)
    emit("search", {"query": query, "url": url})
    new: list[dict] = []
    total = pages = 0
    while total < limit:
        _check(stop)
        try:
            cards = parse_cards(client.get(GUEST_SEARCH + "?" + urlencode({**params, "start": total})))
        except (AuthWall, RateLimited):
            if not pages:
                raise
            break  # keep what earlier pages gave
        pages += 1
        for c in cards[:limit - total]:
            if c["id"] not in seen:
                seen.add(c["id"])
                card = {**c, "query": query}
                new.append(card)
                emit("card", card)
        total += len(cards)
        if len(cards) < PAGE_SIZE:
            break
    print(f"  collected {min(total, limit)} cards, {len(new)} new ({pages} requests)", flush=True)
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


def read_posting(client: GuestClient, card: dict) -> dict:
    """LinkedIn posting (guest endpoint first: no authwall on logged-out reads) → company ATS
    text when an apply URL is exposed, else LinkedIn's full description + criteria."""
    view = {}
    if str(card.get("id") or "").isdigit():
        for attempt in range(2):  # right after searches the guest endpoint can wall or answer empty once
            try:
                view = parse_posting(client.get(GUEST_POSTING.format(id=card["id"])))
            except AuthWall:
                if attempt:
                    raise
                client.sleep(client.delay * 4)
                continue
            if len(view.get("text") or "") >= LINKEDIN_MIN_ATS_TEXT:
                break
    if len(view.get("text") or "") < LINKEDIN_MIN_ATS_TEXT and card.get("href"):
        view = parse_posting(client.get(card["href"]))
    apply = apply_target(view.get("apply") or "")
    criteria = view.get("criteria") or {}
    extra = {"apply_type": view.get("apply_type") or "", "level": criteria.get("Seniority level", ""),
             "employment": criteria.get("Employment type", "")}
    head = "\n".join(f"{k}: {v}" for k, v in criteria.items())
    li_text = (head + "\n\n" if head else "") + str(view.get("text") or "")
    if apply:
        final, text = client.get_external(apply)
        if len(text) >= LINKEDIN_MIN_ATS_TEXT:
            return {"source": "ATS", "url": final or apply, "apply_url": apply, "text": text, **extra}
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
                 max_cards: int = LINKEDIN_MAX_CARDS, max_open: int = LINKEDIN_MAX_OPEN,
                 delay: float = LINKEDIN_DELAY, mock: bool = False, out_dir: Path | None = None,
                 client: GuestClient | None = None, on_event=None, stop=None) -> Path | None:
    """`profile` (raw {candidate, search}) wins over `profile_path`. `on_event(kind, data)` streams
    status / search / card / triage / fit / done for the web UI; `stop` is a threading.Event.
    `delay` is the mean gap in seconds between LinkedIn requests."""
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

    own = client is None
    client = client or GuestClient(delay, stop=stop)
    t0 = time.perf_counter()
    listings: list[dict] = []
    matches: list[dict] = []
    stopped = ""
    try:
        seen: set[str] = set()
        for q in queries:
            _check(stop)
            emit("status", {"msg": f"Searching LinkedIn: {q}"})
            try:
                listings += collect_cards(client, q, location, posted, remote, max_cards,
                                          seen=seen, emit=emit, stop=stop)
            except (AuthWall, RateLimited) as e:
                if not listings:
                    raise
                print(f"  LinkedIn blocked the search for {q!r} ({e}) — going on with {len(listings)} cards",
                      flush=True)
                break
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
                    post = read_posting(client, c)
                except AuthWall as e:
                    walls += 1
                    if walls >= AUTHWALL_LIMIT:
                        raise
                    print(f"  login wall reading {c['id']} ({str(e)[:80]}) — skipping, cooling down", flush=True)
                    emit("status", {"msg": "LinkedIn showed a login wall; skipping one posting and slowing down"})
                    client.sleep(client.delay * 8)
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
    except RateLimited as e:
        stopped = f"LinkedIn rate limit ({e}) — stopped, results so far are saved; retry later"
    except (KeyboardInterrupt, Stopped):
        stopped = "stopped — results so far are saved"
    finally:
        if own:
            client.close()
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


