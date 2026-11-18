FROM python:3.12-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    FARMAFACTURAS_HOST=127.0.0.1 \
    FARMAFACTURAS_PORT=8000 \
    FARMAFACTURAS_DATA_DIR=/app/data \
    FARMAFACTURAS_ARCHIVE_DIR=/app/archivo_facturas

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY updater.py .

VOLUME ["/app/data", "/app/archivo_facturas"]

EXPOSE 8000

CMD ["python", "-m", "app.main"]
