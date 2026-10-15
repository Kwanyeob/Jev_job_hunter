# Development status

**As of:** 2026-10-14 · **Branch:** `main` @ `d574dd3` (pushed) · **Tests:** 36 passing (`.venv/bin/python -m pytest -q`, or `uv run pytest -q`)

Jev AI Job Hunter is a coding-agent skill and CLI (`jjh`). Collectors gather job postings, and [Jev](https://docs.typesafe.ai) (TypeSafe System One) makes every decision about which links to open and how well a job fits. The host LLM never picks links or judges fit.

## What works

| Command | What it does | Transport | State |
|---|---|---|---|
| `jjh run --url <homepage> --query …` | Walks a company site to its careers page and the ATS. Jev scores each link and job. | Chrome over CDP (visible window) | Hunt logic unchanged since 2026-09-18 (`chrome.py` gained Windows paths and `ensure_chrome` on 2026-10-02); not re-run in this period |
| `jjh start` / `step` / `report` | The same hunt in slow motion, from fixtures or pasted page JSON | Fixtures / stdin | Covered by `tests/test_mock_flow.py` |
| `jjh linkedin` | `config/profile.yaml` → LinkedIn search → Jev triages titles → reads the top postings → Jev fit → `results/linkedin-<time>.xlsx` | **Plain HTTP** to LinkedIn guest endpoints (no Chrome, logged out) | Live-verified 2026-10-12 with `--mock` Jev |
| `jjh watch` | Polls the same search every 30–60 min (random), scores only unseen jobs, and keeps one `results/linkedin-watch.xlsx` | Plain HTTP | Live-verified 2026-10-14 with `--mock` Jev: `--once` twice, plus a 9-cycle loop |
| `jjh web` | Local page on `127.0.0.1:8765`: drop a resume PDF → Claude Fable 5.1 drafts the profile → live table → Excel download | Runs `jjh linkedin` underneath | Covered by `tests/test_web.py`; not run by hand since the HTTP switch |
| `scripts/li_compare.py` | Compares logged-out vs logged-in LinkedIn results by job ID | stdlib HTTP + DevTools snippet | Used once; see the evidence doc below |

## LinkedIn pipeline (the active work)

```
profile.yaml ──► search_queries ──► score_cards ──► Excel / state
                 (collect_cards,    (triage → read_posting → score_fit)
                  10 per request)
```

- **Requests:** 10 cards per guest search request (`seeMoreJobPostings/search?start=N`), then `jobPosting/{id}` for detail. `GuestClient` waits an irregular gap before every request (mean `--delay` 12 s; ×0.6–1.6, sometimes a 1–3× longer pause), keeps cookies, and on HTTP 429 backs off 60 s → 120 s. If LinkedIn keeps blocking, the run stops and saves what it has.
- **Per run:** `jjh linkedin` reads up to 30 cards per query (3 requests).
- **Per watch cycle:** page 1 per query, going deeper only while every card is unseen (max 3 pages). Unseen jobs go to Jev. Picked jobs that a block left unread stay unseen so the next cycle retries them. Quiet hours are 01–07. A blocked cycle doubles the next gap, capped at 4 h.
- **Output:** Excel sheets Matches / All listings / Profile, plus a `First seen` column. In watch mode the latest cycle's rows are bold.
- **Where things live:**
  - `state/linkedin-watch.json`: seen IDs, listings, matches, last 50 cycles. A different search gets `linkedin-watch-<key>.json`.
  - `results/`
  - Both directories are git-ignored.

## Evidence and decisions

- **Logged out sees the same jobs as logged in.** 22/26 logged-in page-1 results (85%) appear in the logged-out top 50, and only one miss is a distinct posting. Ranking differs (top-10 overlap 2/10), which Jev's re-scoring makes moot. → [`docs/evidence/2026-10-12-linkedin-guest-vs-login.md`](evidence/2026-10-12-linkedin-guest-vs-login.md). **Decision:** stay logged out, so no account is at risk.
- **Why HTTP over Chrome:** CDP-driven Chrome opening API URLs looks no more human, and HTTP needs no browser window. The original Chrome reader opened the guest API URLs directly anyway.
- **LinkedIn behavior learned:**
  - No `Accept` header → 429 on the first request.
  - About 6 requests in a few minutes → 429.
  - The logged-in search moved to `/jobs/search-results/`, drops `location` on redirect, and keeps job IDs only in `componentkey="job-card-component-ref-<id>"`.
- **Plan that led to `jjh watch`:** [`docs/plans/2026-10-12-linkedin-watch.md`](plans/2026-10-12-linkedin-watch.md) (done).

## Code map

| Module | Role |
|---|---|
| `linkedin.py` | `GuestClient`, guest HTML parsers (`parse_cards`, `parse_posting`), `collect_cards`, `search_queries`, `score_cards`, `run_linkedin` |
| `watch.py` | `WatchState`, `run_cycle`, scheduling (`next_run_at`, quiet hours, back-off), `watch` loop |
| `jev.py`, `questions.py` | Jev calls (`ask`; `--mock` for offline) and the questions and thresholds asked |
| `excel.py` | `write_xlsx` |
| `profile.py`, `resume.py` | Profile loading (personal info stripped before anything goes to Jev) and resume PDF → profile via Claude |
| `web.py`, `web/index.html` | `jjh web` server (stdlib + Server-Sent Events) |
| `chrome.py`, `runner.py`, `links.py`, `policy.py`, `filters.py`, `snapshot.py`, `extract.js` | The Chrome/CDP company-site hunt (`jjh run`) |
| `cli.py`, `board.py`, `store.py` | CLI, terminal output, run state and log |

## Not verified / known gaps

- **Real Jev has not been run in this period.** Every live run since 2026-10-12 used `--mock`, because there is no `.env` yet (copy `.env.example`, then set `TYPESAFE_API_KEY`).
- **There is no `config/profile.yaml` yet.** The live runs used `config/profile.example.yaml`.
- **Company apply URLs are missing logged out.** LinkedIn hides them, so most postings are scored from LinkedIn's own description rather than the ATS page.
- **The parsers depend on LinkedIn's HTML class names.** A markup change breaks them. The fixtures in `tests/fixtures/li_guest_*.html` show the expected shape.
- **LinkedIn's User Agreement prohibits automated collection.** Volume is kept low (about one request per query per watch cycle), but the risk is not zero.
- **`chrome.ensure_chrome` is now unused.** It was kept for possible `jjh run` use.
- **Ruff reports style warnings** under the global config, such as `re.S` aliases and import order. These match the existing code style.

## Next steps (not started)

1. Write `config/profile.yaml` from a resume, add `TYPESAFE_API_KEY`, and run `jjh linkedin` and `jjh watch --once` with real Jev.
2. Optionally:
   - Run `jjh watch` in the web UI
   - Add a macOS notification for strong matches
   - Add a launchd plist around `jjh watch --once`
   - Prune old listings from the watch state
