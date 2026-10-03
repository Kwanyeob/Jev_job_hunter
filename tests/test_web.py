"""Resume extraction (fake Claude client) and the local web server end to end (fake Chrome)."""

from __future__ import annotations

import json
import threading
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import pytest
from openpyxl import load_workbook

from jev_job_hunter import resume, web
from jev_job_hunter.linkedin import run_linkedin

from test_linkedin import FakeSession

PROFILE = {
    "candidate": {"headline": "Backend Engineer, 4 years Java", "summary": "Builds APIs.",
                  "years_experience": 4, "seniority": "mid", "skills": ["Java", "Python", "AWS", "PostgreSQL"],
                  "target_roles": ["Backend Engineer", "AI Engineer"], "interests": ["APIs"],
                  "locations": ["Seoul", "Remote"], "languages": ["English"], "education": ""},
    "search": {"queries": ["Backend Engineer"], "location": "Seoul", "remote": "any", "posted": "week"},
}


class FakeClient:
    def __init__(self, stop_reason="end_turn", text=json.dumps(PROFILE)):
        self.kw = None
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self._create))
        self._resp = SimpleNamespace(stop_reason=stop_reason,
                                     content=[SimpleNamespace(type="thinking"), SimpleNamespace(type="text", text=text)])

    def _create(self, **kw):
        self.kw = kw
        return self._resp


def test_extract_profile_request_shape():
    client = FakeClient()
    got = resume.extract_profile(b"%PDF-1.7 fake", client=client)
    assert got == PROFILE
    kw = client.kw
    assert kw["model"] == "claude-fable-5-1"
    assert kw["fallbacks"] == "default" and kw["betas"] == ["server-side-fallback-2026-07-01"]
    assert "thinking" not in kw
    schema = kw["output_config"]["format"]["schema"]
    fields = set(schema["properties"]["candidate"]["properties"])
    assert not fields & {"name", "email", "phone", "address"}
    doc, text = kw["messages"][0]["content"]
    assert doc["type"] == "document" and doc["source"]["media_type"] == "application/pdf"
    assert "Today is" in text["text"]


def test_extract_profile_errors():
    with pytest.raises(resume.ResumeError, match="not a PDF"):
        resume.extract_profile(b"hello", client=FakeClient())
    with pytest.raises(resume.ResumeError, match="declined"):
        resume.extract_profile(b"%PDF-1.7", client=FakeClient(stop_reason="refusal"))
    with pytest.raises(resume.ResumeError, match="cut off"):
        resume.extract_profile(b"%PDF-1.7", client=FakeClient(stop_reason="max_tokens"))


@pytest.fixture
def server(tmp_path, monkeypatch):
    (tmp_path / "config").mkdir()
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    monkeypatch.setattr(resume, "extract_profile", lambda pdf, client=None: json.loads(json.dumps(PROFILE)))

    def runner(root, **kw):
        return run_linkedin(root, delay=0, session=FakeSession(), out_dir=root / "results", **kw)

    hunt = web.Hunt()
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), web.make_handler(tmp_path, hunt, runner=runner))
    httpd.daemon_threads = True
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}", tmp_path, hunt
    httpd.shutdown()


def _post(url, body: bytes, ctype: str):
    req = urllib.request.Request(url, data=body, method="POST", headers={"Content-Type": ctype})
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read())


def test_web_flow(server):
    base, root, hunt = server
    with urllib.request.urlopen(base + "/", timeout=5) as r:
        assert b"Drop your resume PDF" in r.read()
    state = json.loads(urllib.request.urlopen(base + "/api/state", timeout=5).read())
    assert state["claude"] is True and state["jev"] is False and state["profile"] is None

    got = _post(base + "/api/resume", b"%PDF-1.7 fake", "application/pdf")
    assert got["profile"]["candidate"]["target_roles"] == ["Backend Engineer", "AI Engineer"]
    assert (root / "config/profile.yaml").is_file()

    started = _post(base + "/api/hunt", json.dumps({"profile": got["profile"], "max_open": 5}).encode(),
                    "application/json")
    assert started["mock"] is True  # no Jev key → forced mock

    kinds, data = [], {}
    with urllib.request.urlopen(base + "/api/events?since=0", timeout=10) as r:
        kind = ""
        for raw in r:
            line = raw.decode("utf-8").rstrip("\n")
            if line.startswith("event: "):
                kind = line[7:]
            elif line.startswith("data: "):
                kinds.append(kind)
                data.setdefault(kind, []).append(json.loads(line[6:]))
                if kind == "done":
                    break
    assert kinds.count("card") == 3
    assert {"id", "title", "company", "href", "posted", "posted_text"} <= set(data["card"][0])
    assert "triage" in kinds and kinds.count("fit") == 2
    assert Path(data["done"][0]["xlsx"]).is_file()

    with urllib.request.urlopen(base + "/api/export.xlsx", timeout=10) as r:
        assert "attachment" in r.headers["Content-Disposition"]
        xlsx = r.read()
    out = root / "export.xlsx"
    out.write_bytes(xlsx)
    wb = load_workbook(out)
    assert len(list(wb["Matches"].iter_rows(min_row=2))) == 2
    assert len(list(wb["All listings"].iter_rows(min_row=2))) == 3


def test_web_rejects_empty_profile(server):
    base, _, _ = server
    req = urllib.request.Request(base + "/api/hunt", data=json.dumps({"profile": {}}).encode(), method="POST",
                                 headers={"Content-Type": "application/json"})
    with pytest.raises(urllib.error.HTTPError) as e:
        urllib.request.urlopen(req, timeout=5)
    assert e.value.code == 400
