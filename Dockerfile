# ─────────────────────────────────────────────
# Stage 1: Build React frontend (main app + Kairos chatbot UI merged)
# ─────────────────────────────────────────────
FROM node:20-alpine AS frontend-build
WORKDIR /app/frontend

# Copy lockfile first so `npm ci` layer can be cached across rebuilds
# whenever only source files (not deps) change.
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --no-audit --no-fund

COPY frontend/ ./

# Public, build-time frontend configuration.
#
# Vite inlines `import.meta.env.VITE_*` into the bundle at BUILD time, so a
# Cloud Run runtime environment variable can never reach the compiled JS — the
# frontend is compiled here, inside stage 1. Without this ARG,
# VITE_TURNSTILE_SITE_KEY resolved to "" in every production image and the bot
# challenge widget silently never rendered.
#
# Only the PUBLIC site key belongs here. It ships to browsers by design. The
# Turnstile *secret* is verified server-side and must only ever be a backend
# runtime variable (SIGNUP_CHALLENGE_SECRET) — never a VITE_* value, or it would
# be baked into the public bundle.
ARG VITE_TURNSTILE_SITE_KEY=""
ENV VITE_TURNSTILE_SITE_KEY=${VITE_TURNSTILE_SITE_KEY}

RUN npm run build

# ─────────────────────────────────────────────
# Stage 2: Python backend + built frontend
# ─────────────────────────────────────────────
# Pin to 3.12 to match the local dev environment. Earlier the image was on
# 3.11 and we hit a SyntaxError on import (PEP 701 backslash-in-f-string
# only legal from 3.12+) that silently broke /auth/forgot-password in prod
# because the email module failed to load. Keeping the runtime aligned with
# local removes the entire "works locally, breaks in prod" class of bugs.
FROM python:3.12-slim
WORKDIR /app

# Keep Python output unbuffered + skip .pyc files in the container layer.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

COPY backend/requirements.txt ./
RUN pip install -r requirements.txt

COPY backend/ ./
COPY --from=frontend-build /app/frontend/dist ./static

# Cloud Run / Heroku / Fly inject $PORT. EXPOSE is just metadata; the CMD
# below honours $PORT and falls back to 8080 for local `docker run`.
EXPOSE 8080

# Run pending Alembic migrations then start Gunicorn.
# Migrations are purely additive — safe to run on every deploy.
# `${PORT:-8080}` is critical: hardcoding 8080 makes Cloud Run deploys
# fail health checks whenever the platform allocates a different port.
#
# IMPORTANT: migrations must NOT gate gunicorn startup. Previously this was
# `alembic upgrade heads && exec gunicorn ...` — when the DB was transiently
# unreachable at deploy time (e.g. the Supabase pooler dropping connections),
# alembic exited non-zero, the `&&` short-circuited, gunicorn never bound the
# port, and Cloud Run failed the whole revision ("container failed to start
# and listen on the port"). That turns a brief DB blip into a hard outage that
# also blocks rolling out any fix. We now run migrations best-effort and ALWAYS
# start gunicorn, so the container becomes healthy and self-heals the moment
# the DB recovers. Schema is additionally self-ensured at app startup
# (lifespan ensure_tables + ADD COLUMN IF NOT EXISTS), so a skipped migration
# degrades gracefully rather than bricking the service.
#
# The behaviour above is preserved — gunicorn always starts — but the failure is no
# longer silent. backend/start.sh retries transient database errors and, if migrations
# genuinely cannot be applied, prints a MIGRATION_FAILURE banner naming the schema
# version it is actually running against. A real migration error used to look like one
# grey line, and the app then served requests against the old schema.
CMD ["sh", "./start.sh"]
