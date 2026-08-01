# syntax=docker/dockerfile:1

# ---- deps stage -------------------------------------------------------
FROM python:3.12-slim AS deps
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends build-essential \
    && rm -rf /var/lib/apt/lists/*
COPY pyproject.toml ./
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
# does listen on WEBHOOK_PORT (default 8080) â€” expose it so an indexer can
# reach it when this container is deployed behind a public URL.
EXPOSE 8080

CMD ["python", "-m", "whale_alpha.main"]
