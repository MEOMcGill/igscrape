# Running `igscrape` in Docker

The container runs the CLI with a real (headful) Camoufox browser on a virtual X
display (`Xvfb`). That keeps Camoufox's stealth fingerprint intact on a server with
no screen. When you need to see the browser, for a first login or a checkpoint, the
`login` service also serves the display over noVNC.

| File | Role |
|---|---|
| [`Dockerfile`](Dockerfile) | Python 3.12 slim, browser system libs, Xvfb/x11vnc/noVNC, ffmpeg, the dependencies from `uv.lock`, `igscrape` (editable), and `camoufox fetch`. |
| [`docker-compose.yml`](docker-compose.yml) | `scraper` (the CLI) and `login` (the same, plus noVNC on `127.0.0.1:6080`). |
| [`docker/entrypoint.sh`](docker/entrypoint.sh) | Starts `Xvfb` (and, with `VNC=1`, fluxbox + x11vnc + websockify), then `exec`s your command. |

## Build

```bash
UID=$(id -u) GID=$(id -g) docker compose build
```

The container user gets your uid/gid, so files it writes into `db/` and `data/` are
owned by you on the host. (On Docker Desktop for macOS the defaults are fine.)

## State

| Host | Container | |
|---|---|---|
| `./db` | `/app/db` | `accounts.db`, the account pool (credentials + cookies) |
| `./data` | `/app/data` | scrape output |

Both are bind mounts and `.dockerignore`d, so they survive rebuilds and are never
baked into the image. The package is installed editable at `/app`, so the CLI's
defaults (`db/accounts.db`, `data/<endpoint>/`) land in them without any flags.

## Accounts

```bash
docker compose run --rm scraper igscrape add --username me --password '...'
docker compose run --rm scraper igscrape list -v
```

A first login usually needs a human (2FA, a challenge). Do it on the noVNC display:

```bash
docker compose run --rm --service-ports login igscrape login me --mode manual
```

then open <http://localhost:6080/vnc.html?autoconnect=1&resize=scale> and log in.
On a server, forward the port first: `ssh -L 6080:localhost:6080 <host>`.

Log accounts in **inside the container**, so the session's fingerprint is the one it
will scrape with. Two browsers on one Instagram session can invalidate it, so don't
use the same account from the container and from another machine at the same time.

## Scrape

Every CLI command from the [README](README.md) works as-is:

```bash
docker compose run --rm scraper igscrape scrape user-timeline natgeo \
    --start-date 2026-01-01 --end-date 2026-02-01
docker compose run --rm scraper igscrape scrape post DXyz123AbC
docker compose run --rm scraper igscrape flatten data/UserTimeline --format parquet
```

Leave the browser headful (the default): it renders to the container's Xvfb display.
`--headless` maps to Camoufox's `headless="virtual"`, which starts a second Xvfb of
its own without waiting for it to come up; under Docker Desktop on Apple silicon that
raced and failed with `cannot open display`.

## Proxies

Per-account, stored in the pool and applied by the browser session (a proxy needs
all three fields):

```bash
docker compose run --rm scraper igscrape set me proxy_server http://proxy:8080
docker compose run --rm scraper igscrape set me proxy_username user
docker compose run --rm scraper igscrape set me proxy_password pass
```

## Troubleshooting

| Symptom | Cause |
|---|---|
| Tabs crash / `NS_ERROR_...` on page load | `/dev/shm` too small; compose sets `shm_size: 2gb`. |
| `cannot open display` | `--headless` (see *Scrape*); drop it. |
| noVNC page doesn't load | Only the `login` service publishes 6080, and only with `--service-ports`. |
| Permission denied writing `db/` or `data/` | Rebuild with `UID=$(id -u) GID=$(id -g)`. |
