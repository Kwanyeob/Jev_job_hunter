# Jev AI Job Hunter

Demo MVP: a coding-agent **skill** that hunts AI/software jobs on real company sites.

**Chrome sees and acts. Jev decides.** The host CLI runs one command. `jjh` drives Chrome over CDP and calls [Jev](https://docs.typesafe.ai) (TypeSafe System One) to score every link and job. The host LLM never picks what to click.

Skill: [`skills/jev-job-hunter`](skills/jev-job-hunter/SKILL.md).

<video src="assets/jev_job.mp4" controls playsinline preload="metadata" width="100%">
  <a href="assets/jev_job.mp4">Watch the demo</a>
</video>

## Setup

```bash
cp .env.example .env   # then set TYPESAFE_API_KEY (optional for --mock)
uv sync
```

Visible Chrome (not headless). Chrome DevTools MCP is optional; `jjh run` talks to Chrome over CDP directly.

## One-command hunt

```bash
uv run jjh run --url https://openai.com --query "Applied AI Engineer, Codex"
uv run jjh run --url https://typesafe.ai --query "backend / AI engineer"
uv run jjh log
```

Any company homepage works. `--companies openai` is only a shortcut. Add `--mock` with no API key, `--max-pages N`, `--tab reuse`. Tests: `uv run pytest -q`.

## LinkedIn from your resume → Excel

**Web:** `uv run jjh web` opens http://127.0.0.1:8765. Drop a resume PDF (Claude Fable 5.1 reads it once into an editable profile; needs `ANTHROPIC_API_KEY`), press **Start hunt**, and watch LinkedIn cards (title, company, location, updated, job ID, link) stream into the table as Jev scores them. **Download Excel** works mid-run.

**CLI:** give the agent your resume, it writes `config/profile.yaml` (shape: [`config/profile.example.yaml`](config/profile.example.yaml), no contact info, git-ignored), then:

```bash
uv run jjh linkedin                       # queries / location from profile.yaml
uv run jjh linkedin --query "AI Engineer" --location "Seoul" --posted week --max-open 10
```

LinkedIn public search over plain HTTP (logged out, no Chrome; 10 cards per request with irregular ~12 s pauses, `--delay` to change; 60 s → 120 s back-off on 429) → Jev triages card titles → the full posting from LinkedIn's guest endpoint (description + seniority / employment criteria; a company ATS page when LinkedIn exposes the apply URL, which it no longer does logged out) → Jev fit vs your profile → `results/linkedin-<time>.xlsx`. A login wall skips one posting and cools down; two in a row, or a 429 that outlasts the back-off, stop the hunt and still write what was scored. Evidence that logged out sees the same jobs: [`docs/evidence/`](docs/evidence/2026-10-12-linkedin-guest-vs-login.md).

### Keep watching

```bash
uv run jjh watch                          # every 30–60 min (random), quiet 01–07, Ctrl+C to stop
uv run jjh watch --every 20-40 --quiet 0-8
uv run jjh watch --once                   # one cycle, for cron / launchd
```

Each cycle reads page 1 per query (deeper only while every card is new, up to 3 pages), and only jobs never seen before go to Jev. Seen job IDs live in `state/linkedin-watch.json`. Everything goes into one `results/linkedin-watch.xlsx` with a **First seen** column, and the latest cycle's rows are bold. When LinkedIn blocks a cycle, the next gap doubles (capped at 4 h). Picked jobs that were never read are retried next time. Same flags as `jjh linkedin` (except `--max-cards`); `--posted` defaults to `day`.

## Slow-motion (fixtures / narrated demos)

```bash
uv run jjh start --mock
uv run jjh step --page tests/fixtures/openai_home.page.json --mock
uv run jjh step --page tests/fixtures/openai_careers.page.json --mock
uv run jjh step --page tests/fixtures/openai_job.page.json --mock
uv run jjh report
```

If CDP is down: `evaluate_script` (no `filePath`) then `uv run jjh step --page - <<'EOF' ... EOF`.
