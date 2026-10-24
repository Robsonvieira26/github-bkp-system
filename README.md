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
3. Create the data volume (once per server; see [Data](#data)):

   ```bash
   docker volume create github-bkp-data
   ```

4. Start it:

   ```bash
   docker compose up -d --build
   ```

5. Open http://127.0.0.1:8808 and add repos as `owner/repo` or a GitHub URL.

## Updating

```bash
git pull
docker compose up -d --build
```

Rebuilding the image, restarting, `docker compose down` and even `docker compose down -v` keep
all backups: they live in the external `github-bkp-data` volume, which compose never removes.

## API

Add repositories from a script with `POST /api/repos`. It accepts the same formats as the form
(`owner/repo`, HTTPS or SSH GitHub URL), 1–100 per request:

```bash
curl -X POST http://127.0.0.1:8808/api/repos \
  -H "Content-Type: application/json" \
  -d '{"repos": ["octocat/Hello-World", "https://github.com/owner/repo"]}'
```

The response has one result per input, in order. `status` is `added`, `duplicate` (already in
the list) or `invalid` (not a GitHub repo name); the HTTP status is `201` when at least one repo
was added, otherwise `200`. A malformed body returns `422`.

```json
{"results": [
  {"input": "octocat/Hello-World", "full_name": "octocat/hello-world", "status": "added"},
  {"input": "https://github.com/owner/repo", "full_name": "owner/repo", "status": "duplicate"}
]}
```

New repos are backed up on the next scheduled run, or right away with "Sincronizar agora".
Like the UI, the API has no authentication (see Security) and requests sent from a browser on
another site are rejected.

## Schedule

`SYNC_CRON` and `SUMMARY_CRON` use standard cron syntax (day-of-week `0` or `7` = Sunday),
evaluated in the `TZ` time zone.

If the service was down at the scheduled time, it runs a catch-up backup on startup
when the last run is older than 23 hours.

## Security

The web UI has **no authentication**. By default it is published on `127.0.0.1` only, so just the
server itself can open it — never expose it to the internet.

### Opening it from other PCs

Set both in `.env`, then `docker compose up -d`:

```dotenv
BIND_ADDR=0.0.0.0            # or the server's LAN / Tailscale IP to listen on that interface only
ALLOWED_HOSTS=127.0.0.1,localhost,192.168.0.10,marte   # the IP / hostname you type in the browser
```

`BIND_ADDR` alone isn't enough: a request whose host isn't in `ALLOWED_HOSTS` gets
`400 Invalid host header`. `HOST_PORT` (default `8808`) changes the published port.

### Behind Nginx Proxy Manager (or another reverse proxy)

Example for `https://git-bkp.local.robsonv.com`:

1. `.env` — publish the port where the proxy can reach it and allow the domain:

   ```dotenv
   BIND_ADDR=0.0.0.0     # or the server's LAN IP; a proxy running in Docker can't reach the host's 127.0.0.1
   ALLOWED_HOSTS=127.0.0.1,localhost,git-bkp.local.robsonv.com
   ```

   Then `docker compose up -d`.
2. In Nginx Proxy Manager, add a Proxy Host:
   - Domain: `git-bkp.local.robsonv.com`
   - Scheme `http`, Forward Hostname/IP: the server's LAN IP, Forward Port: `8808`
   - SSL tab: your certificate for the domain, "Force SSL" on.

The proxy must forward the original `Host` header (Nginx Proxy Manager does by default). The app
compares the browser's `Origin` with that host to block cross-site form posts, so a proxy that
rewrites `Host` to the upstream address makes every add/remove/sync return `403`.

Two guards protect the UI from browser-based attacks: state-changing requests carrying a
cross-site `Origin` / `Sec-Fetch-Site` header are rejected (403), and only the hostnames in
`ALLOWED_HOSTS` (default `127.0.0.1,localhost`) are served, which blocks DNS rebinding. When you
bind to a LAN/VPN address, add it to `ALLOWED_HOSTS`, and **keep `127.0.0.1`** in the list: the
container healthcheck calls `http://127.0.0.1:8080/health`.

The GitHub token is passed to git through environment config on each command and is never
written to the mirrors' `config` files.

## Data

Everything lives in the `github-bkp-data` Docker volume mounted at `/data`:

- `/data/state.db` — SQLite with repos, runs and events
- `/data/repos/<owner>/<repo>.git` — mirrors (never deleted by the service)

The volume is declared `external`, so compose never creates or deletes it, and its name doesn't
depend on the folder the project is in. If it doesn't exist, `docker compose up` stops with
`external volume "github-bkp-data" not found`; create it with `docker volume create github-bkp-data`.
Only `docker volume rm github-bkp-data` deletes the backups.

**Upgrading from a version before the external volume** (data in `github-bkp-system_bkp-data`):
copy it once into the new volume, then start as usual. The old volume is left untouched; remove it
yourself with `docker volume rm github-bkp-system_bkp-data` after checking the new one.

```bash
docker compose down
docker volume create github-bkp-data
docker run --rm --user 0 --entrypoint sh \
  -v github-bkp-system_bkp-data:/from:ro -v github-bkp-data:/to \
  github-bkp-system:latest -c 'cp -a /from/. /to/'
docker compose up -d --build
```

If you use a bind mount instead of the volume, make the host directory writable by UID 1000.

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
