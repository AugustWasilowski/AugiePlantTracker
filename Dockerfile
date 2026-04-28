FROM python:3.12-slim

# Pillow needs a few system libs for HEIC/JPEG/PNG handling.
RUN apt-get update && apt-get install -y --no-install-recommends \
      libjpeg62-turbo \
      libpng16-16 \
      libtiff6 \
      libwebp7 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

ENV PYTHONUNBUFFERED=1 \
    DATA_DIR=/data

VOLUME ["/data"]
EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
