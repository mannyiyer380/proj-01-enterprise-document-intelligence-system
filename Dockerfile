# Enterprise Document Intelligence System (EDIS)
# Build: docker build -t edis .
# Run:   see "Run with Docker" in README.md

FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    # Where fastembed stores the local fallback embedding model
    FASTEMBED_CACHE_PATH=/app/.cache/fastembed

WORKDIR /app

# libgomp1 is the OpenMP runtime that onnxruntime (used by fastembed) needs on slim images
RUN apt-get update \
    && apt-get install -y --no-install-recommends libgomp1 \
    && rm -rf /var/lib/apt/lists/*

# Install dependencies first so this layer is cached between code changes
COPY requirements.txt .
RUN pip install -r requirements.txt

# Run as a non-root user
RUN useradd --create-home --uid 1000 app

COPY --chown=app:app app/ ./app/
COPY --chown=app:app documents/ ./documents/

# Runtime folders: output/ holds accounts, sessions and Chroma indexes; logs/ holds JSON logs
RUN mkdir -p output logs .cache/fastembed && chown -R app:app output logs .cache

USER app

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/api/health', timeout=4)"

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
