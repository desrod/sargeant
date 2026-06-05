# Deploying sargeant as a hosted upload service

This covers running sargeant in hosted mode, where anyone who can reach it may upload sar text and
view it. For the offline single-user mode and the full list of options, see [README.md]
(README.md).

## Why uploads are safe to accept

The parser never executes an uploaded file. It is plain text processing: `split`, regular
expressions, and `float`. There is no call out to `sar` or `sadf`, no `eval`, and no shell. The
only real risk from a hostile upload is resource exhaustion, and the caps below bound that. You do
not need a per-file code-execution sandbox such as gVisor or firejail.

## What protects the service

The backend binds to loopback and runs behind a reverse proxy. A request passes through these
layers:

```
Internet → Caddy (TLS, body cap, timeouts) → sargeant (loopback) → per-session tmpfs dirs
```

In the app:

* Each visitor gets a random session cookie that maps to its own directory.
* Path-traversal guards and filename sanitization apply to every path.
* Uploads are capped at 25 MB per file, and at 512 MB and 64 files per session by default.
* Parsing is bounded by a maximum number of sections and rows, and the report is flagged `truncated`
  if it hits a ceiling.
* A background janitor removes idle sessions once they pass their TTL.
* The hostname in each sar banner is replaced with a random `host-xxxxxxxx` label before the file is
  written or parsed, so a customer machine name does not reach disk or the served JSON.

At the proxy: automatic HTTPS, a request-body cap, and timeouts. Per-IP rate limiting is optional
and covered below.

In the container: a non-root user, a read-only root filesystem, all Linux capabilities dropped,
`no-new-privileges`, and memory, CPU, and PID limits.

## Option A: Docker Compose

`docker-compose.yaml` is self-contained. There are no host paths to set up and nothing to share with
Docker Desktop:

```sh
docker compose up -d
# open http://127.0.0.1:8799/, or front it with your proxy (see below)
```

Uploads land on a Docker-managed tmpfs, which is RAM-backed and wiped when the container restarts.
The published port is `127.0.0.1:8799:8799`, which is loopback only. Change it to `8799:8799` for
direct LAN access.

Tune the limits through `environment:` in the compose file. No rebuild is needed. The keys are the
same `SG_*` variables documented in the README configuration table:

```yaml
environment:
  SG_MAX_UPLOAD_MB: "25"
  SG_MAX_SESSION_MB: "512"
  SG_MAX_FILES: "64"
  SG_SESSION_TTL_HOURS: "1"
  SG_BEHIND_TLS: "0"     # set to "1" only when a TLS proxy sits in front
  SG_PORT: "8799"
```

If you want to live-edit the UI, or put uploads on a host tmpfs instead of the Docker-managed one,
add your own `volumes:` bind mounts to the compose file.

The same thing with plain `docker run`:

```sh
docker build -t sargeant .
docker run -d --name sargeant --restart unless-stopped \
  -p 127.0.0.1:8799:8799 \
  --read-only --tmpfs /tmp:size=16m --tmpfs /data:size=768m,mode=1777 \
  --cap-drop ALL --security-opt no-new-privileges \
  --pids-limit 256 --memory 1g --cpus 1.0 \
  sargeant
```

## Option B: standalone, no container

Because it is standard library only, hosted mode is just Python on the host:

```sh
python3 server.py \
  --uploads-dir /var/lib/sargeant/uploads \
  --host 127.0.0.1 --port 8799 \
  --behind-tls
```

Run it under whatever process manager you already use, such as systemd. You give up the container's
filesystem and memory isolation, but the in-app protections(session isolation, the caps, bounded
parsing, traversal guards, the janitor, and hostname scrubbing) all still apply.

For the RAM-backed, wiped-on-restart behavior, put the uploads directory on a tmpfs:

```sh
mount -t tmpfs -o size=1g,mode=0700 tmpfs /var/lib/sargeant/uploads
```

Adjust the caps with `--max-upload-mb`, `--max-session-mb`, `--max-files`, and
`--session-ttl-hours`. The README configuration table lists their defaults.

## Put it behind Caddy

[`Caddyfile`](Caddyfile) has a ready site block. It targets `sar.example.com` and proxies to
`127.0.0.1:8799`. Change the hostname to your own, paste the block into your Caddy config, and
reload:

```sh
caddy reload --config /etc/caddy/Caddyfile
```

Caddy gets the TLS certificate on its own and forwards `X-Forwarded-Proto: https`, which makes
sargeant mark the session cookie `Secure`. The `--behind-tls` flag forces that regardless, so
either way the cookie is `Secure` in production.

## What each layer defends against

| Threat | What handles it |
|--------|-----------------|
| Code execution from a malicious file | Not possible. The parser never executes file content. |
| One user reading another's data | A per-session cookie maps to an isolated directory, with traversal guards on every path. |
| Memory or disk exhaustion | Per-file and per-session caps, bounded parsing, the container memory limit, and the tmpfs size. |
| Slowloris and held-open connections | A 30-second socket timeout in the app, plus Caddy's timeouts. |
| Upload floods | Caddy's `request_body max_size`, and optional per-IP rate limiting. |
| A container escape widening the blast radius | Non-root user, all capabilities dropped, `no-new-privileges`, and a read-only root filesystem. |
| Stale data lingering | The idle-session janitor, and the tmpfs being wiped on restart. |

## Things to keep in mind

* There are no accounts. A session lives in a cookie and is temporary, so clearing cookies loses
  access to earlier uploads. Add authentication only if you need durable history.
* Rate limiting is optional. It needs the `caddy-ratelimit` plugin, built with `xcaddy`. Without it,
  the size caps and the tmpfs ceiling still bound abuse, but a determined client can keep churning
  uploads, so add the plugin for a public endpoint.
* Logs go to stderr for the app and to `/var/log/caddy` for the proxy. Neither logs file contents.
