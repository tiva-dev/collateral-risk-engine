# CRI service image: the API (CRI_SERVICE_ROLE=api), the private worker
# (CRI_SERVICE_ROLE=worker) and the operational Cloud Run Jobs share it.
FROM python:3.11-slim AS base
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /service
COPY requirements.txt ./
RUN pip install -r requirements.txt
COPY app ./app
ARG GIT_SHA=unknown
ENV CRI_GIT_SHA=${GIT_SHA} \
    HISTORICAL_DATA_CACHE_DIR=/tmp/cri-cache \
    CRI_FALLBACK_AUDIT_PATH=/tmp/cri-audit/audit_log.jsonl
RUN useradd --system --uid 10001 --no-create-home cri && mkdir -p /tmp/cri-cache /tmp/cri-audit && chown -R cri /tmp/cri-cache /tmp/cri-audit
USER 10001
EXPOSE 8080
CMD ["sh", "-c", "exec uvicorn app.platform.app:create_app --factory --host 0.0.0.0 --port ${PORT:-8080} --proxy-headers --forwarded-allow-ips='*' --no-server-header"]

# Research and backtesting image for Cloud Run Jobs.
FROM base AS research
USER root
RUN pip install "numpy>=1.26"
COPY research ./research
USER 10001
CMD ["python", "-m", "research.methodology_lab.run_experiments"]
