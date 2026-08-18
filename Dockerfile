# App-runtime image for the AILIENANT backend (FastAPI + LanceDB + Tree-sitter).
#
# Distinct from ailienant-core/Dockerfile, which builds the command-sandbox
# image published to ghcr.io/gabrielv-engineer/ailienant-sandbox — that image
# is a bare execution cage for agent-run commands, not this application.
#
# Build context is the repo root (see docker-compose.yml) so this file can
# COPY only the ailienant-core/ subtree it needs.

FROM python:3.13-slim AS runtime

# build-essential + git: a few tree-sitter grammar packages have no prebuilt
# wheel for every platform/Python combination and build from source on install.
RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential git \
    && rm -rf /var/lib/apt/lists/*

RUN useradd --create-home --uid 1000 ailienant
WORKDIR /app

COPY ailienant-core/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY ailienant-core/ ./

# Pre-create the app-home mount point with the right ownership: a fresh named
# volume mounted over a path with no matching content in the image is
# initialized owned by root, which would block the non-root user below from
# writing its SQLite catalog / LanceDB tables on first boot.
RUN mkdir -p /home/ailienant/.ailienant \
    && chown -R ailienant:ailienant /app /home/ailienant
USER ailienant
ENV HOME=/home/ailienant

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    AILIENANT_API_PORT=8000

EXPOSE 8000

# Matches the "/" liveness probe main.py already exposes (health_check()).
HEALTHCHECK --interval=10s --timeout=3s --start-period=30s --retries=5 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/').status==200 else 1)"

# --host 0.0.0.0 is required here (unlike the extension's local spawn, which
# uses 127.0.0.1): the container only accepts inbound traffic on its bridge
# interface, not loopback, so binding to loopback would make the port
# unreachable from outside the container despite the compose port mapping.
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
