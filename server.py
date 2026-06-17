"""
Video Merger Microservice (v2 - مُصحَّح)
==========================================
نفس الوظيفة: يستقبل لقطة فيديو + صوت (base64) + ترجمة ASS (base64) + المدة،
يدمجهم بـ FFmpeg، يعيد الفيديو الناتج كـ base64 عبر نمط Submit/Poll.

الإصلاحات عن النسخة السابقة (Gemini):
1. subprocess.run() بقائمة بدل os.system() -> يمنع حقن أوامر (command injection)
2. tempfile.mkdtemp() لكل مهمة -> عزل كامل، لا تعارض بين طلبات متزامنة
3. threading.Lock() على قاموس jobs -> يمنع تلف بيانات عند وصول طلبات متعددة بنفس اللحظة
4. تنظيف يحدث حتى لو فشلت المهمة (finally) -> لا تتراكم ملفات على القرص المحدود لـ Render Free
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

from flask import Flask, request, jsonify

app = Flask(__name__)

API_KEY = os.environ.get("API_KEY")

JOBS = {}
JOBS_LOCK = threading.Lock()  # يحمي القراءة/الكتابة على JOBS من تضارب الـ threads
JOB_TTL_SECONDS = 3600


def require_api_key(view_func):
    """يتحقق من Header X-API-Key بزمن ثابت (compare_digest) لمنع timing attacks."""

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
    """فحص ffmpeg فعلياً، لا قيمة ثابتة (الإصدار السابق كان يُرجع True دائماً بلا فحص حقيقي)."""
    try:
        result = subprocess.run(["ffmpeg", "-version"], capture_output=True, text=True, timeout=10)
        return jsonify({"status": "ok", "ffmpeg_available": result.returncode == 0})
    except Exception as e:
        return jsonify({"status": "error", "ffmpeg_available": False, "error": str(e)}), 500


def _decode_b64_to_file(b64_data: str, output_path: Path):
    if "," in b64_data and b64_data.strip().startswith("data:"):
        b64_data = b64_data.split(",", 1)[1]
    output_path.write_bytes(base64.b64decode(b64_data))


def _download_url_to_file(url: str, output_path: Path):
    req = urllib.request.Request(url, headers={"User-Agent": "video-merger-service/2.0"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        output_path.write_bytes(resp.read())


def _run_merge_job(job_id: str, payload: dict):
    """
    التنفيذ الفعلي. كل مهمة تحصل على مجلد مؤقت مستقل بالكامل (tempfile.mkdtemp)
    بدل الكتابة في WORKDIR الثابت - هذا يمنع أي تعارض أسماء ملفات بين طلبات متزامنة
    ويسهّل التنظيف الكامل (حذف المجلد كله) بخطوة واحدة.
    """
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

        # subprocess.run بقائمة (list) صريحة - بخلاف os.system(f"...{var}...") في النسخة
        # السابقة، هذا النمط لا يمرّ مطلقاً عبر shell، فأي رمز خاص داخل أي حقل (مثل ; أو `)
        # يُعامل كنص حرفي ضمن القيمة فقط، ولا يمكنه إنهاء الأمر أو تنفيذ أمر إضافي.
        # cwd=work_dir يجعل أسماء الملفات نسبية (footage.mp4 لا /tmp/xxx/footage.mp4)
        # لتجنّب مشاكل escaping في فلتر ass= عند وجود مسارات مطلقة معقّدة.
        cmd = [
            "ffmpeg", "-y",
            "-stream_loop", "-1", "-i", "footage.mp4",
            "-i", "voice.wav",
            "-t", str(duration),
            "-vf",
            "scale=1080:1920:force_original_aspect_ratio=increase,"
            "crop=1080:1920,ass=subs.ass",
            "-map", "0:v", "-map", "1:a",
            "-c:v", "libx264", "-preset", "veryfast",
            "-c:a", "aac", "-b:a", "192k",
            "-pix_fmt", "yuv420p",
            "output.mp4",
        ]

        result = subprocess.run(
            cmd, cwd=str(work_dir), capture_output=True, text=True, timeout=300
        )

        if result.returncode != 0:
            raise RuntimeError(f"فشل ffmpeg (rc={result.returncode}):\n{result.stderr[-4000:]}")
        if not output_path.exists() or output_path.stat().st_size == 0:
            raise RuntimeError("ffmpeg لم يُنتج ملف فيديو صالح")

        output_b64 = base64.b64encode(output_path.read_bytes()).decode("ascii")

        with JOBS_LOCK:
            JOBS[job_id]["status"] = "COMPLETED"
            JOBS[job_id]["output_base64"] = output_b64
            JOBS[job_id]["error"] = None

    except Exception as e:
        with JOBS_LOCK:
            JOBS[job_id]["status"] = "FAILED"
            JOBS[job_id]["error"] = str(e)
            JOBS[job_id]["output_base64"] = None

    finally:
        # ينفّذ دائماً (نجاحاً أو فشلاً) - يحذف المجلد المؤقت بالكامل لمنع تراكم الملفات
        # على القرص المحدود لـ Render Free tier عبر تشغيلات متعددة
        try:
            for p in work_dir.glob("*"):
                p.unlink(missing_ok=True)
            work_dir.rmdir()
        except Exception:
            pass


@app.route("/merge/async", methods=["POST"])
@require_api_key
def merge_async():
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
        response["output_base64"] = job.get("output_base64")
    elif job["status"] == "FAILED":
        response["error"] = job.get("error")

    return jsonify(response)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
