"""Compare LinkedIn search results logged out (guest API) vs logged in (pasted from your browser).

  1. python3 scripts/li_compare.py guest --query "AI Engineer" --location "South Korea" --posted week
     → fetches the logged-out results, writes results/li-guest.json, prints the logged-in URL + console snippet.
  2. Open that URL while logged in, run the snippet in DevTools on each results page, paste into a file.
  3. python3 scripts/li_compare.py compare results/li-login.txt
     → overlap, rank-by-rank agreement, and what only one side saw.

Stdlib only. The guest side is a handful of requests with human-ish pauses; it never logs in.
"""

from __future__ import annotations

import argparse
import html
import json
import random
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from urllib.parse import urlencode

GUEST_SEARCH = "https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search"
LOGIN_SEARCH = "https://www.linkedin.com/jobs/search/"
POSTED = {"day": "r86400", "week": "r604800", "month": "r2592000", "any": ""}
REMOTE = {"onsite": "1", "remote": "2", "hybrid": "3", "any": ""}
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36")
DEFAULT_GUEST = Path("results/li-guest.json")
RETRIES_429 = 2  # back off 60s, then 120s, before giving up

# Run in DevTools on a logged-in /jobs/search/ page. Reads the DOM only (no requests, no clicks).
# Finds jobs by card attribute, /jobs/view/ link, jobPosting URN, or componentkey. Accumulates across result pages in localStorage and copies id<TAB>promoted<TAB>title<TAB>company.
SNIPPET = r"""(() => {
  const K = "jjhIds", tidy = (s) => String(s || "").replace(/\s+/g, " ").trim();
  const rows = JSON.parse(localStorage.getItem(K) || "[]"), seen = new Set(rows.map((r) => r[0]));
  const RX = /(?:\/jobs\/view\/(?:[^\/?"\s]*-)?|currentJobId=|jobPosting[:(,]|job-id=")(\d{8,})/;
  const found = new Map(), hits = { attr: 0, link: 0, urn: 0, key: 0 };
  const put = (id, el, how) => { if (!found.has(id)) { found.set(id, el); hits[how]++; } };
  // LinkedIn's markup changes often: try card attributes, then job links, then any attribute holding a jobPosting URN.
  document.querySelectorAll("[data-occludable-job-id], [data-job-id]").forEach((el) => {
    const id = el.getAttribute("data-occludable-job-id") || el.getAttribute("data-job-id");
    if (/^\d+$/.test(id || "")) put(id, el, "attr");
  });
  document.querySelectorAll("a[href]").forEach((a) => {
    const m = a.href.match(RX);
    if (m) put(m[1], a.closest("li") || a.parentElement || a, "link");
  });
  document.querySelectorAll("*").forEach((el) => {
    for (const at of el.attributes) {
      const m = /jobPosting[:(,](\d{8,})/.exec(at.value);
      if (m) { put(m[1], el.closest("li") || el, "urn"); break; }
    }
  });
  // /jobs/search-results/ (2026 UI): no links per card; componentkey="job-card-component-ref-<jobId>".
  const keyed = new Map(), samples = [];
  document.querySelectorAll("[componentkey]").forEach((el) => {
    const v = el.getAttribute("componentkey"), m = /job-card-component-ref-(\d+)/.exec(v);
    if (!m) return;
    if (samples.length < 4) samples.push(v);
    const t = (el.innerText || "").length, prev = keyed.get(m[1]);
    if (t > 0 && t < 800 && (!prev || t > prev.t)) keyed.set(m[1], { el, t });
  });
  for (const [id, { el }] of keyed) put(id, el, "key");
  let added = 0;
  for (const [id, el] of found) {
    if (seen.has(id)) continue;
    const a = el.matches("a") ? el : el.querySelector("a[href*='/jobs/view/'], a[href*='currentJobId']");
    const lines = (el.innerText || "").split("\n").map(tidy).filter((l) => l && !/promoted jobs are ranked/i.test(l));
    const title = tidy((a && (a.getAttribute("aria-label") || a.innerText)) || lines[0]).replace(/ with verification$/, "");
    const company = lines.find((l) => l !== title && !title.startsWith(l)) || "";
    const promoted = /\bpromoted\b|프로모션|광고/i.test(el.innerText || "") ? "promoted" : "";
    rows.push([id, promoted, title, company]); seen.add(id); added++;
  }
  localStorage.setItem(K, JSON.stringify(rows));
  copy(rows.map((r) => r.join("\t")).join("\n"));
  console.log(`+${added} this page, ${rows.length} total — copied.`, { page: location.pathname, ...hits, samples },
              `Reset: localStorage.removeItem("${K}")`);
})();"""

_CARD_ID = re.compile(r'data-entity-urn="urn:li:jobPosting:(\d+)"')
_TITLE = re.compile(r'class="base-search-card__title"[^>]*>(.*?)</h3>', re.S)
_SUBTITLE = re.compile(r'class="base-search-card__subtitle"[^>]*>(.*?)</h4>', re.S)
_LOCATION = re.compile(r'class="job-search-card__location"[^>]*>(.*?)</span>', re.S)
_TAG = re.compile(r"<[^>]+>")
_PASTED_ID = re.compile(r"(?:jobs/view/(?:[^/?\s]*-)?|currentJobId=|jobPosting:|job-id=\"?)(\d{6,})|^\s*(\d{6,})\b")


def _text(m: re.Match | None) -> str:
    return " ".join(html.unescape(_TAG.sub(" ", m.group(1))).split()) if m else ""


def params(query: str, location: str, posted: str, remote: str) -> dict:
    p = {"keywords": query.strip()}
    if location:
        p["location"] = location.strip()
    if POSTED.get(posted):
        p["f_TPR"] = POSTED[posted]
    if REMOTE.get(remote):
        p["f_WT"] = REMOTE[remote]
    return p


def parse_guest_cards(page: str) -> list[dict]:
    """Cards from one guest search response, in page order."""
    out = []
    starts = [m.start() for m in _CARD_ID.finditer(page)] + [len(page)]
    for a, b in zip(starts, starts[1:]):
        chunk = page[a:b]
        out.append({"id": _CARD_ID.match(chunk).group(1), "title": _text(_TITLE.search(chunk)),
                    "company": _text(_SUBTITLE.search(chunk)), "location": _text(_LOCATION.search(chunk))})
    return out


def parse_login(text: str) -> list[dict]:
    """Job IDs (plus optional promoted/title/company columns) from whatever the user pasted, in order."""
    out, seen = [], set()
    for line in text.splitlines():
        cols = line.split("\t")
        if cols and cols[0].strip().isdigit():
            ids = [cols[0].strip()]
        else:
            ids = [a or b for a, b in _PASTED_ID.findall(line)]
            cols = []
        for i in ids:
            if i in seen:
                continue
            seen.add(i)
            out.append({"id": i, "promoted": len(cols) > 1 and cols[1].strip() == "promoted",
                        "title": cols[2].strip() if len(cols) > 2 else "",
                        "company": cols[3].strip() if len(cols) > 3 else ""})
    return out


def _get(url: str) -> tuple[int, str, str]:
    # Without an Accept header LinkedIn answers 429 even to the first request.
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "*/*",
                                               "Accept-Language": "en-US,en;q=0.9,ko;q=0.8"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status, r.geturl(), r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, url, ""


def fetch_guest(p: dict, max_jobs: int, delay: float) -> tuple[list[dict], str]:
    """Pages through the guest API until max_jobs, an empty page, or a block. Returns (jobs, stop reason)."""
    jobs, seen, start, backoffs = [], set(), 0, 0
    while len(jobs) < max_jobs:
        if start:
            time.sleep(delay * random.uniform(0.6, 1.6))
        status, final, body = _get(GUEST_SEARCH + "?" + urlencode({**p, "start": start}))
        if status == 429:
            if backoffs >= RETRIES_429:
                return jobs, "429 rate limited"
            backoffs += 1
            wait = 60 * backoffs * random.uniform(0.8, 1.3)
            print(f"  start={start}: 429, waiting {wait:.0f}s ({backoffs}/{RETRIES_429})", flush=True)
            time.sleep(wait)
            continue
        if status != 200 or re.search(r"/(authwall|login|checkpoint)", final):
            return jobs, f"HTTP {status} {final}"
        cards = parse_guest_cards(body)
        if not cards:
            return jobs, "no more results"
        for c in cards:
            if c["id"] not in seen:
                seen.add(c["id"])
                jobs.append({**c, "rank": len(jobs) + 1})
        print(f"  start={start}: {len(cards)} cards, {len(jobs)} total", flush=True)
        start += len(cards)
    return jobs[:max_jobs], "reached --max"


def cmd_guest(a) -> None:
    p = params(a.query, a.location, a.posted, a.remote)
    print(f"Guest search: {p}")
    jobs, why = fetch_guest(p, a.max, a.delay)
    out = Path(a.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"params": p, "fetched_at": datetime.now().isoformat(timespec="seconds"),
                               "stopped": why, "jobs": jobs}, ensure_ascii=False, indent=2))
    print(f"{len(jobs)} jobs ({why}) → {out}\n")
    print("Next, logged in, open this exact search (same keywords/location/filters):")
    print(f"  {LOGIN_SEARCH}?{urlencode(p)}\n")
    print("Then: python3 scripts/li_compare.py snippet  → run it in DevTools on each results page,")
    print("paste the clipboard into results/li-login.txt, and run: python3 scripts/li_compare.py compare results/li-login.txt")


def _row(j: dict) -> str:
    bits = [j.get("title") or "?", j.get("company") or ""]
    return f"{j['id']}  " + " · ".join(b for b in bits if b) + ("  [promoted]" if j.get("promoted") else "")


def report(guest: list[dict], login: list[dict], show: int = 15) -> str:
    g_ids, l_ids = [j["id"] for j in guest], [j["id"] for j in login]
    g_set, l_set = set(g_ids), set(l_ids)
    both = g_set & l_set
    organic = [j for j in login if not j.get("promoted")]
    o_set = {j["id"] for j in organic}
    lines = [f"guest {len(g_set)} · login {len(l_set)} (promoted {len(l_set) - len(o_set)}) · both {len(both)}"]
    if l_set:
        lines.append(f"login results also in guest: {len(both)}/{len(l_set)} = {len(both) / len(l_set):.0%}")
    if o_set:
        lines.append(f"  excluding promoted:        {len(o_set & g_set)}/{len(o_set)} = {len(o_set & g_set) / len(o_set):.0%}")
    if g_set:
        lines.append(f"guest results also in login: {len(both)}/{len(g_set)} = {len(both) / len(g_set):.0%}")
    for k in (10, 25, 50):
        if len(g_ids) >= k and len(l_ids) >= k:
            lines.append(f"top-{k} overlap: {len(set(g_ids[:k]) & set(l_ids[:k]))}/{k}")
    g_by = {j["id"]: j for j in guest}
    only_l = [j for j in login if j["id"] not in g_set]
    only_g = [g_by[i] for i in g_ids if i not in l_set]
    lines.append(f"\nonly logged in ({len(only_l)}):")
    lines += [f"  {_row(j)}" for j in only_l[:show]] + ([f"  … {len(only_l) - show} more"] if len(only_l) > show else [])
    lines.append(f"\nonly logged out ({len(only_g)}):")
    lines += [f"  {_row(j)}" for j in only_g[:show]] + ([f"  … {len(only_g) - show} more"] if len(only_g) > show else [])
    return "\n".join(lines)


def cmd_compare(a) -> None:
    data = json.loads(Path(a.guest).read_text())
    text = sys.stdin.read() if a.login == "-" else Path(a.login).read_text()
    login = parse_login(text)
    if not login:
        sys.exit("No job IDs found in the logged-in paste.")
    print(f"Search: {data['params']}  (guest fetched {data['fetched_at']}, stopped: {data['stopped']})\n")
    print(report(data["jobs"], login, a.show))


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    g = sub.add_parser("guest", help="fetch logged-out results")
    g.add_argument("--query", required=True)
    g.add_argument("--location", default="")
    g.add_argument("--posted", choices=list(POSTED), default="week")
    g.add_argument("--remote", choices=list(REMOTE), default="any")
    g.add_argument("--max", type=int, default=50, help="jobs to collect (default 50)")
    g.add_argument("--delay", type=float, default=10.0, help="mean seconds between pages")
    g.add_argument("--out", default=str(DEFAULT_GUEST))
    sub.add_parser("snippet", help="print the DevTools snippet for the logged-in page")
    c = sub.add_parser("compare", help="compare guest JSON vs pasted logged-in IDs")
    c.add_argument("login", help="file with the pasted logged-in results, or - for stdin")
    c.add_argument("--guest", default=str(DEFAULT_GUEST))
    c.add_argument("--show", type=int, default=15)
    a = ap.parse_args(argv)
    if a.cmd == "guest":
        cmd_guest(a)
    elif a.cmd == "snippet":
        print(SNIPPET)
    else:
        cmd_compare(a)


if __name__ == "__main__":
    main()
