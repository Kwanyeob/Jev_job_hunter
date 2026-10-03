"""Resume PDF → {candidate, search} profile with Claude (structured output).

Any layout, field, or language: the PDF goes to Claude as a document and comes back as
JSON matching PROFILE_SCHEMA. The schema has no contact fields, so name / email / phone
never come back. Jev still does all the job scoring; this runs once per resume.
"""

from __future__ import annotations

import base64
import json
import os
from datetime import date

MODEL = "claude-fable-5-1"

_LIST = {"type": "array", "items": {"type": "string"}}
PROFILE_SCHEMA = {
    "type": "object",
    "properties": {
        "candidate": {
            "type": "object",
            "properties": {
                "headline": {"type": "string"},
                "summary": {"type": "string"},
                "years_experience": {"type": "number"},
                "seniority": {"type": "string", "enum": ["intern", "entry", "mid", "senior", "staff"]},
                "skills": _LIST,
                "target_roles": _LIST,
                "interests": _LIST,
                "locations": _LIST,
                "languages": _LIST,
                "education": {"type": "string"},
            },
            "required": ["headline", "summary", "years_experience", "seniority", "skills",
                         "target_roles", "interests", "locations", "languages", "education"],
            "additionalProperties": False,
        },
        "search": {
            "type": "object",
            "properties": {
                "queries": _LIST,
                "location": {"type": "string"},
                "remote": {"type": "string", "enum": ["any", "remote", "hybrid", "onsite"]},
                "posted": {"type": "string", "enum": ["day", "week", "month", "any"]},
            },
            "required": ["queries", "location", "remote", "posted"],
            "additionalProperties": False,
        },
    },
    "required": ["candidate", "search"],
    "additionalProperties": False,
}

PROMPT = """Build a job-search profile from the attached resume. It will be used to search
LinkedIn and to score each posting against the candidate.

candidate:
- headline: one line, current role family + years + 2-3 core technologies or domains.
- summary: 2-3 sentences of concrete experience (domains, systems built, measurable results).
- years_experience: total professional experience in years (one decimal), computed from the
  work-history dates; count overlapping jobs once, ongoing roles up to today.
- seniority: from years and scope of the most recent role.
- skills: concrete tools, languages, frameworks, methods the resume shows (max 20).
- target_roles: 3-5 job titles as LinkedIn posts them that this person is qualified for now,
  in the same field as the resume (not only software).
- interests: 3-5 domains the resume points toward.
- locations: current city and country from the resume, plus "Remote".
- languages: spoken languages if stated, else [].
- education: highest degree and school, or "".

search:
- queries: 2-3 short LinkedIn keyword searches taken from target_roles.
- location: the candidate's current location as LinkedIn writes it ("City, Region, Country").
- remote: "any". posted: "week".

Use only what the resume supports; do not invent employers, skills, or numbers.
Never include the person's name, email, phone, address, or profile links anywhere."""


class ResumeError(Exception):
    pass


def has_credentials() -> bool:
    return any((os.environ.get(k) or "").strip()
               for k in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_PROFILE"))


def extract_profile(pdf: bytes, client=None) -> dict:
    """PDF bytes → {"candidate": {...}, "search": {...}}. Raises ResumeError."""
    if not pdf.startswith(b"%PDF"):
        raise ResumeError("not a PDF file")
    try:
        import anthropic
    except ImportError as e:
        raise ResumeError("anthropic SDK not installed (uv sync)") from e
    client = client or anthropic.Anthropic()
    try:
        response = client.beta.messages.create(
            model=MODEL,
            max_tokens=16000,
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
            output_config={"format": {"type": "json_schema", "schema": PROFILE_SCHEMA}},
            messages=[{"role": "user", "content": [
                {"type": "document", "source": {
                    "type": "base64", "media_type": "application/pdf",
                    "data": base64.standard_b64encode(pdf).decode("ascii")}},
                {"type": "text", "text": f"{PROMPT}\n\nToday is {date.today().isoformat()}."},
            ]}],
        )
    except anthropic.AuthenticationError as e:
        raise ResumeError("Anthropic API key is missing or invalid (set ANTHROPIC_API_KEY in .env)") from e
    except anthropic.BadRequestError as e:
        raise ResumeError(f"Claude rejected the request: {e.message}") from e
    except anthropic.RateLimitError as e:
        raise ResumeError("Claude rate limit hit; try again in a minute") from e
    except anthropic.APIStatusError as e:
        raise ResumeError(f"Claude API error {e.status_code}: {e.message}") from e
    except anthropic.APIConnectionError as e:
        raise ResumeError("could not reach the Claude API") from e
    if response.stop_reason == "refusal":
        raise ResumeError("Claude declined to read this document")
    if response.stop_reason == "max_tokens":
        raise ResumeError("Claude's answer was cut off; try again")
    text = next((b.text for b in response.content if b.type == "text"), "")
    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        raise ResumeError("Claude returned invalid JSON") from e
