FROM python:3.12.16-slim-trixie@sha256:8523d6abeb48ceae0b7240d30c5808fc26cb79c1a635bacdf19cf3baecfbec07
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /srv/app
RUN apt-get update && apt-get install -y --no-install-recommends postgresql-client=17+278 && rm -rf /var/lib/apt/lists/*
COPY requirements.txt .
COPY requirements.lock .
RUN pip install --no-cache-dir --require-hashes -r requirements.lock
COPY app ./app
COPY scripts ./scripts
COPY migrations ./migrations
COPY workstreams/tgmate-v2/source_001_003 ./workstreams/tgmate-v2/source_001_003
COPY workstreams/tgmate-v2/app ./workstreams/tgmate-v2/app
COPY workstreams/tgmate-v2/scripts/roles.py ./workstreams/tgmate-v2/scripts/roles.py
COPY workstreams/tgmate-v2/*.sql ./workstreams/tgmate-v2/
COPY workstreams/tgmate-v2/UPLOAD_MANIFEST.json ./workstreams/tgmate-v2/
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
