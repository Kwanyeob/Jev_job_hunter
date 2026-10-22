"""Who uses `jjh serve` and how much: one JSON line per event in data/usage.jsonl, summed by `jjh usage`.

Events: "login" (an invite link opened), "visit" (the app page loaded), "resume" (a resume sent to
Claude, ok or not), "run" (a search or read run, with its jobs found, Jev fits and LinkedIn requests),
"export" (an Excel download). The log only counts; it never holds profile or job content.
"""

from __future__ import annotations

import json
import threading
from datetime import datetime, timedelta
from pathlib import Path

FILE = "usage.jsonl"
RESUME_KRW = 50  # rough Claude cost of reading one resume (docs/STATUS.md)
_lock = threading.Lock()  # web requests and the scheduler thread append to the same file


def record(data_dir: Path, uid: str, kind: str, *, at: datetime | None = None, **fields) -> None:
    line = {"ts": (at or datetime.now()).isoformat(timespec="seconds"), "user": uid, "kind": kind, **fields}
    try:
        with _lock, (Path(data_dir) / FILE).open("a", encoding="utf-8") as f:
            f.write(json.dumps(line, ensure_ascii=False) + "\n")
    except OSError:
        pass  # usage is bookkeeping; never let it break a request or a run


def events(data_dir: Path, since: datetime | None = None) -> list[dict]:
    path = Path(data_dir) / FILE
    if not path.is_file():
        return []
    out = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        try:
            e = json.loads(raw)
        except json.JSONDecodeError:
            continue  # a line cut short by a crash
        if since is None or datetime.fromisoformat(e["ts"]) >= since:
            out.append(e)
    return out


def summarize(data_dir: Path, users, days: int | None = None, now: datetime | None = None) -> list[dict]:
    """One row per user (revoked ones too), most recently seen first."""
    since = (now or datetime.now()) - timedelta(days=days) if days else None
    rows = {u.id: {"name": u.name, "revoked": u.revoked, "last_seen": None, "logins": 0, "visits": 0,
                   "resumes": 0, "resume_fails": 0, "searches": 0, "reads": 0, "found": 0, "jev_triaged": 0,
                   "jev_fits": 0, "linkedin": 0, "errors": 0, "exports": 0} for u in users.all()}
    for e in events(data_dir, since):
        r = rows.get(e.get("user"))
        if r is None:
            continue  # a user since deleted
        kind = e.get("kind")
        if kind in ("login", "visit", "resume", "export"):
            r["last_seen"] = max(r["last_seen"] or "", e["ts"])
        if kind == "login":
            r["logins"] += 1
        elif kind == "visit":
            r["visits"] += 1
        elif kind == "resume":
            r["resumes" if e.get("ok") else "resume_fails"] += 1
        elif kind == "export":
            r["exports"] += 1
        elif kind == "run":
            r["searches" if e.get("run") == "search" else "reads"] += 1
            r["found"] += e.get("new") or 0
            r["linkedin"] += e.get("requests") or 0
            r["errors"] += bool(e.get("error"))
            if not e.get("mock"):  # mock runs never reach Jev
                r["jev_triaged"] += e.get("new") or 0
                r["jev_fits"] += e.get("scored") or 0
    for r in rows.values():
        r["claude_krw"] = r["resumes"] * RESUME_KRW  # failures are mostly non-PDFs that never reach Claude
    return sorted(rows.values(), key=lambda r: r["last_seen"] or "", reverse=True)


COLUMNS = [("name", "user"), ("last_seen", "last seen"), ("visits", "visits"), ("resumes", "resumes"),
           ("searches", "searches"), ("reads", "reads"), ("found", "jobs found"), ("jev_triaged", "Jev triaged"),
           ("jev_fits", "Jev fits"), ("linkedin", "LinkedIn req"), ("errors", "errors"), ("exports", "Excel"),
           ("claude_krw", "Claude KRW")]


def table(rows: list[dict]) -> str:
    def cell(r: dict, key: str) -> str:
        v = r[key]
        if key == "name":
            return v + (" (revoked)" if r["revoked"] else "")
        if key == "last_seen":
            return v[:16].replace("T", " ") if v else "-"
        if key == "resumes" and r["resume_fails"]:
            return f"{v} (+{r['resume_fails']} failed)"
        return str(v)

    grid = [[h for _, h in COLUMNS]] + [[cell(r, k) for k, _ in COLUMNS] for r in rows]
    widths = [max(len(row[i]) for row in grid) for i in range(len(COLUMNS))]
    return "\n".join("  " + "  ".join(c.ljust(w) if i < 2 else c.rjust(w) for i, (c, w) in enumerate(zip(row, widths)))
                     for row in grid)
