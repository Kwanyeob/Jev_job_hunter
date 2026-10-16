"""One thread runs every user's `jjh watch` cycle for `jjh serve`, strictly one at a time.

LinkedIn sees a single, slow client: cycles never overlap, and a random 1–3 min gap separates one
user's cycle from the next. Per-user timing reuses watch.next_run_at (30–60 min, quiet 01–07,
doubling after a blocked cycle) and is kept in data/users/<id>/state/schedule.json so a restart
picks up where it left off. "Search now" just makes a user due immediately; it waits its turn.
"""

from __future__ import annotations

import json
import os
import random
import threading
from collections import defaultdict
from datetime import datetime
from pathlib import Path

from jev_job_hunter.profile import load_profile
from jev_job_hunter.questions import LINKEDIN_DELAY
from jev_job_hunter.users import Users
from jev_job_hunter.watch import (
    BACKOFF_MAX, WATCH_EVERY, WATCH_POSTED, WATCH_QUIET, WatchState, first_run_at, next_run_at, run_cycle,
)

GAP_BETWEEN = (60.0, 180.0)  # seconds between two cycles, whoever they belong to
MAX_QUERIES = 3              # per user, so the operator's Jev quota and LinkedIn stay comfortable
MAX_OPEN = 10                # postings read and fit-scored per cycle
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
        self.every, self.quiet, self.gap = every, quiet, gap
        self.lock = threading.Lock()
        self.wake = threading.Event()
        self.stopping = threading.Event()
        self.pause = pause or self.stopping.wait  # gap between cycles: only shutdown cuts it short
        self.running: str | None = None
        self.thread: threading.Thread | None = None

    # --- per-user schedule -------------------------------------------------------------------
    def _path(self, uid: str) -> Path:
        return self.users.dir(uid) / "state" / "schedule.json"

    def schedule(self, uid: str) -> dict:
        p = self._path(uid)
        data = json.loads(p.read_text(encoding="utf-8")) if p.is_file() else {}
        return {"enabled": False, "next_run": None, "backoff": 1, "manual": False, **data}

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
        due = [u.id for u in self.users.active() if self._is_due(u.id, self.clock())]
        return {"enabled": s["enabled"], "next_run": s["next_run"], "running": self.running == uid,
                "queued": uid in due and self.running != uid, "last": state.cycles[-1] if state.cycles else None}

    def _is_due(self, uid: str, now: datetime) -> bool:
        s = self.schedule(uid)
        return bool((s["enabled"] or s["manual"]) and s["next_run"]
                    and datetime.fromisoformat(s["next_run"]) <= now)

    def due(self, now: datetime) -> list[str]:
        ready = [(self.schedule(u.id)["next_run"], u.id) for u in self.users.active() if self._is_due(u.id, now)]
        return [uid for _, uid in sorted(ready)]

    # --- running cycles ----------------------------------------------------------------------
    def run_one(self, uid: str) -> dict:
        root = self.users.dir(uid)
        emit = lambda kind, data: self.hub.emit(uid, kind, data)  # noqa: E731
        try:
            prof = load_profile(root)
        except SystemExit:
            summary = {"error": "프로필이 없습니다. 이력서를 올리거나 프로필을 저장해 주세요.", "new": 0, "scored": 0}
        else:
            search = prof["search"]
            state = WatchState.load(root, STATE_KEY)
            posted = WATCH_POSTED if state.seen else "week"  # first cycle seeds a week of postings
            mock = self.mock if self.mock is not None else not (os.environ.get("TYPESAFE_API_KEY") or "").strip()
            client = self.client_factory() if self.client_factory else None
            self.running = uid
            emit("status", {"msg": "LinkedIn에서 공고를 찾는 중입니다"})
            try:
                summary = run_cycle(root, state, prof, search["queries"][:MAX_QUERIES], search["location"],
                                    posted, search["remote"], max_open=MAX_OPEN, delay=self.delay, mock=mock,
                                    client=client, clock=self.clock, emit=emit)
            except Exception as e:  # one user's failure must not stop everyone's schedule
                summary = {"error": f"{type(e).__name__}: {e}", "new": 0, "scored": 0}
            finally:
                self.running = None
        with self.lock:
            s = self.schedule(uid)
            s["backoff"] = min(s["backoff"] * 2, BACKOFF_MAX) if summary.get("error") else 1
            s["manual"] = False
            s["next_run"] = (next_run_at(self.clock(), self.every, self.quiet, s["backoff"], self.rng)
                             .isoformat(timespec="seconds") if s["enabled"] else None)
            self._save(uid, s)
        emit("cycle", {k: summary.get(k) for k in ("at", "new", "scored", "strong", "listings", "matches", "error")}
             | {"next_run": s["next_run"]})
        return summary

    def step(self) -> str | None:
        """Run the most overdue user's cycle and then hold the inter-cycle gap; else idle briefly."""
        now = self.clock()
        due = self.due(now)
        if due:
            self.run_one(due[0])
            self.pause(self.rng.uniform(*self.gap))
            return due[0]
        nexts = [datetime.fromisoformat(s["next_run"]) for s in (self.schedule(u.id) for u in self.users.active())
                 if (s["enabled"] or s["manual"]) and s["next_run"]]
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
