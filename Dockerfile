FROM python:3.10-slim

# تثبيت الأدوات الأساسية مع FFmpeg والخطوط العربية لدعم الترجمة
RUN apt-get update && apt-get install -y \
    ffmpeg \
    fonts-dejavu \
    fonts-freefont-ttf \
    curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 10000

CMD ["python", "server.py"]
