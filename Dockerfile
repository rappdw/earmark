# earmark: one image for the API (`earmark serve`) and the worker (`earmark worker`).
# Built and checked by .github/workflows/container.yml on linux/arm64; never run in the
# build sandbox. See docs/deploy.md.

FROM ghcr.io/astral-sh/uv:0.13.10@sha256:a3ea1357342c1a4bf9a0c53a3deb1b55379aefa037338d8113d5771efa01774f AS uv

FROM python:3.13.18-slim-trixie@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90 AS build
COPY --from=uv /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/app/.venv
WORKDIR /src
# Optional extras, space-separated: "transcribe youtube" (default), one of them, or "".
ARG EXTRAS="transcribe youtube"
COPY pyproject.toml uv.lock .python-version README.md ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-install-project \
      $(for e in $EXTRAS; do printf -- '--extra %s ' "$e"; done)
COPY src ./src
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-editable \
      $(for e in $EXTRAS; do printf -- '--extra %s ' "$e"; done)

FROM python:3.13.18-slim-trixie@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90 AS runtime
# The user that owns /data.
ARG UID=1000
ARG GID=1000
RUN test "$UID" -ne 0 && test "$GID" -ne 0 \
 && apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates \
 && rm -rf /var/lib/apt/lists/* \
 && groupadd --gid "$GID" --non-unique earmark \
 && useradd --uid "$UID" --gid "$GID" --non-unique --create-home \
      --home-dir /home/earmark --shell /usr/sbin/nologin earmark \
 && install -d -o earmark -g earmark -m 0750 /data
COPY --from=build /app/.venv /app/.venv
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    EARMARK_HOST=0.0.0.0 \
    EARMARK_DATA_DIR=/data \
    HF_HOME=/data/huggingface \
    HF_HUB_DISABLE_TELEMETRY=1
WORKDIR /home/earmark
USER earmark:earmark
EXPOSE 8787
ENTRYPOINT ["earmark"]
CMD ["serve"]
