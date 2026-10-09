# --- Interface web --------------------------------------------------------
FROM node:20-alpine AS frontend
WORKDIR /app/frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY frontend/ ./
RUN npm run build

# --- Serveur --------------------------------------------------------------
FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PLUME_DATA_DIR=/data

# Polices libres compatibles avec Arial, Times, Courier, Calibri et Cambria
# (les PDF Word/Office s'éditent sans les polices Microsoft).
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      fonts-liberation fonts-dejavu-core fonts-crosextra-carlito fonts-crosextra-caladea fonts-noto-core \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY backend/requirements.txt backend/requirements.txt
RUN pip install --no-cache-dir -r backend/requirements.txt
COPY backend/plume backend/plume
COPY --from=frontend /app/frontend/dist frontend/dist

RUN useradd --system --uid 1000 --home /data plume && mkdir -p /data && chown plume /data
USER plume
VOLUME /data
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/health')"
# Un seul processus : MuPDF n'est pas utilisable depuis plusieurs threads à la fois.
CMD ["python", "-m", "uvicorn", "plume.app:app", "--app-dir", "backend", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips", "*"]
