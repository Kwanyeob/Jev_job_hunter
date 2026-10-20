"""One thread runs every user's `jjh watch` cycles for `jjh serve`, strictly one at a time.

LinkedIn sees a single, slow client: runs never overlap, and a random gap separates one run from
the next. Two kinds of run, so requests never bunch up (LinkedIn answers 429 after ~6-8 quick ones
and seems to allow that many again after ~10 quiet minutes):
- search: every 30–60 min (watch.next_run_at: quiet 01–07, doubling after a blocked run). With the
  watch on it only searches and lets Jev triage; the jobs worth opening join the user's queue.
- read: while that queue is not empty, every 8–14 min, read and Jev-score the next 5 postings.
A manual "search now" with the watch off reads 5 straight away instead, since no reads follow.
Timing is kept in data/users/<id>/state/schedule.json so a restart picks up where it left off.
"""

from __future__ import annotations

import json
import os
import random
import threading
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

from jev_job_hunter.profile import load_profile
from jev_job_hunter.questions import LINKEDIN_DELAY
from jev_job_hunter.users import Users
from jev_job_hunter.watch import (
    BACKOFF_MAX, WATCH_EVERY, WATCH_POSTED, WATCH_QUIET, WatchState, first_run_at, in_quiet, next_run_at,
    quiet_end, run_cycle,
)

GAP_BETWEEN = (60.0, 180.0)  # seconds after a search run before the next run, whoever it belongs to
READ_COOLDOWN = (240.0, 360.0)  # seconds after a read run (5 postings) before the next run
READ_EVERY = (8.0, 14.0)     # minutes between one user's read runs while jobs wait in their queue
RUN_NOW_COOLDOWN = 10.0      # minutes after a search before "search now" may ask for another
READ_STEP = 12.0             # rough minutes from one read run to the next (8–14 min gap + the run itself)
# search intervals a user can pick (minutes, drawn at random between the two each time)
EVERY_OPTIONS = {"30-60": (30.0, 60.0), "60-120": (60.0, 120.0), "180-240": (180.0, 240.0), "360-480": (360.0, 480.0)}
MAX_QUERIES = 3              # per user, so the operator's Jev quota and LinkedIn stay comfortable
MAX_OPEN = 5                 # postings read per run; the rest wait in the queue (fewer 429s)
STATE_KEY = "web"            # one watch state per user, kept across profile edits
EVENTS_KEPT = 500
IDLE_POLL = 30.0


class Hub:
    """Per-user event log the web UI streams over SSE. Ids grow per user; old events roll off."""

    def __init__(self, keep: int = EVENTS_KEPT):
        self.keep = keep
        self.lock = threading.Lock()
        self.events: dict[str, list[dict]] = defaultdict(list)
        self.next_id: dict[str, int] = defaultdict(int)

    def emit(self, uid: str, kind: str, data: dict) -> None:
        with self.lock:
            self.next_id[uid] += 1
            log = self.events[uid]
            log.append({"id": self.next_id[uid], "kind": kind, "data": data})
            del log[:-self.keep]

    def since(self, uid: str, last_id: int) -> list[dict]:
        with self.lock:
            return [e for e in self.events[uid] if e["id"] > last_id]


class Scheduler:
    def __init__(self, users: Users, hub: Hub, *, mock: bool | None = None, clock=datetime.now,
                 client_factory=None, every=WATCH_EVERY, quiet=WATCH_QUIET, gap=GAP_BETWEEN,
                 delay: float = LINKEDIN_DELAY, pause=None, rng=random):
        self.users, self.hub, self.clock, self.rng = users, hub, clock, rng
        self.mock, self.client_factory, self.delay = mock, client_factory, delay
        self.every_default, self.quiet, self.gap = every, quiet, gap
        self.lock = threading.Lock()
        self.wake = threading.Event()
        self.stopping = threading.Event()
        self.pause = pause or self.stopping.wait  # gap between cycles: only shutdown cuts it short
        self.running: str | None = None
        self.running_kind: str | None = None
        self.thread: threading.Thread | None = None

    # --- per-user schedule -------------------------------------------------------------------
    def _path(self, uid: str) -> Path:
        return self.users.dir(uid) / "state" / "schedule.json"

    def schedule(self, uid: str) -> dict:
        p = self._path(uid)
        data = json.loads(p.read_text(encoding="utf-8")) if p.is_file() else {}
        return {"enabled": False, "next_run": None, "next_read": None, "backoff": 1, "manual": False,
                "every": None, **data}

    def every(self, uid: str) -> tuple[float, float]:
        return EVERY_OPTIONS.get(self.schedule(uid)["every"] or "", self.every_default)

    def set_every(self, uid: str, key: str) -> dict:
        """Change how often this user's searches run; a pending search moves to fit the new interval."""
        if key not in EVERY_OPTIONS:
            raise ValueError(key)
        with self.lock:
            s = self.schedule(uid)
            s["every"] = key
            if s["enabled"] and s["next_run"] and not s["manual"]:
                state = WatchState.load(self.users.dir(uid), STATE_KEY)
                searches = [c for c in state.cycles if c.get("kind", "search") == "search"]
                now = self.clock()
                base = datetime.strptime(searches[-1]["at"], "%Y-%m-%d %H:%M") if searches else now
                t = max(base + timedelta(minutes=self.rng.uniform(*EVERY_OPTIONS[key])), now + timedelta(minutes=2))
                s["next_run"] = (quiet_end(t, self.quiet, self.rng) if in_quiet(t, self.quiet) else t).isoformat(timespec="seconds")
            self._save(uid, s)
        self.wake.set()
        return s

    def _save(self, uid: str, s: dict) -> None:
        p = self._path(uid)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(s, indent=1) + "\n", encoding="utf-8")
        os.replace(tmp, p)

    def set_enabled(self, uid: str, on: bool) -> dict:
        with self.lock:
            s = self.schedule(uid)
            s["enabled"] = on
            if on and not s["next_run"]:
                s["next_run"] = first_run_at(self.clock(), self.quiet, self.rng).isoformat(timespec="seconds")
            if not on and not s["manual"]:
                s["next_run"] = None
            if not on:
                s["next_read"] = None
            self._save(uid, s)
        self.wake.set()
        return s

    def run_now(self, uid: str) -> dict:
        with self.lock:
            s = self.schedule(uid)
            s["next_run"], s["manual"] = self.clock().isoformat(timespec="seconds"), True
            self._save(uid, s)
        self.wake.set()
        return s

    def status(self, uid: str) -> dict:
        s = self.schedule(uid)
        state = WatchState.load(self.users.dir(uid), STATE_KEY)
        searches = [c for c in state.cycles if c.get("kind", "search") == "search"]
        run_now_at = None  # "search now" stays off this soon after a search, so searches never bunch up
        if searches:
            ready = datetime.strptime(searches[-1]["at"], "%Y-%m-%d %H:%M") + timedelta(minutes=RUN_NOW_COOLDOWN)
            if ready > self.clock():
                run_now_at = ready.isoformat(timespec="seconds")
        return {"enabled": s["enabled"], "next_run": s["next_run"], "next_read": s["next_read"],
                "every": s["every"] if s["every"] in EVERY_OPTIONS else "30-60",
                "pending": len(state.pending), "running": self.running == uid, "run_now_at": run_now_at,
                "queued": self._due_kind(uid, self.clock()) == "search" and self.running != uid,
                "last": searches[-1] if searches else None, "last_any": state.cycles[-1] if state.cycles else None}

    def eval_plan(self, uid: str, state: WatchState) -> dict[str, dict]:
        """When each queued job should get its Jev fit: {"state": reading|queued|paused, "at": iso?}.
        Read runs take the queue 5 at a time about every READ_STEP minutes, so this is an estimate."""
        s = self.schedule(uid)
        reading = self.running == uid and self.running_kind == "read"
        base = self.clock() if reading else (datetime.fromisoformat(s["next_read"]) if s["next_read"] else None)
        plan = {}
        for i, c in enumerate(state.pending):
            batch = i // MAX_OPEN
            if reading and batch == 0:
                plan[c["id"]] = {"state": "reading"}
            elif not s["enabled"]:
                plan[c["id"]] = {"state": "paused"}
            elif base is None:
                plan[c["id"]] = {"state": "queued"}
            else:
                at = base + timedelta(minutes=READ_STEP * batch)
                plan[c["id"]] = {"state": "queued", "at": at.isoformat(timespec="seconds")}
        return plan

    def _due_kind(self, uid: str, now: datetime) -> str | None:
        """'search', 'read' or None. A search wins when both are due."""
        s = self.schedule(uid)
        if (s["enabled"] or s["manual"]) and s["next_run"] and datetime.fromisoformat(s["next_run"]) <= now:
            return "search"
        if s["enabled"] and s["next_read"] and datetime.fromisoformat(s["next_read"]) <= now:
            return "read"
        return None

    def due(self, now: datetime) -> list[str]:
        ready = []
        for u in self.users.active():
            if self._due_kind(u.id, now):
                s = self.schedule(u.id)
                ready.append((min(t for t in (s["next_run"], s["next_read"]) if t), u.id))
        return [uid for _, uid in sorted(ready)]

    def _read_at(self, now: datetime, minutes: tuple[float, float] = READ_EVERY) -> str:
        t = now + timedelta(minutes=self.rng.uniform(*minutes))
        return (quiet_end(t, self.quiet, self.rng) if in_quiet(t, self.quiet) else t).isoformat(timespec="seconds")

    # --- running cycles ----------------------------------------------------------------------
    def run_one(self, uid: str, kind: str = "search") -> dict:
        root = self.users.dir(uid)
        emit = lambda kind, data: self.hub.emit(uid, kind, data)  # noqa: E731
        try:
            prof = load_profile(root)
        except SystemExit:
            summary = {"error": "no profile yet", "new": 0, "scored": 0}
        else:
            search = prof["search"]
            state = WatchState.load(root, STATE_KEY)
            posted = WATCH_POSTED if state.seen else "week"  # first cycle seeds a week of postings
            mock = self.mock if self.mock is not None else not (os.environ.get("TYPESAFE_API_KEY") or "").strip()
            client = self.client_factory() if self.client_factory else None
            enabled = self.schedule(uid)["enabled"]
            # a search with the watch on leaves reading to the read runs; a one-off search reads its 5 now
            queries = search["queries"][:MAX_QUERIES] if kind == "search" else []
            max_open = 0 if kind == "search" and enabled else MAX_OPEN
            self.running, self.running_kind = uid, kind
            emit("status", {"msg": "Searching LinkedIn for new jobs" if queries else "Reading queued postings"})
            try:
                summary = run_cycle(root, state, prof, queries, search["location"],
                                    posted, search["remote"], max_open=max_open, delay=self.delay, mock=mock,
                                    client=client, clock=self.clock, emit=emit)
            except Exception as e:  # one user's failure must not stop everyone's schedule
                summary = {"error": f"{type(e).__name__}: {e}", "new": 0, "scored": 0}
            finally:
                self.running = self.running_kind = None
        now = self.clock()
        with self.lock:
            s = self.schedule(uid)
            if kind == "search":
                s["backoff"] = min(s["backoff"] * 2, BACKOFF_MAX) if summary.get("error") else 1
                s["manual"] = False
                s["next_run"] = (next_run_at(now, self.every(uid), self.quiet, s["backoff"], self.rng)
                                 .isoformat(timespec="seconds") if s["enabled"] else None)
            if s["enabled"] and summary.get("pending"):
                blocked = kind == "read" and summary.get("error")  # a blocked read waits twice as long
                s["next_read"] = self._read_at(now, tuple(2 * m for m in READ_EVERY) if blocked else READ_EVERY)
            else:
                s["next_read"] = None
            self._save(uid, s)
        emit("cycle", {k: summary.get(k) for k in ("at", "new", "scored", "strong", "listings", "matches", "pending", "error")}
             | {"kind": kind, "next_run": s["next_run"], "next_read": s["next_read"]})
        return summary

    def step(self) -> str | None:
        """Run the most overdue user's cycle and then hold the inter-cycle gap; else idle briefly."""
        now = self.clock()
        due = self.due(now)
        if due:
            kind = self._due_kind(due[0], now)
            self.run_one(due[0], kind)
            self.pause(self.rng.uniform(*(READ_COOLDOWN if kind == "read" else self.gap)))
            return due[0]
        nexts = []
        for s in (self.schedule(u.id) for u in self.users.active()):
            if (s["enabled"] or s["manual"]) and s["next_run"]:
                nexts.append(datetime.fromisoformat(s["next_run"]))
            if s["enabled"] and s["next_read"]:
                nexts.append(datetime.fromisoformat(s["next_read"]))
        wait = min([IDLE_POLL] + [max(0.0, (t - now).total_seconds()) for t in nexts])
        self.wake.wait(wait)
        self.wake.clear()
        return None

    def start(self) -> None:
        def loop():
            while not self.stopping.is_set():
                try:
                    self.step()
                except Exception as e:  # keep the service alive; the next step retries
                    print(f"  scheduler error: {type(e).__name__}: {e}", flush=True)
                    self.stopping.wait(IDLE_POLL)
        self.thread = threading.Thread(target=loop, name="jjh-scheduler", daemon=True)
        self.thread.start()

    def stop(self) -> None:
        self.stopping.set()
        self.wake.set()
