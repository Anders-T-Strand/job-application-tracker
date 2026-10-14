FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    DB_PATH=/data/tracker.db \
    SECRETS_DIR=/secrets \
    RESUME_PATH=/data/resume.txt \
    PROFILE_PATH=/data/profile.md

WORKDIR /app

COPY pyproject.toml README.md ./
COPY app ./app
COPY scripts ./scripts
RUN pip install --no-cache-dir .

RUN useradd --uid 1000 --create-home tracker \
    && mkdir -p /data /secrets \
    && chown tracker:tracker /data /secrets
USER tracker

EXPOSE 8000

# Listens on 0.0.0.0 INSIDE the container namespace only; the host binding is
# restricted to 127.0.0.1 by the compose port mapping (see docker-compose.yml).
# --proxy-headers honors X-Forwarded-Proto from tailscale serve so generated
# absolute URLs (e.g. the bookmarklet) use https.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", \
     "--proxy-headers", "--forwarded-allow-ips", "*"]
