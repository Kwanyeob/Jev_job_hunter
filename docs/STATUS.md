# Development status

**As of:** 2026-10-19

| | |
|---|---|
| Live | **https://shortlist-kj.fly.dev** on Fly.io (app `shortlist-kj`, Toronto `yyz`). Operations: [`OPERATIONS.md`](OPERATIONS.md). |
| Branch | `main` = `origin/main`. A push to `main` runs the tests on GitHub and, if they pass, deploys. |
| Users on the server | `kwanyeob` (operator; data moved from the local PC), `Andrew` (invited 2026-10-19) |
| Tests | 70. All pass on GitHub (Linux). On Windows, `test_secret_key_env_wins_else_generated_once` fails because `chmod 600` has no effect there. |
| Keys | `TYPESAFE_API_KEY`, `ANTHROPIC_API_KEY`, `SECRET_KEY` as Fly secrets. Locally in `.env`. |

Shortlist (formerly JevScout / Jev AI Job Hunter) is a hosted, invite-only web service. From one resume it builds a search profile, polls LinkedIn on the user's schedule, saves every new posting the moment it is found, and has [Jev](https://docs.typesafe.ai) score how well each one fits. Jev makes every decision about which postings to open and how well a job fits; the host LLM never does. The CLI (`jjh`) and the coding-agent skill still work for local use.

The local server is **off**. The `data/` folder on the PC is an old copy; the live data is on the Fly volume. Running `jjh serve` locally would double the LinkedIn requests, so don't.

## What works

| Command | What it does | Verified |
|---|---|---|
| `jjh serve` | The service: invite login, per-user profile, auto search, results, Excel | Live on Fly.io since 2026-10-19 |
| `jjh invite` / `revoke` / `users` | Operator commands; on the server run them with `fly ssh console -C "…"` | Used on the server |
| `jjh usage [--days N]` | Per-user activity and API use from `/data/usage.jsonl` | Tests; not yet on the server |
| `jjh linkedin` | `profile.yaml` → LinkedIn search → Jev triage → read top postings → Jev fit → Excel | Live 2026-10-12 with `--mock` Jev |
| `jjh watch` | The same search every 30–60 min, unseen jobs only, one cumulative Excel | Live 2026-10-14 with `--mock` Jev |
| `jjh web` | Local single-user page on `127.0.0.1:8765` | Tests only |
| `jjh run --url … --query …` | Company site → careers page → ATS, over Chrome/CDP | Not re-run lately |
| `jjh start` / `step` / `report` | The company hunt in slow motion, from fixtures | `tests/test_mock_flow.py` |

## The service (Shortlist)

```
browser ──HTTPS──► Fly.io machine (always on, TZ America/Toronto)
                   ├─ web app: /invite/<token> → signed cookie → own profile / jobs / Excel
                   └─ scheduler thread: every user's runs, strictly one at a time
                        └─ run_cycle → GuestClient (LinkedIn) → Jev → Excel
/data/users.json                         users: id, name, SHA-256 of invite token, revoked flag
/data/users/<id>/config/profile.yaml
/data/users/<id>/state/linkedin-watch.json, schedule.json, dismissed.json
/data/users/<id>/results/linkedin-watch.xlsx (+ -scored.xlsx)
/data/linkedin-requests.jsonl            every LinkedIn request, all users, with its status and user
/data/usage.jsonl                        logins, visits, resumes, runs, Excel downloads per user (counts only)
```

**Login:** one secret invite link per person (only its hash is stored, shown once). Opening it sets an HMAC-signed, HttpOnly cookie for 90 days. Re-inviting a name replaces the link and logs out old sessions. Users can only reach their own folder.

**Resume:** Claude Sonnet 5.5 drafts the profile (about 50 KRW per upload). The PDF stays in memory; contact fields are stripped. Up to 3 search terms. Re-uploading refills the profile and saves it at once; jobs and scores stay.

**Scheduling:**
- Search runs every 30–60 min by default (the user can pick up to 6–8 h). Quiet 01:00–07:00 Toronto time.
- Read runs: 5 postings every 8–14 min while jobs wait in the scoring queue (max 30, best first; the lowest falls off).
- One run at a time across all users, 1–3 min after a search and 4–6 min after a read run.
- "Search now" rests 10 min after a search. A blocked run doubles the next gap (cap 4 h).
- Jobs are saved the moment they are found. Knocked-out jobs (×, with undo) never come back.

**Reposts:** a job with the same company, title and place as one already seen is skipped even under a new LinkedIn job ID (recruiters repost). Saved duplicates are dropped on load unless Jev already scored them. Different agencies posting the same requisition under different titles are not merged.

**Excel:** the download button opens a window with two choices:
- *Only jobs Jev scored* → `shortlist-scored-<date>.xlsx` (Matches + Profile).
- *All jobs* → `shortlist-<date>.xlsx`, opening on **All listings** (every job, with Jev's fit, scored first), then Matches and Profile.

**UI** (`web/app.html`, Korean/English): jobs print like a receipt tape (one dated block per search, latest in red), scores right-aligned (70%+ dark, hover for the per-part breakdown), logos in colour, filter and views (All / Scored / Strong / To score), scoring-queue panel, Excel window.

**Guides:**
- They open by themselves **only for new users** (no profile yet): the welcome guide, then after the first resume one guide that walks the profile and moves on to Jobs found, explained over **sample rows** (drawn only, never saved, tagged "sample", gone when the guide ends).
- Anyone can open them with **?**: with a profile it is one guide, Jobs found first and then My profile; the re-upload screen has its own 2-step guide.
- Profile steps: AI-filled profile, re-upload, auto search, random timing, search now. (Target level and search terms were dropped to keep it short.)
- A "2 / 12" counter replaces the dots, which pushed Next out of the card on long guides. All guides were checked at 1366/1024/768/390 px in both languages with no overflow or overlap.

**Career direction** (branch `career-direction`, 2026-10-22):
- An optional "where you want to go" line on the upload screen (`POST /api/resume?goal=`, 300 chars). Free text: another field, a level, remote only, a place, or a mix. Claude applies each part it states and keeps the rest from the resume.
- The profile stores it as `career_goal`, plus `transferable_skills` (on a change of field, with evidence) and `adjacent_roles` (0-3 other-field roles the resume supports; shown as suggestion chips, never searched unless picked).
- `POST /api/goal` redoes a saved profile for a new direction without the PDF; `""` goes back to the resume's field. Logged as a `resume` usage event with `retarget`.
- Jev's `skills_match`, `seniority_match` and `overall_fit` questions count `transferable_skills` and discount old-field years when `career_goal` is a change of field. Profiles without a goal score as before.
- Form saves keep the three AI-only fields. Prompt examples span several fields so no one move biases every user.
- Checked live: senior-only, remote-only, vague ("pays more"), two directions, licensed field (→ bridge roles), prompt injection (ignored), email in the text (not stored). Open: a direction set later sometimes picks one level lower (entry) than the same direction at upload.

## Deploy and operations

- `Dockerfile` (python 3.14-slim + tzdata, `uv sync --frozen`), `fly.toml` (1 machine, never auto-stopped, `shared-cpu-1x` 512 MB, volume `shortlist_data` at `/data`, daily snapshots kept 5).
- `.github/workflows/deploy.yml`: push to `main` → pytest on Linux → `flyctl deploy`. Docs/Markdown-only pushes don't deploy. Uses the repo secret `FLY_API_TOKEN` (deploy token `github-actions-3`, expires 2027-10-20).
- `flyctl` on the operator PC: `~/.fly/bin/flyctl.exe`. In Git Bash set `MSYS_NO_PATHCONV=1`.
- Invites, logs, request log, backups: [`OPERATIONS.md`](OPERATIONS.md).

**Cost:** Fly charges for the always-on machine and the volume; traffic for a few users is negligible, so more users barely change it. What grows with users is Anthropic (per resume upload) and Jev (per scored job).

## LinkedIn from the cloud

- First request from the Fly IP (2026-10-19): one 429, then fine after the 55 s back-off.
- First ~30 min of real runs on the server: 17 requests, 1 × 429, recovered; 10 posting reads in a row after it were all 200.
- Reading: not blocked, but the datacenter IP draws a little more pushback than the home IP.
- For 2–3 users the expected load is about 100 requests/hour, one at a time with ~12 s gaps; more users stretch each user's gaps rather than bunching requests.
- **To do:** after a day or two with Andrew on, check `/data/linkedin-requests.jsonl`. If 429s pass ~10% or come in runs, lengthen the search gaps or read 3 postings per run instead of 5.

## LinkedIn pipeline

```
profile ──► search_queries ──► triage (Jev) ──► worth_reading ──► read_cards ──► Excel / state
            (collect_cards,                     (best first;       (read_posting
             10 per request,                     queue the          → score_fit)
             reposts skipped)                    overflow)
```

- 10 cards per guest search request, then `jobPosting/{id}` for detail. `GuestClient` waits an irregular gap before each request (mean 12 s, ×0.6–1.6, sometimes 1–3× longer) and keeps cookies for the run.
- On 429 it backs off 60 s, then 120 s; if LinkedIn keeps blocking, the run stops and keeps what it has.
- Watch search: page 1 per query, deeper only while every card is unseen (max 3 pages). Only unseen cards are triaged.

## Evidence and decisions

- **Logged out sees the same jobs as logged in** (85% overlap). → [`evidence/2026-10-12-linkedin-guest-vs-login.md`](evidence/2026-10-12-linkedin-guest-vs-login.md). Stay logged out, so no account is at risk.
- **HTTP over Chrome** for LinkedIn: no browser window, and Chrome looked no more human.
- **Hosted service** rather than local installs; friends need no terminal or keys. The operator's API keys serve everyone.
- **Fly.io in Toronto** because the searches are Toronto jobs; accepting the datacenter-IP risk, now being measured.
- **Deploy from GitHub** rather than the PC, so only committed, tested code goes live.
- **Sonnet 5.5 for resumes** (quality at ~50 KRW) over Haiku 5.5 (~3 KRW).
- **LinkedIn behaviour learned:** no `Accept` header → 429 at once; ~6 rapid requests → 429; 10 posting reads in one cycle → 3 × 429 even from a home IP (hence 5 per run); the logged-in UI moved to `/jobs/search-results/`.
- **Address:** free Fly addresses are always `<app>.fly.dev`; a name without it needs a bought domain (about $10–15/year) plus one DNS record. Not done.
- **Plans:** [`plans/2026-10-12-linkedin-watch.md`](plans/2026-10-12-linkedin-watch.md) (done), [`plans/2026-10-16-friends-service.md`](plans/2026-10-16-friends-service.md) (all three phases done).

## Code map

| Module | Role |
|---|---|
| `server.py`, `web/app.html` | `jjh serve` Starlette app and its single-page UI (guides, sample rows, Excel window) |
| `users.py` | Invites, token hashes, signed sessions, `secret_key` |
| `scheduler.py` | Per-user schedule, search and read runs one at a time, gaps, back-off; `Hub` for SSE |
| `linkedin.py` | `GuestClient`, parsers, `repost_key`, `collect_cards`, `triage`, `read_cards`, `run_linkedin` |
| `watch.py` | `WatchState` (seen, listings, matches, cycles, pending; `drop_reposts`, `known`), `run_cycle`, `write_state_xlsx` |
| `excel.py` | `write_xlsx` (Matches / All listings / Profile; listings optional or first) |
| `jev.py`, `questions.py` | Jev calls (`--mock` offline), questions and thresholds |
| `resume.py`, `profile.py` | Resume PDF → profile; profile loading |
| `web.py`, `web/index.html` | Local single-user `jjh web` |
| `chrome.py`, `runner.py`, … | The Chrome/CDP company-site hunt (`jjh run`) |
| `cli.py`, `board.py`, `store.py` | CLI, terminal output, run state and log |

## Not verified / known gaps

- **LinkedIn from the cloud over days with several users** (see above).
- **Company apply URLs are missing logged out**, so most postings are scored from LinkedIn's description.
- **Parsers depend on LinkedIn class names** (`tests/fixtures/li_guest_*.html` show the expected shape).
- **LinkedIn's User Agreement prohibits automated collection.** Volume is kept low, but the risk is not zero, and a hosted service raises the stakes.
- `/data/linkedin-requests.jsonl` has a stray line that isn't valid JSON (read it with `grep -a`, not `json.loads` over every line).

## Next steps

1. After a day or two, check the server's LinkedIn request log for 429s (with Andrew active).
2. Invite more friends when that looks fine: `fly ssh console --app shortlist-kj -C "jjh invite NAME"`.
3. Optional: a custom domain instead of `shortlist-kj.fly.dev`.
4. Later: notify users of strong matches (email or other), prune old listings.
