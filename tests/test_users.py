"""Invite links and signed sessions for `jjh serve`."""

from __future__ import annotations

import time

import pytest

from jev_job_hunter.users import SESSION_DAYS, Users, make_session, read_session, secret_key

KEY = b"k" * 32


def test_invite_stores_only_a_hash(tmp_path):
    users = Users(tmp_path)
    user, token = users.invite("Minsu")
    assert users.by_token(token).id == user.id
    assert token not in users.path.read_text(encoding="utf-8")
    assert users.by_token("wrong") is None and users.by_token("") is None
    assert users.find("minsu").id == user.id  # names match case-insensitively


def test_reinvite_rotates_link_and_logs_out(tmp_path):
    users = Users(tmp_path)
    user, old = users.invite("Minsu")
    cookie = make_session(KEY, user)
    assert read_session(KEY, users, cookie).id == user.id
    again, new = users.invite("minsu")
    assert again.id == user.id and len(users.all()) == 1
    assert users.by_token(old) is None and users.by_token(new).id == user.id
    assert read_session(KEY, users, cookie) is None  # bound to the old token


def test_revoke_and_delete(tmp_path):
    users = Users(tmp_path)
    user, token = users.invite("Jiwoo")
    cookie = make_session(KEY, user)
    (users.dir(user.id) / "config").mkdir(parents=True)
    users.revoke("Jiwoo")
    assert users.by_token(token) is None and read_session(KEY, users, cookie) is None
    assert users.dir(user.id).is_dir()  # data kept
    users.revoke("jiwoo", delete=True)
    assert users.all() == [] and not users.dir(user.id).exists()
    with pytest.raises(KeyError):
        users.revoke("nobody")


def test_session_rejects_tampering_expiry_and_bad_ids(tmp_path):
    users = Users(tmp_path)
    user, _ = users.invite("A")
    cookie = make_session(KEY, user)
    uid, issued, bind, sig = cookie.split(".")
    assert read_session(KEY, users, f"{uid}.{issued}.{bind}.{'0' * 64}") is None
    assert read_session(b"other-key", users, cookie) is None
    assert read_session(KEY, users, "garbage") is None
    old = make_session(KEY, user, now=time.time() - (SESSION_DAYS + 1) * 86400)
    assert read_session(KEY, users, old) is None
    with pytest.raises(ValueError):
        users.dir("../../etc")


def test_secret_key_env_wins_else_generated_once(tmp_path, monkeypatch):
    monkeypatch.delenv("SECRET_KEY", raising=False)
    first = secret_key(tmp_path)
    assert len(first) == 64 and secret_key(tmp_path) == first
    assert oct((tmp_path / ".secret").stat().st_mode)[-3:] == "600"
    monkeypatch.setenv("SECRET_KEY", "from-env")
    assert secret_key(tmp_path) == b"from-env"
