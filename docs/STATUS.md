# Development status

**As of:** 2026-10-16

| | |
|---|---|
| Branch | `friends-service`, 5 commits ahead of `main` (`7228920`). Not merged, not pushed. |
| Tests | 57 passing (`.venv/bin/python -m pytest -q`, or `uv run pytest -q`) |
| Keys in `.env` | `TYPESAFE_API_KEY` set; `ANTHROPIC_API_KEY` empty |

Jev AI Job Hunter is a coding-agent skill and CLI (`jjh`). Collectors gather job postings, and [Jev](https://docs.typesafe.ai) (TypeSafe System One) makes every decision about which links to open and how well a job fits. The host LLM never picks links or judges fit.

**Current goal:** a hosted, invite-only web service that 1–2 friends use for free from a browser. Phases 1–2 are done. Phase 3 (cloud deploy) is postponed. See [Friends service](#friends-service-jjh-serve).

## What works

| Command | What it does | Transport | Verified |
|---|---|---|---|
| `jjh serve` | Invite-only multi-user web service. Each user gets a profile (resume → AI draft), an automatic watch, cumulative results and Excel. | Starlette + uvicorn; LinkedIn over HTTP | Local server, real LinkedIn + **real Jev**, one full cycle (2026-10-16) |
| `jjh invite` / `revoke` / `users` | Operator commands: create or renew a link, block or delete a user, list users | — | Tests; `invite` used in the live run |
| `jjh linkedin` | `profile.yaml` → LinkedIn search → Jev triage → read top postings → Jev fit → `results/linkedin-<time>.xlsx` | **Plain HTTP** to LinkedIn guest endpoints (no Chrome, logged out) | Live 2026-10-12 with `--mock` Jev |
| `jjh watch` | The same search every 30–60 min (random). Only unseen jobs are scored, into one `results/linkedin-watch.xlsx`. | Plain HTTP | Live 2026-10-14 with `--mock` Jev: `--once` ×2 and a 9-cycle loop |
| `jjh web` | Local single-user page on `127.0.0.1:8765`: resume drop, live table, Excel | Runs `jjh linkedin` | Tests only since the HTTP switch |
| `jjh run --url … --query …` | Walks a company site to its careers page and the ATS. Jev scores links and jobs. | Chrome over CDP | Hunt logic unchanged since 2026-09-18; not re-run lately |
| `jjh start` / `step` / `report` | The company hunt in slow motion, from fixtures | Fixtures / stdin | `tests/test_mock_flow.py` |
| `scripts/li_compare.py` | Compares logged-out vs logged-in LinkedIn results | stdlib HTTP + DevTools snippet | Used once (evidence below) |

## Friends service (`jjh serve`)

Plan: [`docs/plans/2026-10-16-friends-service.md`](plans/2026-10-16-friends-service.md).

```
browser ──► jjh serve (one process)
            ├─ web app: /invite/<token> → signed cookie → own profile / watch / results / Excel
            └─ scheduler thread: every user's watch cycle, strictly one at a time
                 └─ run_cycle → GuestClient (LinkedIn) → Jev → Excel   (same code as `jjh watch`)
data/users.json                    users: id, name, SHA-256 of invite token, revoked flag
data/users/<id>/config/profile.yaml
data/users/<id>/state/linkedin-watch.json  + schedule.json
data/users/<id>/results/linkedin-watch.xlsx
```

**Login:**
- Each person gets one secret invite link. Only its hash is stored, and the link is shown once.
- Opening the link sets an HMAC-signed, HttpOnly, SameSite=Lax cookie that lasts 90 days.
- Re-inviting the same name invalidates the old link and its sessions.
- `revoke` blocks the user at once. `--delete` also removes their folder.

**Isolation:** each user can only reach `data/users/<id>/`. Tests confirm that A's cookie can't read B's results or Excel.

**Resume:**
- Claude **Sonnet 5.5** (`effort: medium`, refusal fallback on) drafts the profile, at about 50 KRW per resume.
- The PDF stays in memory and is never written to disk. Contact fields are stripped.
- Searches are capped at 3 per user.
- Without `ANTHROPIC_API_KEY` the upload box is hidden, and the user types the profile by hand.

**Scheduler:**
- Timing:
  - One cycle at a time across all users, with a random 1–3 min gap between cycles.
  - Each user's timing reuses `jjh watch`: 30–60 min gaps, quiet 01–07, and a blocked cycle doubles the next gap (capped at 4 h).
  - The schedule is persisted, so a restart resumes it.
- Searches:
  - "Search now" queues a one-off run.
  - A user's first cycle seeds a week of postings. Later cycles poll the last day.
- Reads:
  - **At most 5 postings are read per cycle.** The other jobs Jev rated worth reading wait in `pending` (best first, at most 30) and are read first in the next cycle, without re-triage.
  - Jobs a block left unread go back to that queue.

**UI** (`web/app.html`, Korean):
- Resume drop and profile form
- Watch switch with last run, next run, and how many jobs are waiting to be read
- "지금 한 번 찾기" (search now) button and live progress over SSE
- Results table, newest first: a "새로" (new) tag marks the latest cycle and green rows mean fit ≥ 70%
- Filter and Excel download

**Live run (2026-10-16, local, real Jev):**
- Flow checked end to end: invite login → profile save → search now → cycle → SSE events (30 cards, 10 fits, 1 cycle) → results → Excel (14 KB).
- Cycle: 30 cards, 10 read and scored, 17 LinkedIn requests in about 7 minutes.
- Three 429s happened during the posting reads. That is what led to the 5-reads-per-cycle cap.
- **The 5-read cap has not been re-run live yet.**

**Phase 3, postponed:**
- Dockerfile and `fly.toml` (one always-on machine, volume at `/data`, `TZ=Asia/Seoul`)
- Fly secrets
- `docs/OPERATIONS.md`
- After deploying, check whether LinkedIn blocks the datacenter IP
- Needs the operator to: sign up for Fly.io with billing (≈ $5/month), run `fly auth login`, and set `ANTHROPIC_API_KEY`.

## LinkedIn pipeline

```
profile ──► search_queries ──► triage (Jev) ──► worth_reading ──► read_cards ──► Excel / state
            (collect_cards,                     (best first;       (read_posting
             10 per request)                     watch queues       → score_fit)
                                                 the overflow)
```

- **Requests:**
  - 10 cards per guest search request, then `jobPosting/{id}` for detail.
  - `GuestClient` waits an irregular gap before each request: mean 12 s, ×0.6–1.6, and sometimes 1–3× longer.
  - It keeps cookies for the run.
  - On 429 it backs off 60 s, then 120 s. If LinkedIn keeps blocking, the run stops and saves what it has.
- **`jjh linkedin`:** up to 30 cards per query (3 requests), then reads the top `--max-open` (default 10).
- **Watch cycle:**
  - Searching: page 1 per query, going deeper only while every card is unseen (max 3 pages).
  - Scoring: only unseen cards are triaged, and up to `max_open` jobs are read from the pending queue plus the new cards.
  - Defaults: 10 reads for the `jjh watch` CLI, 5 in `jjh serve`.
- **Output:** Excel sheets Matches / All listings / Profile, with a `First seen` column. In watch mode the latest cycle's rows are bold.

## Evidence and decisions

- **Logged out sees the same jobs as logged in.** 85% overlap; ranking differs. → [`docs/evidence/2026-10-12-linkedin-guest-vs-login.md`](evidence/2026-10-12-linkedin-guest-vs-login.md). **Decision:** stay logged out, so no account is at risk.
- **HTTP over Chrome:** HTTP needs no browser window. Chrome opening API URLs looked no more human.
- **A hosted service rather than local installs**, because friends shouldn't need a terminal or keys.
  - Codex CLI or a personal ChatGPT login can't serve other people, which is why the operator's API key is used for resumes.
  - The cloud host is chosen over a home machine, accepting the LinkedIn-blocking risk, which is to be measured after deploy.
- **Sonnet 5.5 for resumes.** Haiku 5.5 would cost about 3 KRW per resume, but the user chose quality at about 50 KRW.
- **LinkedIn behavior learned:**
  - No `Accept` header → 429 on the first request.
  - About 6 rapid requests → 429.
  - 10 posting reads in one cycle → 3× 429 (even from a home IP).
  - The logged-in UI moved to `/jobs/search-results/`, drops `location` on redirect, and keeps job IDs only in `componentkey`.
- **Plans:** [`2026-10-12-linkedin-watch.md`](plans/2026-10-12-linkedin-watch.md) (done) and [`2026-10-16-friends-service.md`](plans/2026-10-16-friends-service.md) (phases 1–2 done).

## Code map

| Module | Role |
|---|---|
| `server.py`, `web/app.html` | `jjh serve` Starlette app and its Korean single-page UI |
| `users.py` | Invites, token hashes, signed sessions, `secret_key` (env `SECRET_KEY`, else generated into `data/.secret`) |
| `scheduler.py` | `Scheduler` (per-user schedule, one-at-a-time cycles, gaps, back-off) and `Hub` (per-user SSE event log) |
| `linkedin.py` | `GuestClient`, parsers, `collect_cards`, `search_queries`, `triage`, `worth_reading`, `report_triage`, `read_cards`, `score_cards`, `run_linkedin` |
| `watch.py` | `WatchState` (seen, listings, matches, cycles, pending), `run_cycle`, `next_run_at` and quiet hours, the `watch` CLI loop |
| `jev.py`, `questions.py` | Jev calls (`--mock` offline), questions and thresholds |
| `resume.py` | Resume PDF → profile (Claude Sonnet 5.5, structured output) |
| `profile.py`, `excel.py` | Profile loading (personal info stripped) and `write_xlsx` |
| `web.py`, `web/index.html` | Local single-user `jjh web` (stdlib) |
| `chrome.py`, `runner.py`, `links.py`, `policy.py`, `filters.py`, `snapshot.py`, `extract.js` | The Chrome/CDP company-site hunt (`jjh run`) |
| `cli.py`, `board.py`, `store.py` | CLI, terminal output, run state and log |

Environment for `jjh serve`:
- `TYPESAFE_API_KEY`, `ANTHROPIC_API_KEY`
- `SECRET_KEY` (optional locally)
- `PUBLIC_URL` (used in invite links)
- `JJH_DATA_DIR` (default `data/`)
- `HOST` / `PORT` (default 127.0.0.1:8000)

## Not verified / known gaps

- **The 5-reads-per-cycle cap and the pending queue are tested but not yet run live.**
- **Not deployed.** Whether LinkedIn blocks a cloud IP is unknown.
- **The resume upload has not been run live.** There is no `ANTHROPIC_API_KEY` yet; it is covered by tests with a fake client.
- **CLI `jjh linkedin` / `jjh watch` with real Jev not run;** only the server cycle used real Jev.
- **No `config/profile.yaml`** for the operator; the live runs used test profiles.
- **Real Jev scores run lower than mock scores.** The top was 43% for a test profile whose skills didn't match the query.
- **Company apply URLs are missing logged out,** so most postings are scored from LinkedIn's description.
- **The parsers depend on LinkedIn class names.** The fixtures in `tests/fixtures/li_guest_*.html` show the expected shape.
- **LinkedIn's User Agreement prohibits automated collection.** Volume is kept low, but the risk is not zero; a hosted service raises the stakes.
- **`chrome.ensure_chrome` is unused**, and Ruff style warnings match the existing code.

## Next steps

1. Decide whether to merge `friends-service` into `main`.
2. Run one live `jjh serve` cycle again to confirm the 5-read cap cuts 429s.
3. Add `ANTHROPIC_API_KEY` and try a real resume upload.
4. Phase 3 when ready: Fly.io account → Dockerfile / `fly.toml` / `docs/OPERATIONS.md` → deploy → LinkedIn block check → invite friends.
5. Later: email or other notification for strong matches, pruning old listings.
