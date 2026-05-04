# syntax=docker/dockerfile:1.7

# For stronger reproducibility, pin the base to a digest. Find the current digest with:
#   docker pull python:3.11-slim && docker inspect python:3.11-slim --format '{{index .RepoDigests 0}}'
# then replace the line below with: FROM python:3.11-slim@sha256:<digest> AS base
FROM python:3.11-slim AS base

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# Create unprivileged user for runtime.
RUN groupadd --system --gid 1001 app && \
    useradd  --system --uid 1001 --gid app --home-dir /app --shell /sbin/nologin app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
RUN chown -R app:app /app

USER app

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health',timeout=3).status==200 else 1)"

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
