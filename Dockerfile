FROM python:3.11-slim

RUN apt-get update && apt-get install -y ffmpeg && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY . .

RUN pip install --no-cache-dir -r requirements.txt

CMD gunicorn server:app \
    --bind 0.0.0.0:${PORT:-8080} \
    --workers 1 \
    --timeout 1200 \
    --keep-alive 120
