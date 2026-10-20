# Shortlist (`jjh serve`) for Fly.io: one always-on machine, user data on the volume at /data.
FROM python:3.14-slim

# tzdata so TZ (set in fly.toml) drives the quiet hours and the times users see
RUN apt-get update && apt-get install -y --no-install-recommends tzdata \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir uv

ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy PYTHONUNBUFFERED=1 PYTHONIOENCODING=utf-8
WORKDIR /app

# dependencies first, from the lockfile, so code changes rebuild fast
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project

COPY jev_job_hunter ./jev_job_hunter
COPY config/companies.yaml config/profile.example.yaml ./config/
RUN uv sync --frozen --no-dev

ENV PATH=/app/.venv/bin:$PATH JJH_DATA_DIR=/data HOST=0.0.0.0 PORT=8080
EXPOSE 8080
CMD ["jjh", "serve"]
