# sargeant — lean, distributable image.
#
# The app is pure-stdlib Python (no pip dependencies), so the image is just a
# tiny Python base + a few source files — no build stage, no wheels, no cache.
#
#   docker build -t sargeant .
#   docker compose up -d        # see docker-compose.yaml
#
# Everything is configurable at run time via SG_* env vars (see README/compose),
# so you never need to rebuild to change ports or limits.

FROM python:3.13-alpine

# Don't write .pyc files (keeps the read-only rootfs happy) and unbuffer stdout
# so `docker logs` is live.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# Run as an unprivileged, fixed UID so the host can constrain it predictably.
RUN adduser -D -u 10001 -s /sbin/nologin sargeant

WORKDIR /app
COPY parser.py server.py ./
COPY static/ ./static/

# Hosted (multi-tenant) defaults. Uploads live on /data, which docker-compose
# mounts as a size-capped tmpfs (RAM-backed, wiped on restart). Bind 0.0.0.0
# *inside* the container; the host decides what to publish.
ENV SG_UPLOADS_DIR=/data \
    SG_HOST=0.0.0.0 \
    SG_PORT=8799
RUN mkdir -p /data && chown sargeant:sargeant /data

USER sargeant
EXPOSE 8799

ENTRYPOINT ["python3", "server.py"]
