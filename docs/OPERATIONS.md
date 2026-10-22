# Operations (Fly.io)

Shortlist (`jjh serve`) runs on Fly.io as app **`shortlist-kj`**: one always-on machine in Toronto (`yyz`), with user data on the volume `shortlist_data` mounted at `/data`.

- URL: https://shortlist-kj.fly.dev
- Config: [`fly.toml`](../fly.toml) and [`Dockerfile`](../Dockerfile)
- Time zone: `America/Toronto`. Quiet hours (01–07) and every time a user sees follow it.

`flyctl` is at `~/.fly/bin/flyctl.exe` on the operator's Windows machine. In Git Bash, set `MSYS_NO_PATHCONV=1` first, or remote paths like `/data` get rewritten to Windows paths.

## Deploy

A push to `main` deploys by itself: [`.github/workflows/deploy.yml`](../.github/workflows/deploy.yml) runs the tests, and only when they pass runs `flyctl deploy`. Pushes that touch only docs or Markdown don't deploy. To redeploy without a change, use **Run workflow** on the repo's Actions tab.

The workflow uses the repo secret `FLY_API_TOKEN`, a deploy token for this app only, valid for a year:

```bash
fly tokens create deploy --app shortlist-kj --name github-actions --expiry 8760h
```

Paste it at GitHub → Settings → Secrets and variables → Actions → `FLY_API_TOKEN`. When it expires, deploys fail at the deploy step; make a new one the same way.

To deploy from this PC instead (it sends the working tree, uncommitted changes included):

```bash
fly deploy --app shortlist-kj --remote-only --ha=false
```

The image builds on Fly's remote builder. Dependencies come from `uv.lock`. A deploy restarts the machine, and each user's schedule resumes from `/data`.

## Secrets

Set once with `fly secrets set` and never stored in the repo:

| Name | What |
|---|---|
| `TYPESAFE_API_KEY` | Jev |
| `ANTHROPIC_API_KEY` | Resume reading (Sonnet 5.5) |
| `SECRET_KEY` | Signs login cookies. Changing it logs everyone out. |

`fly secrets list --app shortlist-kj` shows the names only.

## Users

```bash
fly ssh console --app shortlist-kj -C "jjh invite NAME"          # prints the private link; re-inviting replaces it
fly ssh console --app shortlist-kj -C "jjh users"
fly ssh console --app shortlist-kj -C "jjh revoke NAME"          # block now, keep data
fly ssh console --app shortlist-kj -C "jjh revoke NAME --delete" # block and delete their folder
```

## Usage

```bash
fly ssh console --app shortlist-kj -C "jjh usage"            # per user, since logging began
fly ssh console --app shortlist-kj -C "jjh usage --days 7"   # the last week only
```

One row per user: last seen, page visits, resumes read (Claude, about 50 KRW each), search and read runs, jobs found, jobs Jev triaged and scored (mock runs excluded), LinkedIn requests, failed runs, Excel downloads. It is summed from `/data/usage.jsonl`, one line per event, which holds counts only, never profile or job content. Logging started with this feature, so earlier use isn't counted. Each line in `/data/linkedin-requests.jsonl` now also names the user whose run sent it.

## Logs and checks

```bash
fly logs --app shortlist-kj                                       # live server output, including each search
fly ssh console --app shortlist-kj -C "tail -n 30 /data/linkedin-requests.jsonl"   # every LinkedIn request with its status
fly status --app shortlist-kj
```

Watch the request log for `429`s. A run of them means LinkedIn is pushing back on the datacenter IP.

## Backups

The volume takes a daily snapshot and keeps 5 (`fly volumes snapshots list shortlist_data --app shortlist-kj`). To copy the data out:

```bash
fly ssh console --app shortlist-kj -C "tar czf /tmp/backup.tgz -C /data users users.json"
fly ssh sftp get /tmp/backup.tgz backup.tgz --app shortlist-kj
```

## Cost

One `shared-cpu-1x` machine with 512 MB that never stops, plus a 1 GB volume. Check the current price on the Fly.io dashboard.
