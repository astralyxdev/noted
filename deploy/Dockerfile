# noted: ядро на FastAPI + SQLite и дэшборд на astralyx-ui.
# Собирать из корня проекта:  docker build -f deploy/Dockerfile -t noted .

# ─────────────────────────── сборка дэшборда ───────────────────────────
# Node нужен только здесь. В финальный образ уезжает один каталог с готовыми
# файлами, так что ни node_modules, ни самого Node в рантайме нет.
FROM node:22-slim AS ui

WORKDIR /ui
COPY dashboard/package.json dashboard/package-lock.json ./
RUN npm ci
COPY dashboard/ ./
RUN npm run build

# ──────────────────────────────── ядро ────────────────────────────────
FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    NOTED_DB=/data/tasks.db \
    NOTED_UI_DIR=/app/dashboard/dist \
    NOTED_HOST=0.0.0.0 \
    NOTED_PORT=8787

WORKDIR /app

# Зависимости отдельным слоем — код меняется чаще, чем они
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY pyproject.toml main.py ./
COPY api ./api
COPY mcp_adapter ./mcp_adapter
RUN pip install --no-cache-dir --no-deps -e .

COPY --from=ui /ui/dist ./dashboard/dist

# База живёт в томе, а не в слое образа: пересборка не должна стирать задачи
RUN useradd --system --uid 10001 noted && mkdir -p /data && chown noted:noted /data
USER noted
VOLUME ["/data"]
EXPOSE 8787

HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
  CMD python -c "import urllib.request as u, sys; sys.exit(0 if u.urlopen('http://127.0.0.1:8787/healthz', timeout=2).status == 200 else 1)"

# Внутри контейнера слушаем 0.0.0.0, наружу публикуем только на 127.0.0.1 хоста.
CMD ["noted-api"]
