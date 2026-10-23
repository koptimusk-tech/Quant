FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml ./
COPY qal ./qal
RUN pip install --no-cache-dir ".[postgres,yahoo,crypto]"
COPY alembic.ini ./
COPY migrations ./migrations
# Runs as an unprivileged user; data lives on a volume.
RUN useradd -m qal && mkdir -p /data && chown qal /data
USER qal
ENV QAL_DATA_DIR=/data
EXPOSE 8000
CMD ["sh", "-c", "alembic upgrade head && uvicorn qal.api.main:app --host 0.0.0.0 --port 8000"]
