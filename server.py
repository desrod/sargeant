#!/usr/bin/env python3
"""sargeant — a lightweight browser viewer for sar (sysstat) text reports.

Deliberately built on the Python stdlib ``http.server`` with zero web-framework
dependencies (see project memory ``sargeant-stack``): it must start fast, stay
maintainable, and run offline against a sosreport on a local machine.

Two modes
---------
* Offline (default): `python3 server.py [--data DIR]` serves the local
  `sarXX` files in DIR, read-only, single user, bound to localhost. This is
  the original sosreport-analysis workflow and implicitly trusts its input.

* Hosted/multi-tenant: `python3 server.py --uploads-dir DIR` turns on
  per-session isolation and the upload endpoint so anyone can upload sar data
  and have it analyzed. Each remote user gets a random session token (HttpOnly
  cookie) backed by an isolated temp directory under DIR; uploads are size and
  quota capped and parsing is bounded (see `parser.py`). Old sessions are
  reaped by a background cleanup. This mode is meant to sit behind a reverse
  proxy (Caddy/nginx) that terminates TLS and adds request-size limits and
  rate limiting. Reminder: stdlib `http.server` is not hardened to face the
  internet directly. See DEPLOY.md.

Endpoints
---------
GET  /                          -> static index.html
GET  /static/<file>             -> vendored CSS/JS assets
GET  /api/files                 -> {"files": [{"name","day","host"}...]}
GET  /api/data?file=<name>      -> full parsed report (JSON)
POST /api/upload?name=<fname>   -> (hosted mode only) store raw body, returns meta

Usage
-----
    python3 server.py [--data DIR] [--host HOST] [--port PORT]
    python3 server.py --uploads-dir DIR --host 0.0.0.0 --behind-tls
"""

from __future__ import annotations

import argparse
import json
import os
import parser as sar
import re
import secrets
import shutil
import sys
import threading
import time
from functools import lru_cache
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

HERE = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(HERE, "static")

# sar text report filenames in offline mode: `sarXX` (XX = day of month). The
# binary `saXX` files are intentionally ignored, only text sar data is read.
# TODO: Support binary decoding later.
SAR_FILE_RE = re.compile(r"^sar(\d{2})$")

# Session tokens: url-safe, fixed'ish length, nothing path-like.
SESSION_RE = re.compile(r"^[A-Za-z0-9_-]{16,64}$")
SESSION_COOKIE = "sg_session"

_CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
}


class Config:
    # Here be Ye Offline mode:
    data_dir = HERE
    # Hosted mode (None => offline). When set, per-session isolation is active.
    uploads_dir: str | None = None
    max_upload_bytes = 25 * 1024 * 1024  # per single uploaded file
    max_session_bytes = 100 * 1024 * 1024  # total per session
    max_files_per_session = 64
    session_ttl_seconds = 6 * 3600
    secure_cookie = False  # set True if behind TLS (--behind-tls)

    @classmethod
    def multiuser(cls) -> bool:
        return cls.uploads_dir is not None


# Filename / path safety
def _safe_upload_name(raw: str) -> str:
    """Reduce an arbitrary client filename to a safe, flat storage name."""
    base = os.path.basename(raw or "")
    base = re.sub(r"[^A-Za-z0-9._-]", "_", base)
    base = base.lstrip(".")  # no hidden/dotfiles, no empty files
    return (base or "upload")[:64]


def _resolve_in(data_dir: str, name: str, multiuser: bool) -> str | None:
    """Resolve ``name`` to a file strictly inside ``data_dir`` (no traversal)."""
    if multiuser:
        if name != _safe_upload_name(name):
            return None
    elif not SAR_FILE_RE.match(name):
        return None
    if not name or "/" in name or "\\" in name:
        return None
    real_dir = os.path.realpath(data_dir)
    path = os.path.realpath(os.path.join(real_dir, name))
    if os.path.commonpath([path, real_dir]) != real_dir:
        return None
    return path if os.path.isfile(path) else None


def _session_dir(token: str, create: bool = False) -> str | None:
    """Map a session token to its isolated directory under uploads_dir."""
    if not Config.uploads_dir or not SESSION_RE.match(token):
        return None
    real_base = os.path.realpath(Config.uploads_dir)
    path = os.path.realpath(os.path.join(real_base, token))
    if os.path.commonpath([path, real_base]) != real_base or path == real_base:
        return None
    if create:
        os.makedirs(path, exist_ok=True)
    return path


# Listing / parsing the sar data into the view
def _list_files(data_dir: str, multiuser: bool) -> list[str]:
    try:
        names = os.listdir(data_dir)
    except OSError:
        return []
    if multiuser:
        out = [
            n
            for n in names
            if os.path.isfile(os.path.join(data_dir, n)) and n == _safe_upload_name(n)
        ]
    else:
        out = [n for n in names if SAR_FILE_RE.match(n)]
    return sorted(out)


# When hosted, we parse untrusted input, so cap it (parser defaults are the
# defensive ceilings); offline mode trusts files and disables the ceilings.
@lru_cache(maxsize=256)
def _parse_cached(path: str, mtime: float, capped: bool) -> str:
    """Parse a sar file to a JSON string, memoized on (path, mtime, mode)."""
    caps = {} if capped else {"max_sections": None, "max_rows": None}
    report = sar.parse_file(path, **caps)
    return json.dumps(report.to_dict())


def _report_json(path: str) -> str:
    return _parse_cached(path, os.path.getmtime(path), Config.multiuser())


def _file_meta(data_dir: str, multiuser: bool) -> list[dict]:
    out = []
    for name in _list_files(data_dir, multiuser):
        path = os.path.join(data_dir, name)
        host = day = None
        try:
            with open(path, "r", errors="replace") as fh:
                first = fh.readline()
            host, _, _, _, day = sar._parse_header_line(first)
        except OSError:
            pass
        out.append({"name": name, "day": day, "host": host})
    out.sort(key=lambda m: (m["day"] or "", m["name"]))
    return out


# Session cleanup: reap session dirs whose mtime is older than the TTL.
def _start_cleanup() -> None:
    base = Config.uploads_dir
    if not base:
        return
    interval = max(60, Config.session_ttl_seconds // 6)

    def run() -> None:
        while True:
            time.sleep(interval)
            now = time.time()
            try:
                entries = os.listdir(base)
            except OSError:
                continue
            for name in entries:
                p = os.path.join(base, name)
                try:
                    if (
                        os.path.isdir(p)
                        and now - os.path.getmtime(p) > Config.session_ttl_seconds
                    ):
                        shutil.rmtree(p, ignore_errors=True)
                except OSError:
                    pass

    threading.Thread(target=run, name="sargeant-cleanup", daemon=True).start()


class Handler(BaseHTTPRequestHandler):
    server_version = "sargeant/1.0"
    protocol_version = "HTTP/1.1"
    # Slow-client mitigation (defense in depth; the proxy is the real defense).
    timeout = 30

    def setup(self) -> None:
        super().setup()
        self._set_cookie: str | None = None

    # The main user session start here
    def _session_token(self) -> str:
        """Return the caller's session token, minting one if absent/invalid."""
        cookies = SimpleCookie(self.headers.get("Cookie", ""))
        morsel = cookies.get(SESSION_COOKIE)
        if morsel and SESSION_RE.match(morsel.value):
            return morsel.value
        token = secrets.token_urlsafe(24)
        secure = (
            "; Secure"
            if (
                Config.secure_cookie or self.headers.get("X-Forwarded-Proto") == "https"
            )
            else ""
        )
        self._set_cookie = (
            f"{SESSION_COOKIE}={token}; Path=/; HttpOnly; "
            f"SameSite=Strict{secure}; Max-Age={Config.session_ttl_seconds}"
        )
        return token

    def _current_data_dir(self, create: bool = False) -> str | None:
        if Config.multiuser():
            return _session_dir(self._session_token(), create=create)
        return Config.data_dir

    # Low-level file send, enable gzip in Caddy also as needed for compression
    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        if self._set_cookie:
            self.send_header("Set-Cookie", self._set_cookie)
            self._set_cookie = None
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _send_json(self, obj_or_str, code: int = 200) -> None:
        if isinstance(obj_or_str, (bytes, str)):
            body = obj_or_str.encode() if isinstance(obj_or_str, str) else obj_or_str
        else:
            body = json.dumps(obj_or_str).encode()
        self._send(code, body, _CONTENT_TYPES[".json"])

    def _send_error_json(self, code: int, msg: str) -> None:
        self._send_json({"error": msg}, code=code)

    def _send_static(self, rel: str) -> None:
        # rel is already validated previously, as a bare filename.
        path = os.path.join(STATIC_DIR, rel)
        if not os.path.isfile(path):
            self._send_error_json(404, f"not found: {rel}")
            return
        ext = os.path.splitext(path)[1]
        ctype = _CONTENT_TYPES.get(ext, "application/octet-stream")
        with open(path, "rb") as fh:
            self._send(200, fh.read(), ctype)

    def _read_body(self, max_bytes: int) -> tuple[bytes | None, int]:
        """Read the request body, bounded. Returns (data, http_error_or_0)."""
        raw_len = self.headers.get("Content-Length")
        if raw_len is None:
            return None, 411  # Length Required
        try:
            length = int(raw_len)
        except ValueError:
            return None, 400
        if length < 0:
            return None, 400
        if length > max_bytes:
            return None, 413  # Payload Too Large, kaboom!
        data = bytearray()
        remaining = length
        while remaining > 0:
            chunk = self.rfile.read(min(remaining, 65536))
            if not chunk:
                break
            data += chunk
            remaining -= len(chunk)
        return bytes(data), 0

    # routing
    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        route = parsed.path
        qs = parse_qs(parsed.query)

        if route in ("/", "/index.html"):
            # Ensure a session cookie is minted on first page load, nom nom!
            if Config.multiuser():
                self._session_token()
            self._send_static("index.html")
            return

        if route.startswith("/static/"):
            rel = route[len("/static/") :]
            if "/" in rel or "\\" in rel or not rel:
                self._send_error_json(400, "bad asset path")
                return
            self._send_static(rel)
            return

        if route == "/api/files":
            data_dir = self._current_data_dir()
            files = _file_meta(data_dir, Config.multiuser()) if data_dir else []
            self._send_json({"files": files, "uploads": Config.multiuser()})
            return

        if route == "/api/data":
            name = (qs.get("file") or [""])[0]
            data_dir = self._current_data_dir()
            path = _resolve_in(data_dir, name, Config.multiuser()) if data_dir else None
            if not path:
                self._send_error_json(404, f"no such sar file: {name!r}")
                return
            try:
                self._send_json(_report_json(path))
            except Exception as exc:  # noqa: BLE001, report parse failures as 500
                self._send_error_json(500, f"parse error: {exc}")
            return

        self._send_error_json(404, "not found")

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        route = parsed.path
        qs = parse_qs(parsed.query)

        if route != "/api/upload":
            self._send_error_json(404, "not found")
            return
        if not Config.multiuser():
            self._send_error_json(405, "uploads disabled (offline mode)")
            return

        name = _safe_upload_name((qs.get("name") or ["upload"])[0])
        data_dir = self._current_data_dir(create=True)
        if not data_dir:
            self._send_error_json(500, "could not allocate session")
            return

        # Per-session quotas before we even read the body of the data.
        existing = _list_files(data_dir, multiuser=True)
        if name not in existing and len(existing) >= Config.max_files_per_session:
            self._send_error_json(409, "session file limit reached; delete or wait")
            return
        used = sum(
            os.path.getsize(os.path.join(data_dir, n))
            for n in existing
            if os.path.isfile(os.path.join(data_dir, n))
        )

        body, err = self._read_body(Config.max_upload_bytes)
        if err:
            self._send_error_json(err, "upload rejected (too large or malformed)")
            return
        if not body:
            self._send_error_json(400, "empty upload")
            return
        if used + len(body) > Config.max_session_bytes:
            self._send_error_json(413, "session storage quota exceeded")
            return

        # Scrub the hostname from the sar header before it ever touches disk:
        # in a customer's sosreport the machine name can be PII. Replaced with a
        # random label, so neither the stored file nor the served JSON leaks it,
        # and no non-anonymous data ever lands on disk or in the ephemeral tmpfs
        body = sar.scrub_banner_hostname(body)

        # Write atomically into the session dir
        dest = _resolve_in(data_dir, name, multiuser=True) or os.path.join(
            data_dir, name
        )
        tmp = dest + ".part"
        try:
            with open(tmp, "wb") as fh:
                fh.write(body)
            os.replace(tmp, dest)
            os.utime(data_dir)  # bump session mtime so the cleanup stays warm
        except OSError as exc:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            self._send_error_json(500, f"could not store upload: {exc}")
            return

        # Parse once now so we can report a clear error on garbage input
        try:
            host, _, _, _, day = sar._parse_header_line(
                body.split(b"\n", 1)[0].decode(errors="replace")
            )
        except Exception:  # noqa: BLE001
            host = day = None
        self._send_json({"ok": True, "name": name, "host": host, "day": day})

    do_HEAD = do_GET

    def log_message(self, fmt: str, *args) -> None:  # quieter default logging
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))


def _env(name: str, default: str | None = None) -> str | None:
    """Environment override for a CLI default (lets the Docker image be
    configured purely via `environment:` in docker-compose)."""
    return os.environ.get(name, default)


def _env_bool(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes", "on")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="sargeant sar viewer")
    # Every default is overridable by an SG_* env var (for containerized use)
    # and then by an explicit CLI flag (which wins).
    ap.add_argument(
        "--data",
        default=_env("SG_DATA", HERE),
        help="offline mode: directory with sarXX files [$SG_DATA]",
    )
    ap.add_argument(
        "--uploads-dir",
        default=_env("SG_UPLOADS_DIR"),
        help="hosted mode: base dir for per-session uploads, enables "
        "uploads [$SG_UPLOADS_DIR]",
    )
    ap.add_argument("--host", default=_env("SG_HOST", "127.0.0.1"), help="[$SG_HOST]")
    ap.add_argument(
        "--port", type=int, default=int(_env("SG_PORT", "8799")), help="[$SG_PORT]"
    )
    ap.add_argument(
        "--max-upload-mb",
        type=int,
        default=int(_env("SG_MAX_UPLOAD_MB", "25")),
        help="hosted: max size of a single uploaded file [$SG_MAX_UPLOAD_MB]",
    )
    ap.add_argument(
        "--max-session-mb",
        type=int,
        default=int(_env("SG_MAX_SESSION_MB", "100")),
        help="hosted: max total storage per session [$SG_MAX_SESSION_MB]",
    )
    ap.add_argument(
        "--max-files",
        type=int,
        default=int(_env("SG_MAX_FILES", "64")),
        help="hosted: max files per session [$SG_MAX_FILES]",
    )
    ap.add_argument(
        "--session-ttl-hours",
        type=float,
        default=float(_env("SG_SESSION_TTL_HOURS", "6")),
        help="hosted: idle session lifetime before reaping [$SG_SESSION_TTL_HOURS]",
    )
    ap.add_argument(
        "--behind-tls",
        action="store_true",
        default=_env_bool("SG_BEHIND_TLS"),
        help="hosted: force the Secure flag on the session cookie. Usually "
        "unneeded — a TLS proxy's X-Forwarded-Proto is auto-detected "
        "[$SG_BEHIND_TLS]",
    )
    args = ap.parse_args(argv)

    if args.uploads_dir:
        Config.uploads_dir = os.path.realpath(args.uploads_dir)
        os.makedirs(Config.uploads_dir, exist_ok=True)
        Config.max_upload_bytes = args.max_upload_mb * 1024 * 1024
        Config.max_session_bytes = args.max_session_mb * 1024 * 1024
        Config.max_files_per_session = args.max_files
        Config.session_ttl_seconds = int(args.session_ttl_hours * 3600)
        Config.secure_cookie = args.behind_tls
        _start_cleanup()
        print(f"sargeant: HOSTED mode — per-session uploads under {Config.uploads_dir}")
        print(
            f"sargeant: limits {args.max_upload_mb}MB/file, "
            f"{args.max_session_mb}MB & {args.max_files} files/session, "
            f"TTL {args.session_ttl_hours}h"
        )
    else:
        Config.data_dir = os.path.realpath(args.data)
        files = _list_files(Config.data_dir, multiuser=False)
        print(
            f"sargeant: OFFLINE mode — serving {len(files)} sar file(s) "
            f"from {Config.data_dir}"
        )
    print(f"sargeant: http://{args.host}:{args.port}/")

    httpd = ThreadingHTTPServer((args.host, args.port), Handler)
    httpd.daemon_threads = True
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nsargeant: shutting down")
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
