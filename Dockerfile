# Sweeper Bot V2 - Dockerfile
# AUDIT FIX #17: Container deployment support
# SECTION 21 AUDIT: Add observability port and health check
FROM python:3.13-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc g++ && \
    rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN mkdir -p data logs

ENV PYTHONUNBUFFERED=1
ENV LOG_JSON=true
ENV LOG_LEVEL=INFO
ENV OBS_PORT=9090

EXPOSE 9090

HEALTHCHECK --interval=60s --timeout=10s --retries=3 --start-period=30s \
    CMD python3 -c "import urllib.request; urllib.request.urlopen('http://localhost:9090/health')" || exit 1

CMD ["python3", "run_paper.py", "--cycles", "0"]
