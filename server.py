"""
Video Merger Microservice (v3 - Backblaze B2)
================================================
نفس الوظيفة الأساسية، لكن بدل إرجاع الفيديو النهائي كـ base64 ضخم بالذاكرة
(السبب الفعلي لتكرار "Instance failed" / 502 على Render Free - 512MB)،
يرفعه مباشرة لـ Backblaze B2 (تخزين متوافق مع S3) ويرجع رابطاً نصياً قصيراً فقط.

التغيير الجوهري عن v2:
- لا output_base64 إطلاقاً بأي استجابة - فقط video_url
- الرفع لـ B2 يتم بالقراءة من القرص مباشرة (boto3 upload_file)، لا بتحميل
  الملف كاملاً كـ Python bytes object أولاً
- هذا يلغي تماماً ذروة استهلاك الذاكرة التي كانت تحدث عند:
  1. base64.b64encode() لملف كامل بالذاكرة
  2. تضمين تلك السلسلة الضخمة بجسم استجابة JSON
"""

import base64
import os
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

API_KEY = os.environ.get("API_KEY")

# --- إعدادات Backblaze B2 (من متغيرات بيئة Render) ---
B2_ENDPOINT = os.environ.get("B2_ENDPOINT")          # مثال: https://s3.eu-central-003.backblazeb2.com
B2_KEY_ID = os.environ.get("B2_KEY_ID")              # keyID من Application Keys
B2_APPLICATION_KEY = os.environ.get("B2_APPLICATION_KEY")  # applicationKey من Application Keys
B2_BUCKET_NAME = os.environ.get("B2_BUCKET_NAME")    # اسم الـ Bucket (مثال: football-shorts-diyar-2026)

JOBS = {}
JOBS_LOCK = threading.Lock()
JOB_TTL_SECONDS = 3600


def _get_b2_client():
    """ينشئ عميل boto3 متوافق مع S3 API الخاص بـ B2. يُستدعى عند كل رفع (lightweight)."""
    if not all([B2_ENDPOINT, B2_KEY_ID, B2_APPLICATION_KEY, B2_BUCKET_NAME]):
        raise RuntimeError(
            "متغيرات بيئة B2 غير مكتملة - تأكد من ضبط B2_ENDPOINT, B2_KEY_ID, "
            "B2_APPLICATION_KEY, B2_BUCKET_NAME على Render"
        )
    return boto3.client(
        "s3",
        endpoint_url=B2_ENDPOINT,
        aws_access_key_id=B2_KEY_ID,
        aws_secret_access_key=B2_APPLICATION_KEY,
        config=Config(signature_version="s3v4"),
    )


def require_api_key(view_func):
    @wraps(view_func)
    def wrapped(*args, **kwargs):
        if not API_KEY:
            return view_func(*args, **kwargs)
        provided = request.headers.get("X-API-Key", "")
        if not secrets.compare_digest(provided, API_KEY):
            return jsonify({"status": "error", "error": "مفتاح API غير صالح أو مفقود (Header: X-API-Key)"}), 401
        return view_func(*args, **kwargs)

    return wrapped


def _cleanup_old_jobs():
    now = time.time()
    with JOBS_LOCK:
        expired = [jid for jid, j in JOBS.items() if now - j.get("created_at", now) > JOB_TTL_SECONDS]
        for jid in expired:
            JOBS.pop(jid, None)


@app.route("/health", methods=["GET"])
def health():
    """يفحص ffmpeg + إعدادات B2 معاً، لتشخيص أسرع لو فشل أي جزء."""
    try:
        result = subprocess.run(["ffmpeg", "-version"], capture_output=True, text=True, timeout=10)
        ffmpeg_ok = result.returncode == 0
    except Exception as e:
        return jsonify({"status": "error", "ffmpeg_available": False, "error": str(e)}), 500

    b2_configured = all([B2_ENDPOINT, B2_KEY_ID, B2_APPLICATION_KEY, B2_BUCKET_NAME])

    return jsonify({
        "status": "ok",
        "ffmpeg_available": ffmpeg_ok,
        "b2_configured": b2_configured,
    })


def _decode_b64_to_file(b64_data: str, output_path: Path):
    if "," in b64_data and b64_data.strip().startswith("data:"):
        b64_data = b64_data.split(",", 1)[1]
    output_path.write_bytes(base64.b64decode(b64_data))


def _download_url_to_file(url: str, output_path: Path):
    req = urllib.request.Request(url, headers={"User-Agent": "video-merger-service/3.0"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        output_path.write_bytes(resp.read())


def _upload_to_b2(local_path: Path, job_id: str) -> str:
    """
    يرفع الملف مباشرة من القرص لـ B2 عبر boto3.upload_file، الذي يقرأ ويرسل
    الملف على دفعات داخلياً (multipart لو كبر الحجم) بدل تحميله كاملاً بالذاكرة.
    يعيد رابطاً عاماً مباشراً للملف (يعمل فوراً لأن الـ Bucket مضبوط Public).
    """
    client = _get_b2_client()
    object_key = f"videos/{job_id}.mp4"

    client.upload_file(
        Filename=str(local_path),
        Bucket=B2_BUCKET_NAME,
        Key=object_key,
        ExtraArgs={"ContentType": "video/mp4"},
    )

    # رابط مباشر عام بصيغة S3-compatible القياسية لـ B2
    # شكل endpoint المُدخل: https://s3.eu-central-003.backblazeb2.com
    endpoint_host = B2_ENDPOINT.replace("https://", "").replace("http://", "")
    video_url = f"https://{B2_BUCKET_NAME}.{endpoint_host}/{object_key}"
    return video_url


def _run_merge_job(job_id: str, payload: dict):
    work_dir = Path(tempfile.mkdtemp(prefix=f"merge_{job_id}_"))
    try:
        footage_path = work_dir / "footage.mp4"
        voice_path = work_dir / "voice.wav"
        subs_path = work_dir / "subs.ass"
        output_path = work_dir / "output.mp4"

        footage_url = payload.get("footage_url")
        if not footage_url:
            raise ValueError("footage_url مطلوب")
        _download_url_to_file(footage_url, footage_path)

        voice_b64 = payload.get("voice_base64")
        if not voice_b64:
            raise ValueError("voice_base64 مطلوب")
        _decode_b64_to_file(voice_b64, voice_path)

        subs_b64 = payload.get("subs_base64")
        if not subs_b64:
            raise ValueError("subs_base64 مطلوب")
        _decode_b64_to_file(subs_b64, subs_path)

        duration = payload.get("duration_seconds")
        if not duration or float(duration) <= 0:
            raise ValueError("duration_seconds مطلوب ويجب أن يكون أكبر من صفر")
        duration = float(duration)

        cmd = [
            "ffmpeg", "-y",
            "-stream_loop", "-1", "-i", "footage.mp4",
            "-i", "voice.wav",
            "-t", str(duration),
            "-vf",
            "scale=1080:1920:force_original_aspect_ratio=increase,"
            "crop=1080:1920,ass=subs.ass",
            "-map", "0:v", "-map", "1:a",
            "-c:v", "libx264", "-preset", "ultrafast",
            "-threads", "1",
            "-c:a", "aac", "-b:a", "128k",
            "-pix_fmt", "yuv420p",
            "-movflags", "+faststart",
            "output.mp4",
        ]

        result = subprocess.run(
            cmd, cwd=str(work_dir), capture_output=True, text=True, timeout=300
        )

        if result.returncode != 0:
            raise RuntimeError(f"فشل ffmpeg (rc={result.returncode}):\n{result.stderr[-4000:]}")
        if not output_path.exists() or output_path.stat().st_size == 0:
            raise RuntimeError("ffmpeg لم يُنتج ملف فيديو صالح")

        # الفرق الجوهري عن v2: رفع مباشر لـ B2 بدل base64.b64encode بالذاكرة
        video_url = _upload_to_b2(output_path, job_id)

        with JOBS_LOCK:
            JOBS[job_id]["status"] = "COMPLETED"
            JOBS[job_id]["video_url"] = video_url
            JOBS[job_id]["error"] = None

    except Exception as e:
        with JOBS_LOCK:
            JOBS[job_id]["status"] = "FAILED"
            JOBS[job_id]["error"] = str(e)
            JOBS[job_id]["video_url"] = None

    finally:
        try:
            for p in work_dir.glob("*"):
                p.unlink(missing_ok=True)
            work_dir.rmdir()
        except Exception:
            pass


@app.route("/merge/async", methods=["POST"])
@require_api_key
def merge_async():
    content_length = request.content_length or 0
    MAX_REQUEST_BYTES = 80 * 1024 * 1024
    if content_length > MAX_REQUEST_BYTES:
        return jsonify({
            "status": "error",
            "error": f"حجم الطلب ({content_length // (1024*1024)}MB) يتجاوز الحد الآمن "
                     f"({MAX_REQUEST_BYTES // (1024*1024)}MB) لخادم بذاكرة محدودة"
        }), 413

    payload = request.get_json(force=True, silent=True)
    if not payload:
        return jsonify({"status": "error", "error": "JSON body غير صالح أو مفقود"}), 400

    _cleanup_old_jobs()

    job_id = str(uuid.uuid4())
    with JOBS_LOCK:
        JOBS[job_id] = {"status": "IN_PROGRESS", "created_at": time.time()}

    thread = threading.Thread(target=_run_merge_job, args=(job_id, payload), daemon=True)
    thread.start()

    return jsonify({"status": "IN_PROGRESS", "job_id": job_id}), 202


@app.route("/status/<job_id>", methods=["GET"])
@require_api_key
def get_status(job_id):
    with JOBS_LOCK:
        job = JOBS.get(job_id)

    if not job:
        return jsonify({"status": "error", "error": "job_id غير موجود (انتهت صلاحيته أو أُعيد تشغيل الخدمة)"}), 404

    response = {"status": job["status"]}
    if job["status"] == "COMPLETED":
        response["video_url"] = job.get("video_url")
    elif job["status"] == "FAILED":
        response["error"] = job.get("error")

    return jsonify(response)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
