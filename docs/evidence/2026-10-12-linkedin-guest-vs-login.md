# LinkedIn search: logged out (guest API) vs logged in

**Date:** 2026-10-12 · **Tool:** [`scripts/li_compare.py`](../../scripts/li_compare.py) · **Raw data:** [`data/`](data/)

## Question

Does a logged-out LinkedIn job search return materially different postings than a logged-in one? This decides whether `jjh linkedin` can stay logged out (and move from Chrome to plain HTTP polling) without missing jobs.

## Verdict

**Same pool, different order.** 22 of 26 logged-in page-1 results (85%) appear in the logged-out top 50. Of the 4 misses, only 1 is a distinct posting. Ranking is heavily personalized (top-10 overlap 2/10), which matters little because Jev re-scores every card against the profile anyway.

Logged out is good enough for **coverage**. What logging in would add, but this test did not measure: company apply URLs, applicant counts, and recommendations that fall outside the search query.

## Setup

| | Logged out | Logged in |
|---|---|---|
| Source | `GET /jobs-guest/jobs/api/seeMoreJobPostings/search` (stdlib `urllib`, 10 cards/request) | `/jobs/search-results/` in the user's own Chrome session |
| Query | `keywords=AI Engineer`, `location=South Korea`, `f_TPR=r604800` (past week) | same, with location re-entered by hand (see findings) |
| Collected | top 50 (5 requests, ~10 s apart) | page 1: 24 cards + the job open in the detail pane |
| How | `li_compare.py guest` | DevTools snippet from `li_compare.py snippet` (reads the DOM only) |
| Time | 16:19:59 (local) | same afternoon, after the guest run |

## Results

```
guest 50 · login 26 · both 22
login results also in guest: 22/26 = 85%
guest results also in login: 22/50 = 44%   (expected: login page 1 holds only 25)
top-10 overlap: 2/10
top-25 overlap: 15/25
```

Full output: [`data/2026-10-12-compare.txt`](data/2026-10-12-compare.txt).

### The 4 logged-in-only results

| Job ID | Posting | Real difference? |
|---|---|---|
| 4460196723 | Job open in the detail pane; the snippet read its title as "How promoted jobs are ranked" | Unknown: not identified |
| 4468705795 | Harper: Backend Engineer, AI Agents at an AI Company That Just Raised $550M | No. Harper posts near-identical listings under new IDs, and the guest side has 2 other Harper variants (4472884178, 4468715344) |
| 4465498451 | Harper: Founding Engineer, AI Agent | No, same reason |
| 4450041924 | Toss: ML Engineer (CSS) | **Yes.** It is not in the guest top 50, though it may rank below 50 |

Logged-out results 27–50 are absent from logged-in page 1 by construction, since that page shows only 25 results.

## Incidental findings

1. **The guest API needs an `Accept` header.** Without one, `urllib` got HTTP 429 on its very first request while `curl` succeeded. Adding `Accept: */*` fixed it.
2. **The guest API rate-limits fast.** About 6 requests within a few minutes produced a 429 on the second page. With ~10 s between pages and no earlier burst, 5 pages went through cleanly. `li_compare.py` backs off 60 s and then 120 s on a 429.
3. **The logged-in UI moved to `/jobs/search-results/` and dropped the `location` parameter on redirect.** The URL kept only `keywords` and `f_TPR`, so the location had to be re-entered by hand. Any automation that builds logged-in URLs must handle this.
4. **The new logged-in cards have no job links or `data-job-id` attributes.** The ID is only in `componentkey="job-card-component-ref-<jobId>"`, and class names are obfuscated (`gflk21 …`).
5. **The snippet's promoted flag missed promoted cards.** It matched only capitalized "Promoted", while the UI shows lowercase text such as "How promoted jobs are ranked". This is why the run reports `promoted 0`, which is wrong. Fixed in the snippet after this run.

## Limits

- One query, one location, one time window, logged-in page 1 only, single run.
- The logged-in feed is personalized to one account; another profile could rank differently.
- Coverage only. The test did not compare posting detail (description, apply URL, applicant counts).

To strengthen the result: repeat with 1–2 more queries, and collect logged-in page 2 to compare top 50 against top 50.

## Reproduce

```bash
python3 scripts/li_compare.py guest --query "AI Engineer" --location "South Korea" --posted week
# logged in: open the printed URL, re-enter the location if it was dropped, scroll the list,
# localStorage.removeItem("jjhIds"), run `python3 scripts/li_compare.py snippet` in DevTools,
# save the clipboard to results/li-login.txt
python3 scripts/li_compare.py compare results/li-login.txt
```
