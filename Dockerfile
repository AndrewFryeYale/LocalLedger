# Local Ledger web app + CLI. Build: docker build -t localledger .   (not deployed by this repo)
FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install -e ".[postgres]"
COPY config ./config
COPY prompts ./prompts
COPY theses ./theses
COPY models ./models
COPY data ./data
# raw/, out/ and the SQLite DB live on a mounted volume in production (or use DATABASE_URL=postgres...)
RUN mkdir -p /app/raw /app/out /app/.cache
EXPOSE 8000
CMD ["sh", "-c", "ll seed && uvicorn localledger.web.app:app --host 0.0.0.0 --port ${PORT:-8000}"]
