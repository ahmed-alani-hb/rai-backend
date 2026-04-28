# Production-ready Dockerfile for ERP Thaki backend.
# Uses Python 3.12 (battle-tested with all our dependencies — avoids the
# wheel issues we hit with 3.14 during local dev).

FROM python:3.12-slim

# Build essentials only for compiling any wheels that need it.
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python deps in a separate layer so code changes don't bust the cache
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r requirements.txt

# Now copy the app
COPY app ./app

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

# Most platforms (Cloud Run, Fly.io, Render, Railway) inject $PORT
EXPOSE 8000

# --workers 1 because we use in-process caching. Scale by adding more
# instances (Cloud Run, Fly.io machines), not workers per instance.
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000} --workers 1 --proxy-headers --forwarded-allow-ips '*'"]
