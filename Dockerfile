FROM python:3.11-slim

# تثبيت ffmpeg + الحزم الكاملة للخطوط العربية لمنع تشوه النصوص (يشمل Noto Naskh Arabic)
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    fontconfig \
    fonts-noto \
    fonts-noto-core \
    fonts-noto-extra \
    && fc-cache -f -v \
    && rm -rf /var/lib/apt/lists/*

# أمر تأكيدي لفحص اللغات المدعومة داخل الحاوية للتأكد من تفعيل العربية
RUN fc-list :lang=ar || true

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY server.py .

# ضبط المنفذ الافتراضي ليتطابق مع إعدادات Render (10000)
EXPOSE 10000

CMD ["python", "server.py"]
