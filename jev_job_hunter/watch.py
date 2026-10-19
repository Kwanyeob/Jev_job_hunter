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
    PAGE_SIZE, AuthWall, GuestClient, RateLimited, Stopped, hunt_meta, read_cards, report_triage,
    search_queries, triage, worth_reading,
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
PENDING_MAX = 30             # triaged jobs waiting for a read slot; the lowest-rated fall off
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
        self.pending: list[dict] = data.get("pending") or []  # worth reading, not read yet

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
                                   "matches": self.matches, "cycles": self.cycles[-CYCLES_KEPT:],
                                   "pending": self.pending},
                                  indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
        _replace(tmp, self.path)


def _replace(tmp: Path, path: Path) -> None:
    """os.replace, retried briefly: on Windows it fails while the web server is reading the file."""
    for attempt in range(20):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            if attempt == 19:
                raise
            time.sleep(0.05)


def dismissed(root: Path) -> set[str]:
    """Job IDs the user knocked out in the web UI. Kept apart from the watch state, which a running
    cycle rewrites, so a knock-out made mid-cycle is never lost."""
    path = root / "state" / "dismissed.json"
    return set(json.loads(path.read_text(encoding="utf-8"))) if path.is_file() else set()


def set_dismissed(root: Path, ids: list[str], on: bool = True) -> set[str]:
    current = dismissed(root)
    current = current | set(ids) if on else current - set(ids)
    path = root / "state" / "dismissed.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(sorted(current)) + "\n", encoding="utf-8")
    _replace(tmp, path)
    return current


def write_state_xlsx(root: Path, state: WatchState, meta: dict, new_ids: frozenset[str] = frozenset()) -> Path:
    """The cumulative Excel: every match and listing the user hasn't knocked out."""
    gone = dismissed(root)
    return write_xlsx(state.xlsx,
                      sorted((m for m in state.matches if m["id"] not in gone),
                             key=lambda m: m.get("fit") or 0, reverse=True),
                      sorted((c for c in state.listings if c["id"] not in gone),
                             key=lambda c: c.get("open") or 0, reverse=True),
                      meta, new_ids=new_ids)


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
              client: GuestClient | None = None, clock=datetime.now, emit=None) -> dict:
    """One poll: search page 1 per query (more only while every card is unseen), Jev-triage the new
    cards, then read + fit up to `max_open` of the best-rated jobs waiting (earlier cycles' leftovers
    and this cycle's). The rest wait in `state.pending` for the next cycle, which keeps each cycle's
    LinkedIn reads few. Each card, triage and fit is saved the moment it arrives (the web UI shows
    them live, and a crash keeps them); the cumulative Excel is rewritten at the end."""
    cand = prof["candidate"]
    own = client is None
    client = client or GuestClient(delay)
    started = clock()
    stamp = started.strftime("%Y-%m-%d %H:%M")
    cards: list[dict] = []
    matches: list[dict] = []
    gone = dismissed(root)
    queue = worth_reading([c for c in state.pending if c["id"] not in gone])
    to_read: list[dict] = []
    error, stopped = "", False

    by_id = {c["id"]: c for c in state.listings}

    def fill_logo(job_id: str, logo: str) -> bool:
        listing = by_id.get(job_id)
        if listing is None or listing.get("logo") or not logo:
            return False
        listing["logo"] = logo
        return True

    def record(kind: str, data: dict) -> None:
        if kind == "seen":  # older jobs found before logos were kept get theirs here, at no extra request
            if fill_logo(data["id"], data.get("logo") or ""):
                state.save()
            return
        if kind == "card":  # the same dict collect_cards returns, so later triage scores land here too
            data["first_seen"] = stamp
            state.seen[data["id"]] = stamp
            state.listings.append(data)
            by_id[data["id"]] = data
            state.save()
        elif kind == "triage":
            state.save()
        elif kind == "fit":
            state.matches.append(matches[-1])
            if data["id"] in by_id:  # the job may have been listed in an earlier cycle
                by_id[data["id"]]["opened"] = True
            fill_logo(data["id"], data.get("logo") or "")
            state.save()
        if emit:  # `emit(kind, data)` streams progress to the web UI
            emit(kind, data)

    try:
        cards = search_queries(client, queries, location, posted, remote, WATCH_MAX_PAGES * PAGE_SIZE,
                               seen=set(state.seen), stop_at_seen=True, emit=record,
                               experience=cand.get("target_seniority") or ())
        if cards:
            record("status", {"msg": f"Jev is triaging {len(cards)} titles"})
            jev_ms = triage(cand, " | ".join(queries), cards, mock)
            queue = worth_reading(queue + cards)
        to_read, queue = queue[:max_open], queue[max_open:]
        if cards or to_read:
            report_triage(root, cards, to_read, jev_ms if cards else 0, emit=record)
        read_cards(root, cand, to_read, client, matches, mock=mock, emit=record)
    except AuthWall as e:
        error = f"LinkedIn login wall ({e})"
    except RateLimited as e:
        error = f"LinkedIn rate limit ({e})"
    except (Stopped, KeyboardInterrupt):
        error, stopped = "stopped", True
    except Exception as e:  # e.g. Jev unreachable: keep what was found, report it, try again next cycle
        error = f"{type(e).__name__}: {e}"
    finally:
        if own:
            client.close()

    # Picked jobs a block or stop kept us from reading go back to the front of the queue.
    read = {m["id"] for m in matches}
    unread = [c for c in to_read if c["id"] not in read]
    for c in unread:
        c["opened"] = False
    state.pending = worth_reading(unread + queue)[:PENDING_MAX]

    strong = sorted((m for m in matches if (m.get("fit") or 0) >= FIT_SAVE_THRESHOLD),
                    key=lambda m: m["fit"], reverse=True)
    summary = {"at": stamp, "kind": "search" if queries else "read",
               "new": len(cards), "scored": len(matches), "strong": len(strong),
               "requests": client.requests, "listings": len(state.listings), "matches": len(state.matches),
               "pending": len(state.pending), "error": error}
    state.cycles.append(summary)
    state.save()
    meta = {**hunt_meta(prof, queries, location, posted, remote, mock), "watch.state": str(state.path),
            "watch.cycles": len(state.cycles), "watch.last_run": stamp}
    xlsx = write_state_xlsx(root, state, meta, new_ids=frozenset(c["id"] for c in cards))
    append_log(root, {"cmd": "watch", "stage": "cycle", "links": len(cards),
                      "total_ms": int((clock() - started).total_seconds() * 1000),
                      "action": error or f"NEW {len(cards)}"})
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
