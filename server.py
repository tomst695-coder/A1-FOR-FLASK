"""
Video Merger Microservice (v3 - Backblaze B2 & Sequential Rendering)
====================================================================
تحديث احترافي: يقوم بدمج الفيديو مشهداً بمشهد لتوفير الذاكرة العشوائية (RAM)،
ثم يرفع الفيديو النهائي مباشرة إلى Backblaze B2 المتوافق مع S3، ويرجع رابطاً نصياً قصيراً فقط.
يلغي هذا التحديث تماماً مشكلة "Instance failed" أو خطأ 502 على Render.
"""

import os
import gc
import secrets
import subprocess
import tempfile
import threading
import time
import uuid
import urllib.request
from functools import wraps
from pathlib import Path

import boto3
from botocore.client import Config
from flask import Flask, request, jsonify

app = Flask(__name__)

# جلب مفتاح الأمان الأساسي للسيرفر
API_KEY = os.environ.get("API_KEY")

# --- إعدادات Backblaze B2 (من متغيرات بيئة Render) ---
B2_ENDPOINT = os.environ.get("B2_ENDPOINT")          # مثال: https://s3.eu-central-003.backblazeb2.com
B2_KEY_ID = os.environ.get("B2_KEY_ID")              # الـ keyID الخاص بك
B2_APPLICATION_KEY = os.environ.get("B2_APPLICATION_KEY")  # الـ applicationKey الخاص بك
B2_BUCKET_NAME = os.environ.get("B2_BUCKET_NAME")      # اسم الـ Bucket: football-shorts-diyar-2026

# تخزين حالة المهام بالذاكرة لتتبعها
JOBS = {}
JOBS_LOCK = threading.Lock()


def require_api_key(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not API_KEY:
            return f(*args, **kwargs)
        
        token = request.headers.get("X-API-Key")
        if not token or not secrets.compare_digest(token, API_KEY):
            return jsonify({"status": "error", "error": "مفتاح API (X-API-Key) غير صالح أو مفقود"}), 401
        return f(*args, **kwargs)
    return decorated


def _run_merge_job(job_id, payload):
    """
    الدالة الأساسية التي تعمل في الخلفية (Thread) لدمج المشاهد بالتسلسل
    ثم رفع النتيجة إلى Backblaze B2 وتطهير الذاكرة والقرص.
    """
    tmp_dir = tempfile.TemporaryDirectory(prefix=f"b2_render_{job_id}_")
    base_path = Path(tmp_dir.name)
    
    try:
        scenes = payload.get("scenes", [])
        if not scenes:
            raise ValueError("مصفوفة المشاهد (scenes) فارغة أو غير موجودة في الطلب")
        
        rendered_scenes_paths = []
        
        # 🚀 خطوة 1: المعالجة المتسلسلة لكل مشهد على حدة لتوفير الذاكرة (Sequential Batch)
        for index, scene in enumerate(scenes):
            video_url = scene.get("video_url")
            audio_url = scene.get("audio_url")
            
            if not video_url or not audio_url:
                raise ValueError(f"المشهد في الحقل {index} يفتقد لرابط الفيديو أو الصوت")
                
            local_video = base_path / f"raw_video_{index}.mp4"
            local_audio = base_path / f"raw_audio_{index}.wav"
            scene_output = base_path / f"rendered_scene_{index}.mp4"
            
            # تنزيل ملفات المشهد الحالي فقط إلى القرص
            urllib.request.urlretrieve(video_url, str(local_video))
            urllib.request.urlretrieve(audio_url, str(local_audio))
            
            # دمج الصوت والفيديو للمشهد الحالي عبر FFmpeg مباشرة (أسرع وأخف للذاكرة من MoviePy)
            cmd = [
                "ffmpeg", "-y",
                "-i", str(local_video),
                "-i", str(local_audio),
                "-map", "0:v:0",
                "-map", "1:a:0",
                "-c:v", "libx264",
                "-c:a", "aac",
                "-shortest",
                str(scene_output)
            ]
            
            subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)
            rendered_scenes_paths.append(str(scene_output))
            
            # مسح الملفات الخام الفردية فوراً من القرص وتطهير الذاكرة المؤقتة
            if local_video.exists(): local_video.unlink()
            if local_audio.exists(): local_audio.unlink()
            gc.collect()

        # 🚀 خطوة 2: جمع المشاهد الجاهزة (Concatenation) دون إعادة ترميز ثقيلة
        concat_txt_path = base_path / "inputs.txt"
        with open(concat_txt_path, "w", encoding="utf-8") as f:
            for p in rendered_scenes_paths:
                f.write(f"file '{p}'\n")
                
        final_video_path = base_path / "final_output.mp4"
        
        concat_cmd = [
            "ffmpeg", "-y",
            "-f", "concat",
            "-safe", "0",
            "-i", str(concat_txt_path),
            "-c", "copy",  # نسخ مباشر بدون استهلاك معالج أو رام
            str(final_video_path)
        ]
        subprocess.run(concat_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)

        # 🚀 خطوة 3: الرفع المباشر من القرص إلى Backblaze B2 عبر boto3
        if not all([B2_ENDPOINT, B2_KEY_ID, B2_APPLICATION_KEY, B2_BUCKET_NAME]):
            raise ValueError("متغيرات بيئة Backblaze B2 غير مكتملة في إعدادات Render")

        s3_client = boto3.client(
            's3',
            endpoint_url=B2_ENDPOINT,
            aws_access_key_id=B2_KEY_ID,
            aws_secret_access_key=B2_APPLICATION_KEY,
            config=Config(signature_version='s3v4')
        )
        
        b2_file_name = f"{job_id}.mp4"
        
        # الرفع كملف مباشرة دون قراءته كبايتات في الـ RAM
        s3_client.upload_file(
            Filename=str(final_video_path),
            Bucket=B2_BUCKET_NAME,
            Key=b2_file_name,
            ExtraArgs={'ContentType': 'video/mp4'}
        )
        
        # توليد الرابط المباشر للملف المرفوع
        video_url_result = f"{B2_ENDPOINT.replace('s3.', '')}/{B2_BUCKET_NAME}/{b2_file_name}"
        
        with JOBS_LOCK:
            JOBS[job_id] = {
                "status": "COMPLETED",
                "video_url": video_url_result,
                "updated_at": time.time()
            }

    except Exception as e:
        with JOBS_LOCK:
            JOBS[job_id] = {
                "status": "error",
                "error": str(e),
                "updated_at": time.time()
            }
    finally:
        # تطهير وتنظيف المجلد المؤقت بالكامل من القرص الصلب لضمان تصفير المساحة
        try:
            tmp_dir.cleanup()
        except Exception:
            pass
        gc.collect()


def _cleanup_old_jobs():
    """تنظيف سجلات المهام القديمة لتجنب ملء الذاكرة بمرور الأيام"""
    now = time.time()
    max_age = 3600  # ساعة واحدة
    with JOBS_LOCK:
        to_delete = [jid for jid, info in JOBS.items() if now - info.get("updated_at", now) > max_age]
        for jid in to_delete:
            del JOBS[jid]


@app.route("/health", methods=["GET"])
def health():
    b2_status = all([B2_ENDPOINT, B2_KEY_ID, B2_APPLICATION_KEY, B2_BUCKET_NAME])
    return jsonify({
        "status": "healthy",
        "b2_configured": b2_status,
        "api_key_active": bool(API_KEY)
    }), 200


@app.route("/merge/async", methods=["POST"])
@require_api_key
def merge_async():
    content_length = request.content_length or 0
    MAX_REQUEST_BYTES = 5 * 1024 * 1024  # 5 ميجابايت كحد أقصى للطلب النصي JSON
    if content_length > MAX_REQUEST_BYTES:
        return jsonify({"status": "error", "error": "حجم طلب الـ JSON يتجاوز الحد المسموح به"}), 413

    payload = request.get_json(force=True, silent=True)
    if not payload:
        return jsonify({"status": "error", "error": "JSON body غير صالح أو مفقود"}), 400

    _cleanup_old_jobs()

    job_id = str(uuid.uuid4())
    with JOBS_LOCK:
        JOBS[job_id] = {"status": "IN_PROGRESS", "created_at": time.time()}

    # تشغيل المهمة في الخلفية فوراً للرد على n8n في أقل من ثانية وتفادي الـ Timeout
    thread = threading.Thread(target=_run_merge_job, args=(job_id, payload), daemon=True)
    thread.start()

    return jsonify({"status": "IN_PROGRESS", "job_id": job_id}), 202


@app.route("/status/<job_id>", methods=["GET"])
@require_api_key
def get_status(job_id):
    with JOBS_LOCK:
        job = JOBS.get(job_id)

    if not job:
        return jsonify({"status": "error", "error": "رقم المهمة job_id غير موجود أو انتهت صلاحيته"}), 404

    return jsonify(job), 200


if __name__ == "__main__":
    # تشغيل السيرفر على المنفذ الافتراضي لـ Render
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
