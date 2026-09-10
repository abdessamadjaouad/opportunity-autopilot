ARG PYTHON_IMAGE=python:3.14-slim-bookworm@sha256:9ab8d9c8514b44f90cf0029dd42fdd7e9e211e639c8b995304cc04568dee900f
ARG NODE_IMAGE=node:24-bookworm-slim@sha256:2fe369e969550cde8e867afc3fe370b260140cab4a23d467074295b42163d553
FROM ${NODE_IMAGE} AS web
WORKDIR /build
COPY web/package*.json ./
RUN npm ci
COPY web/ ./
RUN npm run build

FROM ${PYTHON_IMAGE}
ARG APP_UID=1000
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PATH=/app/.venv/bin:$PATH PLAYWRIGHT_BROWSERS_PATH=/opt/browsers
RUN apt-get update && apt-get install -y --no-install-recommends bubblewrap texlive-xetex texlive-latex-extra fonts-lato poppler-utils curl ca-certificates && rm -rf /var/lib/apt/lists/*
RUN install -d /usr/share/postgresql-common/pgdg && curl --fail -o /usr/share/postgresql-common/pgdg/apt.postgresql.org.asc https://www.postgresql.org/media/keys/ACCC4CF8.asc
COPY ops/pgdg.sources /etc/apt/sources.list.d/pgdg.sources
RUN apt-get update && apt-get install -y --no-install-recommends postgresql-client-18 && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir uv==0.12.5
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev && .venv/bin/playwright install --with-deps chromium && chmod -R a+rX /opt/browsers
COPY app/ app/
COPY migrations/ migrations/
COPY alembic.ini ./
COPY ops/ ops/
COPY --from=web /build/dist web/dist
RUN useradd --uid ${APP_UID} --create-home autopilot && mkdir -p /app/data /app/portfolio && chown -R autopilot:autopilot /app/data /app/portfolio
USER ${APP_UID}:${APP_UID}
CMD ["python", "-m", "app", "serve", "--host", "0.0.0.0"]
