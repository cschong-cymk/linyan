FROM python:3.12-slim

# ffmpeg for video generation, fonts for any text rendering
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    fonts-dejavu-core \
    fonts-liberation \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt gunicorn psycopg2-binary

COPY . .

RUN mkdir -p /app/data/uploads /app/data/outputs

EXPOSE 8080

# Single worker (SQLite-style threading model + background threads), 4 threads for concurrency
CMD ["gunicorn", "--bind", "0.0.0.0:8080", "--workers", "1", "--threads", "4", "--timeout", "120", "app:app"]
