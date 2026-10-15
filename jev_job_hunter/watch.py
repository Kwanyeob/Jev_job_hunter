"""`jjh watch`: poll LinkedIn at irregular 30–60 min gaps, score only postings not seen before,
and keep one cumulative Excel (results/linkedin-watch.xlsx). Quiet at night; backs off when blocked.

State lives in state/linkedin-watch.json: every job ID seen (with when), all listings and matches,
and a short history of cycles. It is rewritten atomically after each cycle.
"""

from __future__ import annotations

import hashlib
import json
import os
import random
import time
from datetime import datetime, timedelta
from pathlib import Path

from jev_job_hunter import board
from jev_job_hunter.excel import write_xlsx
from jev_job_hunter.linkedin import (
    PAGE_SIZE, AuthWall, GuestClient, RateLimited, Stopped, hunt_meta, score_cards, search_queries,
)
from jev_job_hunter.profile import load_profile
from jev_job_hunter.questions import FIT_SAVE_THRESHOLD
from jev_job_hunter.store import append_log

WATCH_EVERY = (30.0, 60.0)   # minutes between cycles, drawn uniformly each time
WATCH_QUIET = (1, 7)         # local hours with no requests (start inclusive, end exclusive)
WATCH_MAX_PAGES = 3          # per query per cycle; page 2+ only while every card is new
WATCH_POSTED = "day"
BACKOFF_MAX = 8              # gap multiplier cap after blocked cycles
GAP_CAP_MIN = 240.0          # a backed-off gap never exceeds 4 h
WAKE_JITTER_MIN = 20.0       # leave quiet hours at end + 0–20 min
CYCLES_KEPT = 50
TICK = 30.0                  # seconds per sleep slice, so a laptop waking from sleep catches up


def watch_key(queries: list[str], location: str, posted: str, remote: str) -> str:
    blob = json.dumps({"q": sorted(q.strip().lower() for q in queries), "l": location.strip().lower(),
                       "p": posted, "r": remote}, sort_keys=True)
    return hashlib.sha1(blob.encode()).hexdigest()[:8]


class WatchState:
    def __init__(self, path: Path, key: str, data: dict | None = None):
        data = data or {}
        self.path, self.key = path, key
        self.seen: dict[str, str] = data.get("seen") or {}
        self.listings: list[dict] = data.get("listings") or []
        self.matches: list[dict] = data.get("matches") or []
        self.cycles: list[dict] = data.get("cycles") or []

    @classmethod
    def load(cls, root: Path, key: str) -> WatchState:
        """state/linkedin-watch.json, unless it belongs to another search: then a file of its own."""
        path = root / "state" / "linkedin-watch.json"
        if path.is_file():
            data = json.loads(path.read_text(encoding="utf-8"))
            if data.get("key") == key:
                return cls(path, key, data)
            print(f"  note: {path.name} tracks a different search; using linkedin-watch-{key}.json", flush=True)
            path = path.with_name(f"linkedin-watch-{key}.json")
        data = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else None
        return cls(path, key, data)

    @property
    def xlsx(self) -> Path:
        return self.path.parent.parent / "results" / f"{self.path.stem}.xlsx"

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"key": self.key, "seen": self.seen, "listings": self.listings,
                                   "matches": self.matches, "cycles": self.cycles[-CYCLES_KEPT:]},
                                  indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
        os.replace(tmp, self.path)


def in_quiet(t: datetime, quiet: tuple[int, int] | None) -> bool:
    if not quiet:
        return False
    start, end = quiet
    return start <= t.hour < end if start < end else (t.hour >= start or t.hour < end)


def quiet_end(t: datetime, quiet: tuple[int, int], rng=random) -> datetime:
    """When quiet hours that contain `t` end, plus a few random minutes."""
    end = t.replace(hour=quiet[1], minute=0, second=0, microsecond=0)
    if end <= t:
        end += timedelta(days=1)
    return end + timedelta(minutes=rng.uniform(0, WAKE_JITTER_MIN))


def next_run_at(now: datetime, every: tuple[float, float] = WATCH_EVERY,
                quiet: tuple[int, int] | None = WATCH_QUIET, backoff: int = 1, rng=random) -> datetime:
    minutes = rng.uniform(*every) * backoff
    if backoff > 1:
        minutes = min(minutes, max(GAP_CAP_MIN, every[1]))
    t = now + timedelta(minutes=minutes)
    return quiet_end(t, quiet, rng) if in_quiet(t, quiet) else t


def first_run_at(now: datetime, quiet: tuple[int, int] | None, rng=random) -> datetime:
    return quiet_end(now, quiet, rng) if in_quiet(now, quiet) else now


def wait_until(at: datetime, clock=datetime.now, sleeper=time.sleep) -> None:
    """Sleep in short slices against the wall clock (a suspended laptop resumes and runs at once)."""
    while (left := (at - clock()).total_seconds()) > 0:
        sleeper(min(left, TICK))


def run_cycle(root: Path, state: WatchState, prof: dict, queries: list[str], location: str,
              posted: str, remote: str, *, max_open: int, delay: float, mock: bool,
              client: GuestClient | None = None, clock=datetime.now) -> dict:
    """One poll: search page 1 per query (more only while every card is unseen), score the new ones,
    fold them into the state and rewrite the cumulative Excel. Returns a cycle summary."""
    cand = prof["candidate"]
    own = client is None
    client = client or GuestClient(delay)
    started = clock()
    stamp = started.strftime("%Y-%m-%d %H:%M")
    cards: list[dict] = []
    matches: list[dict] = []
    error, stopped = "", False
    try:
        cards = search_queries(client, queries, location, posted, remote, WATCH_MAX_PAGES * PAGE_SIZE,
                               seen=set(state.seen), stop_at_seen=True)
        for c in cards:
            c["first_seen"] = stamp
        if cards:
            score_cards(root, cand, " | ".join(queries), cards, client, matches, max_open=max_open, mock=mock)
    except AuthWall as e:
        error = f"LinkedIn login wall ({e})"
    except RateLimited as e:
        error = f"LinkedIn rate limit ({e})"
    except (Stopped, KeyboardInterrupt):
        error, stopped = "stopped", True
    finally:
        if own:
            client.close()

    # Cards picked for a read that never happened (blocked / stopped) stay unseen, so the next
    # cycle triages and reads them again instead of losing them.
    read = {m["id"] for m in matches}
    keep = [c for c in cards if not (error and c.get("opened") and c["id"] not in read)]
    for c in keep:
        state.seen[c["id"]] = stamp
    state.listings += keep
    state.matches += matches

    strong = sorted((m for m in matches if (m.get("fit") or 0) >= FIT_SAVE_THRESHOLD),
                    key=lambda m: m["fit"], reverse=True)
    summary = {"at": stamp, "new": len(keep), "scored": len(matches), "strong": len(strong),
               "requests": client.requests, "listings": len(state.listings), "matches": len(state.matches),
               "error": error}
    state.cycles.append(summary)
    state.save()
    meta = {**hunt_meta(prof, queries, location, posted, remote, mock), "watch.state": str(state.path),
            "watch.cycles": len(state.cycles), "watch.last_run": stamp}
    xlsx = write_xlsx(state.xlsx,
                      sorted(state.matches, key=lambda m: m.get("fit") or 0, reverse=True),
                      sorted(state.listings, key=lambda c: c.get("open") or 0, reverse=True),
                      meta, new_ids=frozenset(c["id"] for c in keep))
    append_log(root, {"cmd": "watch", "stage": "cycle", "links": len(keep),
                      "total_ms": int((clock() - started).total_seconds() * 1000),
                      "action": error or f"NEW {len(keep)}"})
    return {**summary, "stopped": stopped, "xlsx": str(xlsx), "strong_matches": strong}


def watch(root: Path, *, profile_path: str | None = None, queries: list[str] | None = None,
          location: str | None = None, posted: str | None = None, remote: str | None = None,
          max_open: int, delay: float, mock: bool, every: tuple[float, float] = WATCH_EVERY,
          quiet: tuple[int, int] | None = WATCH_QUIET, once: bool = False,
          client_factory=None, clock=datetime.now, sleeper=time.sleep, max_cycles: int | None = None) -> None:
    prof = load_profile(root, profile_path)
    search = prof["search"]
    queries = [q for q in (queries or search["queries"]) if q.strip()]
    location = search["location"] if location is None else location
    posted = posted or WATCH_POSTED
    remote = remote or search["remote"]
    board.print_li_start(prof["candidate"], queries, location, posted, remote)
    state = WatchState.load(root, watch_key(queries, location, posted, remote))
    board.print_watch_start(state.path, len(state.seen), every, quiet, once)

    def cycle() -> dict:
        client = client_factory() if client_factory else None
        return run_cycle(root, state, prof, queries, location, posted, remote, max_open=max_open,
                         delay=delay, mock=mock, client=client, clock=clock)

    if once:
        if in_quiet(clock(), quiet):
            print(f"  quiet hours ({quiet[0]:02d}–{quiet[1]:02d}); nothing fetched. Use --quiet none to override.")
            return
        board.print_watch_cycle(cycle(), None)
        return

    at, backoff, n = first_run_at(clock(), quiet), 1, 0
    try:
        while True:
            if at > clock():
                print(f"  next run {at:%H:%M}", flush=True)
            wait_until(at, clock, sleeper)
            try:
                summary = cycle()
            except Exception as e:  # e.g. Jev unreachable: keep watching, just slower
                summary = {"error": f"{type(e).__name__}: {e}", "stopped": False}
                print(f"  cycle failed: {summary['error']}", flush=True)
            if summary["stopped"]:
                break
            backoff = min(backoff * 2, BACKOFF_MAX) if summary["error"] else 1
            at = next_run_at(clock(), every, quiet, backoff)
            if "xlsx" in summary:
                board.print_watch_cycle(summary, at)
            n += 1
            if max_cycles and n >= max_cycles:
                break
    except KeyboardInterrupt:
        pass
    print(f"  watch stopped. State {state.path}", flush=True)
