FROM python:3.12-slim-bookworm

WORKDIR /app
ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir . \
    && python -m playwright install --with-deps chromium \
    && useradd --create-home appuser \
    && mkdir -p /data/cache /data/out \
    && chown -R appuser:appuser /data \
    && chmod -R a+rX /ms-playwright

ENV DERSNOTU_CACHE_DIR=/data/cache \
    DERSNOTU_OUT_DIR=/data/out \
    PYTHONUNBUFFERED=1
USER appuser
EXPOSE 8000
CMD ["sh", "-c", "exec python -m dersnotu.cli serve --host 0.0.0.0 --port ${PORT:-8000}"]
