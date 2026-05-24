FROM python:3.13-slim
RUN useradd --create-home --uid 1000 sandbox \
 && mkdir -p /work \
 && chown sandbox:sandbox /work
USER sandbox
WORKDIR /workspace
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    RUFF_CACHE_DIR=/work/.ruff_cache \
    MYPY_CACHE_DIR=/work/.mypy_cache
CMD ["tail", "-f", "/dev/null"]
