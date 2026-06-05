FROM python:3.11-slim

WORKDIR /app
ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# Deps first (better layer caching)
COPY requirements.txt .
RUN pip install -r requirements.txt

# App code
COPY . .

# SQLite (and WAL files) live on a mounted volume at /data so they survive
# restarts and redeploys. Override DATABASE_URL only if you use Postgres.
ENV DATABASE_URL="sqlite+aiosqlite:////data/notebot.db"

EXPOSE 5001

# SINGLE worker — the in-process scheduler must not be duplicated.
CMD ["gunicorn", "-w", "1", "--threads", "8", "--timeout", "120", \
     "-b", "0.0.0.0:5000", "wsgi:application"]
