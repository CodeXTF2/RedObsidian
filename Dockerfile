# syntax=docker/dockerfile:1
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    REDOBSIDIAN_INSTANCE_PATH=/data/instance \
    REDOBSIDIAN_UPLOAD_DIR=/data/uploads

# postgresql-client provides pg_dump/pg_restore for manage_backup.py
RUN apt-get update \
 && apt-get install -y --no-install-recommends postgresql-client \
 && rm -rf /var/lib/apt/lists/* \
 && useradd --create-home --uid 10001 redobsidian \
 && mkdir -p /data/instance /data/uploads /backups \
 && chown -R redobsidian:redobsidian /data /backups

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt
COPY --chown=redobsidian:redobsidian . .

USER redobsidian
EXPOSE 5000

HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:5000/healthz', timeout=4).status == 200 else 1)"

# Migrations run once in the entrypoint, then gunicorn starts with auto-migrate off.
# One gevent worker per container; scale with more containers + REDIS_URL + sticky sessions at the proxy.
CMD ["sh", "-c", "python -c 'from app import create_app; create_app()' && REDOBSIDIAN_AUTO_MIGRATE=0 exec gunicorn --worker-class gevent --workers 1 --bind 0.0.0.0:5000 --forwarded-allow-ips='*' --access-logfile - wsgi:app"]
