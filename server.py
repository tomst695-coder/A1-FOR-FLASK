import os
import uuid
import threading
import base64
import requests
from flask import Flask, request, jsonify

app = Flask(__name__)

# جلب مفتاح الأمان من متغيرات البيئة لحماية السيرفر
API_KEY = os.environ.get("API_KEY")

# مخزن مؤقت لحالة المهام المستمرة
jobs = {}

def check_auth(req):
    if not API_KEY:
        return True # إذا لم يتم ضبط مفتاح أمان يعمل السيرفر مفتوحاً
    return req.headers.get("X-API-Key") == API_KEY

@app.route('/health', methods=['GET'])
def health():
    return jsonify({"status": "ok", "ffmpeg_available": True}), 200

def async_merge_task(job_id, footage_url, voice_base64, subs_base64, duration):
    try:
        footage_path = f"{job_id}_footage.mp4"
        voice_path = f"{job_id}_voice.wav"
        subs_path = f"{job_id}_subs.ass"
        output_path = f"{job_id}_output.mp4"

        # تحميل الفيديو الأصلي
        response = requests.get(footage_url, stream=True)
        with open(footage_path, 'wb') as f:
            for chunk in response.iter_content(chunk_size=8192):
                f.write(chunk)

        # فك تشفير الصوت والترجمة من base64
        with open(voice_path, 'wb') as f:
            f.write(base64.b64decode(voice_base64))
        
        with open(subs_path, 'wb') as f:
            f.write(base64.b64decode(subs_base64))

        # أمر FFmpeg لدمج الصوت وحرق الترجمة وقص الفيديو عمودياً 1080x1920 لشورتس يوتيوب
        ffmpeg_cmd = (
            f'ffmpeg -y -stream_loop -1 -i "{footage_path}" -i "{voice_path}" '
            f'-t {duration} -vf "scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,subtitles={subs_path}" '
            f'-c:v libx264 -preset superfast -c:a aac -b:a 192k -pix_fmt yuv420p "{output_path}"'
        )
        
        exit_code = os.system(ffmpeg_cmd)
        
        if exit_code == 0 and os.path.exists(output_path):
            with open(output_path, "rb") as video_file:
                encoded_string = base64.b64encode(video_file.read()).decode('utf-8')
            jobs[job_id] = {"status": "COMPLETED", "output_base64": encoded_string}
        else:
            jobs[job_id] = {"status": "FAILED", "error": "FFmpeg processing failed"}

        # تنظيف الملفات المؤقتة فوراً
        for p in [footage_path, voice_path, subs_path, output_path]:
            if os.path.exists(p): os.remove(p)

    except Exception as e:
        jobs[job_id] = {"status": "FAILED", "error": str(e)}

@app.route('/merge/async', methods=['POST'])
def merge_async():
    if not check_auth(request):
        return jsonify({"error": "Unauthorized"}), 401

    data = request.json
    job_id = str(uuid.uuid4())
    jobs[job_id] = {"status": "IN_PROGRESS"}

    # تشغيل المعالجة في الخلفية لمنع الـ Timeout في n8n
    threading.Thread(target=async_merge_task, args=(
        job_id, 
        data['footage_url'], 
        data['voice_base64'], 
        data['subs_base64'], 
        data.get('duration_seconds', 50)
    )).start()

    return jsonify({"status": "IN_PROGRESS", "job_id": job_id}), 202

@app.route('/status/<job_id>', methods=['GET'])
def get_status(job_id):
    if not check_auth(request):
        return jsonify({"error": "Unauthorized"}), 401

    job = jobs.get(job_id)
    if not job:
        return jsonify({"error": "Job not found"}), 404
        
    return jsonify(job), 200

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=10000)