# github-bkp-system

Self-hosted service that keeps daily-updated mirrors of chosen GitHub repositories
and emails you when a repository disappears from GitHub (or comes back).

## What it does

- Keeps a bare `git` mirror (all branches and tags) of each repo under `/data/repos/<owner>/<repo>.git`.
- Updates every repo once a day (`SYNC_CRON`, default 03:00). The mirror update applies the
  already-fetched refs locally (no second network fetch).
- Never loses history: refs that would vanish (force-push, deleted branch, moved tag) are copied to
  `refs/backup/<date>/...` before updating.
- Emails you when a repo returns 404 on two consecutive checks, when it comes back,
  when a backup fails 3 days in a row, plus a weekly summary.
- Simple web UI to add/remove repos, trigger a sync and see past runs.

## Setup

1. Create a fine-grained GitHub token with **Contents: Read-only** and **Metadata: Read-only**
   for the repos you want to back up.
2. `cp .env.example .env` and fill in the values.
3. Start it:

   ```bash
   docker compose up -d --build
   ```

4. Open http://127.0.0.1:8808 and add repos as `owner/repo` or a GitHub URL.

## Schedule

`SYNC_CRON` and `SUMMARY_CRON` use standard cron syntax (day-of-week `0` or `7` = Sunday),
evaluated in the `TZ` time zone.

If the service was down at the scheduled time, it runs a catch-up backup on startup
when the last run is older than 23 hours.

## Security

The web UI has **no authentication**. The compose file binds it to `127.0.0.1` only.
To reach it from other machines, change the port binding to a LAN or VPN address
(e.g. your Tailscale IP) — never expose it to the internet.

Two guards protect the UI from browser-based attacks: state-changing requests carrying a
cross-site `Origin` / `Sec-Fetch-Site` header are rejected (403), and only the hostnames in
`ALLOWED_HOSTS` (default `127.0.0.1,localhost`) are served, which blocks DNS rebinding. When you
bind to a LAN/VPN address, add it to `ALLOWED_HOSTS`, and **keep `127.0.0.1`** in the list: the
container healthcheck calls `http://127.0.0.1:8080/health`.

The GitHub token is passed to git through environment config on each command and is never
written to the mirrors' `config` files.

## Data

Everything lives in the `bkp-data` volume mounted at `/data`:

- `/data/state.db` — SQLite with repos, runs and events
- `/data/repos/<owner>/<repo>.git` — mirrors (never deleted by the service)

If you use a bind mount instead of the named volume, make the host directory writable by UID 1000.

## Restore

```bash
# working copy
git clone /data/repos/owner/repo.git repo
# republish to a new GitHub repo
git -C /data/repos/owner/repo.git push --mirror https://github.com/owner/new-repo.git
# note: --mirror also pushes refs/backup/*; to publish only branches and tags use:
git -C /data/repos/owner/repo.git push https://github.com/owner/new-repo.git 'refs/heads/*:refs/heads/*' 'refs/tags/*:refs/tags/*'
# list preserved refs
git -C /data/repos/owner/repo.git for-each-ref refs/backup
```

From the host, run these through the container:
`docker compose exec github-bkp git -C /data/repos/owner/repo.git for-each-ref refs/backup`.

## Development

```bash
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
python -m pytest
ruff check .
```
