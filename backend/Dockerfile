FROM python:3.12-slim

WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1

COPY backend/requirements.txt backend/requirements.txt
COPY data_migration/requirements.txt data_migration/requirements.txt
RUN pip install --no-cache-dir -r backend/requirements.txt -r data_migration/requirements.txt

COPY backend/app backend/app
COPY data_migration data_migration
COPY frontend frontend

ENV FRONTEND_DIR=/app/frontend
WORKDIR /app/backend
EXPOSE 8000
# Render (and most PaaS) pass the port in $PORT; default 8000 for docker compose
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
