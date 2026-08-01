# syntax=docker/dockerfile:1

# ---- deps stage -------------------------------------------------------
# NOTE: this project uses setuptools' src-layout (see pyproject.toml's
# [tool.setuptools.packages.find] where = ["src"]). `pip install .` needs
# setuptools to be able to *discover* a package under src/ to build the
# wheel's metadata, even before `src/` has any real code in it — otherwise
# it fails with "error in 'egg_base' option: 'src' does not exist or is not
# a directory". We create a minimal stub package here so pip can resolve
# and install every dependency (the expensive, cacheable part) without the
# real source being present yet; the `build` stage below then copies the
# real src/ over the stub and reinstalls with --no-deps, which is fast
# since dependency resolution already happened. This keeps this layer
# cached across builds where only your source changed, not pyproject.toml.
FROM python:3.12-slim AS deps
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends build-essential \
    && rm -rf /var/lib/apt/lists/*
COPY pyproject.toml README.md ./
RUN mkdir -p src/whale_alpha && touch src/whale_alpha/__init__.py
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir .

# ---- build stage (nothing to compile for a pure-Python project, but this
#      stage exists so the shape matches the original's deps -> build/generate
#      -> slim runtime pipeline, and gives a hook point for future codegen,
#      e.g. an ORM client generation step) ------------------------------
FROM deps AS build
COPY src ./src
COPY alembic ./alembic
COPY alembic.ini ./
COPY scripts ./scripts
RUN pip install --no-cache-dir --no-deps .

# ---- runtime stage ------------------------------------------------------
FROM python:3.12-slim AS runtime
WORKDIR /app

RUN groupadd --system whalealpha && useradd --system --gid whalealpha --create-home whalealpha

COPY --from=build /usr/local/lib/python3.12/site-packages /usr/local/lib/python3.12/site-packages
COPY --from=build /usr/local/bin /usr/local/bin
COPY --from=build /app/src ./src
COPY --from=build /app/alembic ./alembic
COPY --from=build /app/alembic.ini ./
COPY --from=build /app/scripts ./scripts

ENV PYTHONPATH=/app/src \
    PYTHONUNBUFFERED=1 \
    NODE_ENV=production

USER whalealpha

# Telegram updates use long-polling (no port needed for that), but the
# whale-wallet ingestion webhook server (integrations/helius_webhook.py)
# does listen on WEBHOOK_PORT (default 8080) — expose it so an indexer can
# reach it when this container is deployed behind a public URL.
EXPOSE 8080

CMD ["python", "-m", "whale_alpha.main"]
