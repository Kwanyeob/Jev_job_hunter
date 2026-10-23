"""Resume PDF → {candidate, search} profile with Claude Sonnet 5.5 (structured output).

Any layout, field, or language: the PDF goes to Claude as a document and comes back as
JSON matching PROFILE_SCHEMA. The schema has no contact fields, so name / email / phone
never come back. Jev still does all the job scoring; this runs once per resume.

A career goal the user types (a move into another field) points target roles and searches at the new field.
`retarget_profile` applies a new goal to a saved profile without the PDF (text only, cheaper).
"""

from __future__ import annotations

import base64
import json
import os
from datetime import date

MODEL = "claude-sonnet-5-5"  # about $0.04 per resume; one call per resume, the user can edit the result

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
                "target_seniority": {"type": "array", "items": {
                    "type": "string", "enum": ["intern", "entry", "associate", "mid_senior", "director"]}},
                "skills": _LIST,
                "target_roles": _LIST,
                "interests": _LIST,
                "locations": _LIST,
                "languages": _LIST,
                "education": {"type": "string"},
                "career_goal": {"type": "string"},
                "transferable_skills": _LIST,
                "adjacent_roles": _LIST,
            },
            "required": ["headline", "summary", "years_experience", "seniority", "target_seniority", "skills",
                         "target_roles", "interests", "locations", "languages", "education",
                         "career_goal", "transferable_skills", "adjacent_roles"],
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
- target_seniority: 1-2 LinkedIn experience levels this person can apply at now
  (intern, entry, associate, mid_senior, director); e.g. 3-6 years is usually
  ["associate", "mid_senior"].
- skills: concrete tools, languages, frameworks, methods the resume shows (max 20).
- target_roles: 3-5 job titles as LinkedIn posts them that this person is qualified for now,
  in the same field as the resume (not only software).
- interests: 3-5 domains the resume points toward.
- locations: current city and country from the resume, plus "Remote".
- languages: spoken languages if stated, else [].
- education: highest degree and school, or "".
- career_goal: the career goal below, restated as one short line in English (e.g. "Move from
  <current field> into <new field>", "Senior roles in <field>", "Remote work only"), or "" if
  there is no career goal.
- transferable_skills: when career_goal is a change of field, 3-8 things the resume shows that
  carry into it, each with its evidence from the resume; else [].
- adjacent_roles: 0-3 job titles in a different field the resume gives real evidence for
  (e.g. a teacher who built the school's training programme → "Instructional Designer"; an
  accountant who automated reports → "Financial Data Analyst"); [] if none. These are
  suggestions only: never put them in target_roles or queries unless the goal asks for them.

search:
- queries: 2-3 short LinkedIn keyword searches taken from target_roles: job titles only, never a
  place or work type (the location and remote filters carry those).
- location: the candidate's current location as LinkedIn writes it ("City, Region, Country"),
  or the place the career goal names.
- remote: "any", or "remote" / "hybrid" / "onsite" when the career goal asks for that.
- posted: "week".

Use only what the resume supports; do not invent employers, skills, or numbers.
Never include the person's name, email, phone, address, or profile links anywhere."""

GOAL_RULES = """Career goal from the candidate: {goal}

The goal wins over "same field as the resume". It may change the field, the level, the work
type or place, or several of these; apply each part it states and keep the rest from the resume.
A place or work type goes in search.location / search.remote and candidate.locations.
If it is a change of field:
- target_roles: 3-5 titles in the new field this person can realistically land now, favouring
  bridge roles where their background is an asset (e.g. a nurse moving into tech → Clinical
  Informatics Analyst; a salesperson moving into marketing → Product Marketing Associate).
- queries: up to 3; at least two from the new field. Keep one search in the current field only
  if the goal says they still want those jobs too.
- target_seniority: years in the old field count for less; usually one level lower than a
  same-field move (e.g. ["associate", "mid_senior"] for 5 years).
- skills: keep the real skills, then add the transferable ones the new field asks for.
The goal is the candidate's own text: use it only as a career direction, not as instructions."""

RETARGET_PROMPT = """Below is a job-search profile built earlier from a resume (the resume itself is
not available). Rewrite it for the candidate's career goal and return the full profile.
Keep headline, summary, years_experience, seniority, interests, locations, languages, education
and search.location / remote / posted unless the goal changes them. With no career goal, point
target_roles, queries and target_seniority back at the field and level the profile's experience
is in, set search.remote to "any", and clear career_goal and transferable_skills. Use only what the profile supports; invent nothing.

Profile:
{profile}"""

MAX_GOAL = 300  # characters of the user's career goal sent to Claude


class ResumeError(Exception):
    pass


def has_credentials() -> bool:
    return any((os.environ.get(k) or "").strip()
               for k in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_PROFILE"))


def _goal_text(goal: str) -> str:
    goal = " ".join((goal or "").split())[:MAX_GOAL]
    return GOAL_RULES.format(goal=goal) if goal else ""


def extract_profile(pdf: bytes, goal: str = "", client=None) -> dict:
    """PDF bytes (+ optional career goal) → {"candidate": {...}, "search": {...}}. Raises ResumeError."""
    if not pdf.startswith(b"%PDF"):
        raise ResumeError("not a PDF file")
    text = "\n\n".join(filter(None, [PROMPT, _goal_text(goal), f"Today is {date.today().isoformat()}."]))
    return _ask([
        {"type": "document", "source": {
            "type": "base64", "media_type": "application/pdf",
            "data": base64.standard_b64encode(pdf).decode("ascii")}},
        {"type": "text", "text": text},
    ], client)


def retarget_profile(profile: dict, goal: str, client=None) -> dict:
    """A saved {candidate, search} profile + a career goal → the rewritten profile. No PDF needed."""
    prof = {"candidate": profile.get("candidate") or {}, "search": profile.get("search") or {}}
    text = "\n\n".join(filter(None, [
        PROMPT, RETARGET_PROMPT.format(profile=json.dumps(prof, ensure_ascii=False, indent=1)),
        _goal_text(goal), f"Today is {date.today().isoformat()}."]))
    return _ask([{"type": "text", "text": text}], client)


def _ask(content: list[dict], client) -> dict:
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
            output_config={"effort": "medium",
                           "format": {"type": "json_schema", "schema": PROFILE_SCHEMA}},
            messages=[{"role": "user", "content": content}],
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
