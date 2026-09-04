# Sweeper Bot V2 - Dockerfile
# AUDIT FIX #17: Container deployment support
FROM python:3.13-slim

WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc g++ && \
    rm -rf /var/lib/apt/lists/*

# Copy requirements and install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY . .

# Create data and logs directories
RUN mkdir -p data logs

# Environment defaults (override via .env or docker-compose)
ENV PYTHONUNBUFFERED=1
ENV LOG_JSON=true
ENV LOG_LEVEL=INFO

# Default to paper mode (safety: --live is blocked by P0_BLOCKED)
CMD ["python3", "run_paper.py", "--cycles", "0"]
