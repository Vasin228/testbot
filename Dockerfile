FROM python:3.12-slim

# Prevent python from writing pyc files and buffering stdout/stderr
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Install security certificates and required runtime dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Install python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# Create an unprivileged user and isolate data directory
RUN groupadd -g 10001 sandboxgroup && \
    useradd -u 10001 -g sandboxgroup -m -d /app sandboxuser && \
    mkdir -p /app/data && \
    chown -R sandboxuser:sandboxgroup /app

# Copy application source code
COPY --chown=sandboxuser:sandboxgroup . /app

# Switch to unprivileged user
USER sandboxuser

CMD ["python", "-m", "src.main"]
