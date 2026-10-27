FROM python:3.14-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 PLAYWRIGHT_BROWSERS_PATH=/ms-playwright
WORKDIR /app
RUN useradd --create-home --uid 10001 appuser
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir . && python -m playwright install --with-deps chromium
COPY alembic.ini ./
COPY migrations ./migrations
RUN mkdir -p /tmp/softexpert-exporter /mnt/client-share && chown -R appuser:appuser /app /tmp/softexpert-exporter /mnt/client-share
USER appuser
CMD ["uvicorn", "softexpert_exporter.main:app", "--host", "0.0.0.0", "--port", "8000"]
