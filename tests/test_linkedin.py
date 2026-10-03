"""LinkedIn flow with a fake Chrome session: cards → Jev triage → ATS page → Jev fit → xlsx."""

from __future__ import annotations

import io
import json
from contextlib import redirect_stdout
from pathlib import Path

import pytest
from openpyxl import load_workbook

from jev_job_hunter import linkedin
from jev_job_hunter.linkedin import CARDS_JS, VIEW_JS, apply_target, is_authwall, run_linkedin, search_url
from jev_job_hunter.profile import load_profile

ROOT = Path(__file__).resolve().parents[1]
JD = (
    "We are hiring a Backend Engineer to build Python and Go services on AWS with PostgreSQL "
    "and Kafka. 3+ years of experience. Seoul or remote. " * 8
)
CARDS = [
    {"id": "4001", "href": "https://www.linkedin.com/jobs/view/backend-engineer-4001",
     "title": "Backend Engineer", "company": "Toss", "location": "Seoul, South Korea", "posted": "2026-09-25", "posted_text": "3 days ago"},
    {"id": "4002", "href": "https://www.linkedin.com/jobs/view/ai-engineer-4002",
     "title": "AI Engineer, LLM Platform", "company": "Upstage", "location": "Seoul", "posted": "2026-09-24", "posted_text": "4 days ago"},
    {"id": "4003", "href": "https://www.linkedin.com/jobs/view/sales-4003",
     "title": "Account Executive", "company": "Acme", "location": "Seoul", "posted": "2026-09-24", "posted_text": "4 days ago"},
]
VIEWS = {
    CARDS[0]["href"]: {"apply": '"https://www.linkedin.com/jobs/view/externalApply/4001?url=https%3A%2F%2Fboards.greenhouse.io%2Ftoss%2Fjobs%2F1&amp;urlHash=x"',
                       "text": "LinkedIn copy of the description."},
    CARDS[1]["href"]: {"apply": "", "text": JD},  # Easy Apply only → LinkedIn text
}
ATS = {"https://boards.greenhouse.io/toss/jobs/1": JD}


class FakeSession:
    def __init__(self, wall_on: str = ""):
        self.url = ""
        self.visited: list[str] = []
        self.wall_on = wall_on

    def navigate(self, url: str) -> int:
        self.url = url
        self.visited.append(url)
        return 1

    def wait_ready(self, cap: float = 0, interval: float = 0):
        return []

    def scroll_to_bottom(self) -> dict:
        return {}

    def click_text(self, text: str, stay_url: str | None = None) -> bool:
        return False

    def eval_value(self, expression: str, timeout=None):
        if expression == "(" + CARDS_JS + ")()":
            return {"url": self.url, "cards": [dict(c) for c in CARDS]}
        if expression == "(" + VIEW_JS + ")()":
            if self.wall_on and self.url == self.wall_on:
                return {"url": "https://www.linkedin.com/authwall?trk=x"}
            return {"url": self.url, **VIEWS.get(self.url, {"apply": "", "text": ""})}
        raise AssertionError("unexpected JS")

    def extract(self, js: str, need_text: bool = True) -> dict:
        return {"url": self.url, "text": ATS.get(self.url, ""), "links": []}


@pytest.fixture
def profile(tmp_path):
    src = (ROOT / "config/profile.example.yaml").read_text(encoding="utf-8")
    p = tmp_path / "profile.yaml"
    p.write_text(src.replace("candidate:\n", "candidate:\n  name: Jane Doe\n  email: jane@example.com\n", 1),
                 encoding="utf-8")
    return p


def _hunt(tmp_path, profile, session) -> tuple[Path, str]:
    buf = io.StringIO()
    with redirect_stdout(buf):
        path = run_linkedin(ROOT, profile_path=str(profile), delay=0, mock=True,
                            out_dir=tmp_path / "out", session=session)
    return path, buf.getvalue()


def test_profile_strips_pii_and_defaults(profile):
    prof = load_profile(ROOT, str(profile))
    assert "name" not in prof["candidate"] and "email" not in prof["candidate"]
    assert prof["search"]["queries"] == ["Backend Engineer", "AI Engineer"]
    assert prof["search"]["posted"] == "week"


def test_search_url_and_apply_target():
    url = search_url("AI Engineer", "South Korea", "week", "remote")
    assert url.startswith("https://www.linkedin.com/jobs/search/?keywords=AI+Engineer")
    assert "f_TPR=r604800" in url and "f_WT=2" in url and "location=South+Korea" in url
    assert "f_TPR" not in search_url("x", posted="any")
    raw = VIEWS[CARDS[0]["href"]]["apply"]
    assert apply_target(raw) == "https://boards.greenhouse.io/toss/jobs/1"
    assert apply_target("https://jobs.lever.co/a/b") == "https://jobs.lever.co/a/b"
    assert apply_target("https://www.linkedin.com/jobs/view/4001") == ""
    assert apply_target("") == ""
    assert is_authwall("https://www.linkedin.com/authwall?trk=1")
    assert not is_authwall("https://www.linkedin.com/jobs/view/4001")


def test_linkedin_flow_writes_excel(tmp_path, profile):
    session = FakeSession()
    path, out = _hunt(tmp_path, profile, session)
    assert out.strip().endswith("ACTION END")
    assert "STAGE triage" in out and "Overall fit" in out
    # both queries run, cards deduped, sales card not opened, ATS followed
    assert sum(1 for u in session.visited if "/jobs/search/" in u) == 2
    assert "https://boards.greenhouse.io/toss/jobs/1" in session.visited
    assert CARDS[2]["href"] not in session.visited

    wb = load_workbook(path)
    assert wb.sheetnames == ["Matches", "All listings", "Profile"]
    rows = list(wb["Matches"].iter_rows(min_row=2, values_only=True))
    assert len(rows) == 2
    fits = [r[1] for r in rows]
    assert fits == sorted(fits, reverse=True)
    by_title = {r[2]: r for r in rows}
    head = [c.value for c in wb["Matches"][1]]
    assert by_title["Backend Engineer"][head.index("Read from")] == "ATS"
    assert by_title["AI Engineer, LLM Platform"][head.index("Read from")] == "LinkedIn"
    ats_row = 2 + [r[2] for r in rows].index("Backend Engineer")
    apply_col = head.index("Apply / ATS") + 1
    assert wb["Matches"].cell(row=ats_row, column=apply_col).hyperlink.target == "https://boards.greenhouse.io/toss/jobs/1"
    assert by_title["Backend Engineer"][head.index("Job ID")] == "4001"
    assert len(list(wb["All listings"].iter_rows(min_row=2))) == 3
    prof_rows = {r[0]: r[1] for r in wb["Profile"].iter_rows(values_only=True)}
    assert "candidate.email" not in prof_rows and "candidate.name" not in prof_rows

    data = json.loads(path.with_suffix(".json").read_text(encoding="utf-8"))
    assert len(data["matches"]) == 2


def test_authwall_stops_and_still_writes(tmp_path, profile, monkeypatch):
    monkeypatch.setattr(linkedin, "LINKEDIN_OPEN_THRESHOLD", 0.0)
    session = FakeSession(wall_on=CARDS[2]["href"])  # lowest-ranked card hits the wall last
    path, out = _hunt(tmp_path, profile, session)
    assert "login wall" in out
    assert path is not None and path.is_file()
    rows = list(load_workbook(path)["Matches"].iter_rows(min_row=2, values_only=True))
    assert len(rows) == 2
