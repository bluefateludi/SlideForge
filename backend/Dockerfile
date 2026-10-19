# syntax=docker/dockerfile:1
FROM python:3.12.13-slim-bookworm@sha256:4766d8b510c428e595d74b9cc5bbb2fae8e26316fffb4adc89908d79aacd58a2

COPY --from=ghcr.io/astral-sh/uv:0.13.1@sha256:4a1f3666816dafadd20ceb71eac9a7d56d7ef30279f7d64048519ab901ada336 /uv /usr/local/bin/uv
WORKDIR /app

ENV PATH="/app/.venv/bin:$PATH" \
    UV_LINK_MODE=copy \
    UV_NO_DEV=1 \
    UV_PYTHON_DOWNLOADS=never \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    STORAGE_LOCAL_DIR=/data/storage

# Keep third-party dependencies cached when only application code changes.
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv lock --check && uv sync --frozen --no-dev --no-install-project

# Font downloads depend on their manifest/script, not business source files.
COPY scripts/fetch_fonts.py scripts/fonts.json ./scripts/
ARG ALLOW_MISSING_FONTS=false
RUN case "$ALLOW_MISSING_FONTS" in \
      false) python scripts/fetch_fonts.py ;; \
      true) python scripts/fetch_fonts.py --allow-missing ;; \
      *) echo "ALLOW_MISSING_FONTS must be true or false" >&2; exit 1 ;; \
    esac

COPY . .
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev

# Docker initializes a new storage volume with this directory's ownership.
RUN groupadd --gid 10001 slideforge \
    && useradd --uid 10001 --gid slideforge --create-home slideforge \
    && mkdir -p /data/storage \
    && chown slideforge:slideforge /data/storage
USER slideforge

EXPOSE 39800
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "39800"]
