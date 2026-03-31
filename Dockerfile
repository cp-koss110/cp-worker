# Multi-stage build for Worker service
FROM python:3.12-slim AS base

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/*

# ==========================================
# Builder stage
# ==========================================
FROM base AS builder

COPY requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt

# ==========================================
# Production stage
# ==========================================
FROM base AS production

ARG BUILD_DATE
ARG VERSION
ARG VCS_REF

ENV APP_VERSION=${VERSION:-unknown}

LABEL org.opencontainers.image.created="${BUILD_DATE}" \
      org.opencontainers.image.version="${VERSION}" \
      org.opencontainers.image.revision="${VCS_REF}" \
      org.opencontainers.image.title="devops-exam-worker" \
      org.opencontainers.image.description="DevOps Exam Worker Service"

COPY --from=builder /install /usr/local

COPY app/ ./app/

RUN useradd -r -u 1001 appuser && chown -R appuser /app
USER appuser

ENV PYTHONPATH=/app
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

CMD ["python", "-m", "app.worker"] #Bla
