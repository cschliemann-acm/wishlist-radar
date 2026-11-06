FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && python -m playwright install --with-deps chromium \
    && rm -rf /var/lib/apt/lists/*
COPY wishlist_radar ./wishlist_radar
# Mount persistent storage here (on Railway: add a Volume at /data)
ENV DATA_DIR=/data
EXPOSE 8000
# $PORT comes from the host; proxy headers so links use https behind Railway's proxy
CMD uvicorn wishlist_radar.web:app --host 0.0.0.0 --port ${PORT:-8000} --proxy-headers --forwarded-allow-ips="*"
