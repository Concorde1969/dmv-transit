# DMV Regional Transportation Assistant — one image, two roles:
#   transit-mcp chat        web app + MCP server (behind Caddy)
#   transit-mcp scheduler   keeps transit data fresh
FROM python:3.11-slim

# Set WITH_NODE=true to include Node.js for an out-of-process JS scheduling engine
# (PLANNER_ENGINE=process, PLANNER_COMMAND="node engines/<your-engine>/engine.js").
ARG WITH_NODE=false

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONUTF8=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN if [ "$WITH_NODE" = "true" ]; then \
      apt-get update \
      && apt-get install -y --no-install-recommends nodejs \
      && rm -rf /var/lib/apt/lists/*; \
    fi

WORKDIR /app

# 1) Dependencies only (cached until pyproject.toml changes): install against a stub
#    package so code edits don't re-download every library.
COPY pyproject.toml README.md ./
RUN mkdir -p src/transit_mcp && touch src/transit_mcp/__init__.py \
    && pip install . \
    && pip uninstall -y transit-mcp \
    && rm -rf src

# 2) The application itself.
COPY src ./src
RUN pip install --no-deps . && rm -rf build
COPY agencies ./agencies
COPY engines ./engines

RUN useradd --create-home --uid 10001 app \
    && mkdir -p /data/feeds \
    && chown -R app:app /data /app
USER app

ENV APP_ENV=production \
    TRUST_PROXY=true \
    CHAT_HOST=0.0.0.0 \
    CHAT_PORT=8765 \
    TRANSIT_MCP_DB=/data/transit.db \
    CHATLOG_DB=/data/chatlog.db \
    TRANSIT_MCP_CACHE_DIR=/data/cache \
    GTFS_ARCHIVE_DIR=/data/gtfs \
    FEEDS_DIR=/data/feeds \
    AGENCIES_DIR=/app/agencies

VOLUME ["/data"]
EXPOSE 8765

HEALTHCHECK --interval=30s --timeout=5s --start-period=90s --retries=3 \
  CMD python -c "import sys,urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8765/api/health', timeout=4).status == 200 else 1)"

CMD ["transit-mcp", "chat"]
