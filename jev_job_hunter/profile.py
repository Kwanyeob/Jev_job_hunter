"""Candidate profile the host agent writes from a resume (config/profile.yaml)."""

from __future__ import annotations

from pathlib import Path

import yaml

DEFAULT_PROFILE = Path("config") / "profile.yaml"
# Never sent to Jev, even if the agent copied them from the resume.
PII_KEYS = {
    "name", "full_name", "email", "phone", "address", "birthday", "birth_date",
    "linkedin", "github", "website", "urls", "contact",
}
SEARCH_DEFAULTS = {"queries": [], "location": "", "remote": "any", "posted": "week"}


def load_profile(root: Path, path: str | None = None) -> dict:
    p = Path(path) if path else root / DEFAULT_PROFILE
    if not p.is_absolute() and not p.is_file():
        p = root / p
    if not p.is_file():
        raise SystemExit(
            f"ERROR: profile not found: {p}. Ask the agent to build it from your resume "
            "(skills/jev-job-hunter/SKILL.md → LinkedIn), or copy config/profile.example.yaml."
        )
    return normalize_profile(yaml.safe_load(p.read_text(encoding="utf-8")) or {}, str(p))


def _strs(v) -> list[str]:
    items = v.split(",") if isinstance(v, str) else (v or [])
    return [str(x).strip() for x in items if str(x).strip()]


def normalize_profile(data: dict, source: str) -> dict:
    """{candidate, search} → PII stripped, list fields cleaned, search defaults filled."""
    cand = {k: v for k, v in (data.get("candidate") or {}).items() if k not in PII_KEYS and v not in (None, "", [])}
    for k in ("skills", "target_roles", "interests", "locations", "languages"):
        if k in cand:
            cand[k] = _strs(cand[k])
    if not cand.get("target_roles"):
        raise SystemExit(f"ERROR: {source}: candidate.target_roles is empty")
    search = {**SEARCH_DEFAULTS, **{k: v for k, v in (data.get("search") or {}).items() if v is not None}}
    search["queries"] = _strs(search.get("queries")) or cand["target_roles"][:2]
    return {"candidate": cand, "search": search, "path": source}
