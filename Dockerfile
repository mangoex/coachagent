# ==========================================
# STAGE 1: Build dependencies
# ==========================================
FROM python:3.11-slim AS builder

WORKDIR /app

# Install system dependencies needed for compiling packages like psycopg2
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    libpq-dev \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .

# Install dependencies into user space to easily copy them in next stage
RUN pip install --no-cache-dir --user -r requirements.txt

# ==========================================
# STAGE 2: Runtime image
# ==========================================
FROM python:3.11-slim AS final

WORKDIR /app

# Install runtime PostgreSQL client library
RUN apt-get update && apt-get install -y --no-install-recommends \
    libpq5 \
    && rm -rf /var/lib/apt/lists/*

# Copy installed python dependencies from builder
COPY --from=builder /root/.local /root/.local
# Copy project files
COPY . .

# Expose user bin directory in PATH
ENV PATH=/root/.local/bin:$PATH
# Keep logs unbuffered for Cloud Logging to stream instantly
ENV PYTHONUNBUFFERED=1

# Cloud Run defaults to port 8080
EXPOSE 8080

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8080"]
