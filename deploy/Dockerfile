# noted: a FastAPI core and a dashboard on astralyx-ui.
#
# The image carries both storage engines. On its own it runs on SQLite in the
# /data volume; set NOTED_DB_URL and the same image runs on PostgreSQL, which
# is what deploy/docker-compose.yml does.
# Build from the project root:  docker build -f deploy/Dockerfile -t noted .

# ─────────────────────────── building the dashboard ────────────────────
# Node is needed only here. One directory of finished files travels into the
# final image, so neither node_modules nor Node itself reaches the runtime.
FROM node:22-slim AS ui

WORKDIR /ui
COPY dashboard/package.json dashboard/package-lock.json ./
RUN npm ci
COPY dashboard/ ./
RUN npm run build

# ──────────────────────────────── the core ────────────────────────────
FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    NOTED_DB=/data/tasks.db \
    NOTED_UI_DIR=/app/dashboard/dist \
    NOTED_HOST=0.0.0.0 \
    NOTED_PORT=8787

WORKDIR /app

# Dependencies in their own layer: the code changes more often than they do
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY pyproject.toml main.py cli.py ./
COPY api ./api
COPY mcp_adapter ./mcp_adapter
RUN pip install --no-cache-dir --no-deps -e .

COPY --from=ui /ui/dist ./dashboard/dist

# The database lives in a volume, not in an image layer: a rebuild must not
# wipe the tasks
RUN useradd --system --uid 10001 noted && mkdir -p /data && chown noted:noted /data
USER noted
VOLUME ["/data"]
EXPOSE 8787

HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
  CMD python -c "import urllib.request as u, sys; sys.exit(0 if u.urlopen('http://127.0.0.1:8787/healthz', timeout=2).status == 200 else 1)"

# Inside the container we listen on 0.0.0.0; only the host loopback is published.
CMD ["noted-api"]
