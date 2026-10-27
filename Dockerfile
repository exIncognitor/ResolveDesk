FROM node:22-alpine AS frontend
WORKDIR /web
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY frontend/ ./
RUN npm run build

FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 ANONYMIZED_TELEMETRY=False
WORKDIR /app
COPY requirements.txt ./
RUN python -m pip install --no-cache-dir --upgrade pip==26.2.2
RUN python -m pip install --no-cache-dir -r requirements.txt
COPY . .
COPY --from=frontend /web/dist ./frontend/dist
RUN useradd --create-home --uid 10001 resolvedesk
USER resolvedesk
EXPOSE 8080
HEALTHCHECK --interval=10s --timeout=5s --start-period=30s --retries=12 CMD python -c "from urllib.request import urlopen; urlopen('http://127.0.0.1:8080/ready', timeout=4)"
CMD ["python", "-m", "uvicorn", "resolvedesk.app:app", "--host", "0.0.0.0", "--port", "8080", "--workers", "1", "--no-access-log"]
ARG RESOLVEDESK_REVISION=unversioned
LABEL org.opencontainers.image.revision=$RESOLVEDESK_REVISION
