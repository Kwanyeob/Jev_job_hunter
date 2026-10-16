"""Per-user watch scheduling for `jjh serve`: one cycle at a time, gaps, back-off, restart."""

from __future__ import annotations

import io
import random
from contextlib import redirect_stdout
from datetime import datetime, timedelta
from urllib.parse import parse_qs, urlsplit

import pytest

from jev_job_hunter import linkedin
from jev_job_hunter.scheduler import Hub, Scheduler
from jev_job_hunter.users import Users

from test_linkedin import ROOT, FakeLinkedIn, fake_client

T0 = datetime(2026, 10, 16, 10, 0)


class Clock:
    def __init__(self, t: datetime):
        self.t = t

    def __call__(self) -> datetime:
        return self.t


@pytest.fixture
def setup(tmp_path):
    users = Users(tmp_path)
    clock = Clock(T0)
    sites: dict[str, FakeLinkedIn] = {}
    pauses: list[float] = []
    current = {}

    def factory():
        site = FakeLinkedIn()
        sites[current["uid"]] = site
        return fake_client(site)

    hub = Hub()
    sched = Scheduler(users, hub, mock=True, clock=clock, client_factory=factory, delay=0,
                      quiet=None, pause=pauses.append, rng=random.Random(3))
    real_run_one = sched.run_one

    def run_one(uid):
        current["uid"] = uid
        with redirect_stdout(io.StringIO()):
            return real_run_one(uid)

    sched.run_one = run_one
    return users, sched, clock, sites, pauses, hub


def add_user(users: Users, name: str, with_profile: bool = True):
    user, _ = users.invite(name)
    if with_profile:
        cfg = users.dir(user.id) / "config"
        cfg.mkdir(parents=True)
        (cfg / "profile.yaml").write_text((ROOT / "config/profile.example.yaml").read_text(encoding="utf-8"),
                                          encoding="utf-8")
    return user


def posted_filters(site) -> set[str]:
    return {parse_qs(urlsplit(u).query).get("f_TPR", [""])[0] for u in site.visited if u.startswith(linkedin.GUEST_SEARCH)}


def test_two_users_run_one_after_another_with_a_gap(setup):
    users, sched, clock, sites, pauses, hub = setup
    a, b = add_user(users, "A"), add_user(users, "B")
    sched.set_enabled(a.id, True)
    clock.t += timedelta(seconds=5)
    sched.set_enabled(b.id, True)
    assert sched.due(clock.t) == [a.id, b.id]

    assert sched.step() == a.id
    assert sched.running is None and len(pauses) == 1 and 60 <= pauses[0] <= 180
    assert sched.step() == b.id
    assert set(sites) == {a.id, b.id}
    for u in (a, b):  # each user's own folder got its own state and Excel
        d = users.dir(u.id)
        assert (d / "state" / "linkedin-watch.json").is_file() and (d / "results" / "linkedin-watch.xlsx").is_file()
        nxt = datetime.fromisoformat(sched.schedule(u.id)["next_run"])
        assert timedelta(minutes=30) <= nxt - clock.t <= timedelta(minutes=60)
    kinds = [e["kind"] for e in hub.since(a.id, 0)]
    assert "card" in kinds and kinds[-1] == "cycle" and hub.since(a.id, 10_000) == []


def test_first_cycle_seeds_a_week_then_polls_a_day(setup):
    users, sched, clock, sites, pauses, _ = setup
    a = add_user(users, "A")
    sched.run_now(a.id)
    sched.step()
    assert posted_filters(sites[a.id]) == {"r604800"}
    sched.run_now(a.id)
    sched.step()
    assert posted_filters(sites[a.id]) == {"r86400"}


def test_run_now_while_disabled_runs_once(setup):
    users, sched, clock, sites, pauses, _ = setup
    a = add_user(users, "A")
    assert sched.due(clock.t) == []
    sched.run_now(a.id)
    assert sched.status(a.id)["queued"]
    sched.step()
    s = sched.schedule(a.id)
    assert (s["enabled"], s["manual"], s["next_run"]) == (False, False, None)
    assert sched.due(clock.t + timedelta(days=1)) == []


def test_disable_clears_next_run(setup):
    users, sched, clock, *_ = setup
    a = add_user(users, "A")
    assert sched.set_enabled(a.id, True)["next_run"]
    assert sched.set_enabled(a.id, False)["next_run"] is None


def test_blocked_cycle_backs_off(setup, monkeypatch):
    users, sched, clock, sites, pauses, _ = setup
    monkeypatch.setattr(linkedin, "RATE_LIMIT_BACKOFF", 0.0)
    sched.client_factory = lambda: fake_client(FakeLinkedIn(rate_limit=99))
    a = add_user(users, "A")
    sched.set_enabled(a.id, True)
    sched.step()
    s = sched.schedule(a.id)
    assert s["backoff"] == 2
    assert timedelta(minutes=60) <= datetime.fromisoformat(s["next_run"]) - clock.t <= timedelta(minutes=120)
    assert "rate limit" in sched.status(a.id)["last"]["error"]


def test_missing_profile_is_reported_not_fatal(setup):
    users, sched, clock, sites, pauses, hub = setup
    a = add_user(users, "A", with_profile=False)
    sched.run_now(a.id)
    assert sched.step() == a.id
    assert "프로필" in hub.since(a.id, 0)[-1]["data"]["error"]


def test_schedule_survives_restart(setup, tmp_path):
    users, sched, clock, *_ = setup
    a = add_user(users, "A")
    sched.set_enabled(a.id, True)
    fresh = Scheduler(Users(tmp_path), Hub(), mock=True, clock=clock, quiet=None)
    assert fresh.schedule(a.id)["enabled"] and fresh.due(clock.t) == [a.id]


def test_revoked_users_are_skipped(setup):
    users, sched, clock, *_ = setup
    a = add_user(users, "A")
    sched.set_enabled(a.id, True)
    users.revoke("A")
    assert sched.due(clock.t) == []
