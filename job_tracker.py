from __future__ import annotations

from datetime import datetime
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
import json
import math
import mimetypes
import shutil
import sqlite3
from statistics import median
import uuid

import av
import numpy as np

from progress_tracker import start_listener, step_for


def _fetch_json(url: str, timeout: float = 3) -> dict:
    with urlopen(Request(url, headers={"Accept": "application/json"}), timeout=timeout) as response:
        return json.loads(response.read())


def _collect_files(value, results: list[dict]) -> None:
    if isinstance(value, dict):
        if isinstance(value.get("filename"), str):
            results.append(value)
        else:
            for child in value.values():
                _collect_files(child, results)
    elif isinstance(value, list):
        for child in value:
            _collect_files(child, results)


def _generation_seconds(record: dict | None) -> float | None:
    if not record:
        return None
    messages = ((record.get("status") or {}).get("messages") or [])
    started = None
    finished = None
    for message in messages:
        if not isinstance(message, list) or len(message) < 2 or not isinstance(message[1], dict):
            continue
        timestamp = message[1].get("timestamp")
        if not isinstance(timestamp, (int, float)):
            continue
        if message[0] == "execution_start":
            started = timestamp if started is None else min(started, timestamp)
        elif message[0] in {"execution_success", "execution_error", "execution_interrupted"}:
            finished = timestamp if finished is None else max(finished, timestamp)
    if started is None or finished is None or finished < started:
        return None
    return round((finished - started) / 1000, 1)


def _safe_output_path(output_root: Path, item: dict) -> Path | None:
    subfolder = str(item.get("subfolder", "")).replace("\\", "/").strip("/")
    filename = Path(str(item.get("filename", ""))).name
    if not filename:
        return None
    candidate = (output_root / subfolder / filename).resolve()
    return candidate if (candidate == output_root or output_root in candidate.parents) else None


def _job_requests_film_noir(job: sqlite3.Row) -> bool:
    try:
        settings = json.loads(job["request_json"] or "{}")
    except (KeyError, TypeError, ValueError):
        return False
    palette = str(settings.get("cinema_palette") or "").casefold()
    prompt = str(settings.get("prompt") or "").casefold()
    return "film noir" in palette or "color palette: classic film noir palette" in prompt


def _render_film_noir(source: Path, destination: Path) -> None:
    source_container = av.open(str(source))
    target_container = av.open(str(destination), mode="w")
    try:
        source_video = next(stream for stream in source_container.streams if stream.type == "video")
        target_video = target_container.add_stream("libx264", rate=source_video.average_rate or 24)
        target_video.width = source_video.width
        target_video.height = source_video.height
        target_video.pix_fmt = "yuv420p"
        target_video.options = {"crf": "18", "preset": "fast"}
        audio_targets = {}
        for stream in source_container.streams:
            if stream.type != "audio":
                continue
            target_audio = target_container.add_stream("aac", rate=stream.rate or 48000)
            if stream.layout:
                target_audio.layout = stream.layout.name
            audio_targets[stream.index] = target_audio
        frame_index = 0
        for packet in source_container.demux():
            if packet.stream.type == "video":
                for frame in packet.decode():
                    rgb = frame.to_ndarray(format="rgb24").astype(np.float32)
                    gray = rgb[..., 0] * 0.2126 + rgb[..., 1] * 0.7152 + rgb[..., 2] * 0.0722
                    gray = np.clip((gray - 128.0) * 1.28 + 128.0, 0, 255)
                    rng = np.random.default_rng(frame_index + 1940)
                    gray = np.clip(gray + rng.normal(0.0, 2.2, gray.shape), 0, 255).astype(np.uint8)
                    mono = np.repeat(gray[..., None], 3, axis=2)
                    output_frame = av.VideoFrame.from_ndarray(mono, format="rgb24")
                    output_frame.pts = None
                    for encoded in target_video.encode(output_frame):
                        target_container.mux(encoded)
                    frame_index += 1
            elif packet.stream.type == "audio" and packet.stream.index in audio_targets:
                target_audio = audio_targets[packet.stream.index]
                for frame in packet.decode():
                    frame.pts = None
                    for encoded in target_audio.encode(frame):
                        target_container.mux(encoded)
        for encoded in target_video.encode():
            target_container.mux(encoded)
        for target_audio in audio_targets.values():
            for encoded in target_audio.encode():
                target_container.mux(encoded)
    finally:
        target_container.close()
        source_container.close()

def _import_outputs(connection: sqlite3.Connection, job: sqlite3.Row, data_root: Path, output_root: Path, output_data: dict, timestamp: str, generation_seconds: float | None = None) -> int:
    items: list[dict] = []
    _collect_files(output_data.get("outputs", {}), items)
    imported = 0
    for item in items:
        source_path = _safe_output_path(output_root, item)
        if not source_path or not source_path.is_file():
            continue
        original = source_path.name
        existing = connection.execute("SELECT id FROM assets WHERE source=? AND filename=? AND project_id=?", (job["prompt_id"], original, job["project_id"])).fetchone()
        if existing:
            if generation_seconds is not None:
                connection.execute("UPDATE assets SET generation_seconds=COALESCE(generation_seconds,?) WHERE id=?", (generation_seconds, existing["id"]))
            continue
        asset_id = str(uuid.uuid4())
        folder = data_root / "media" / job["owner_id"] / job["project_id"]
        folder.mkdir(parents=True, exist_ok=True)
        destination = folder / f"{asset_id}-{original}"
        mime = mimetypes.guess_type(original)[0] or "application/octet-stream"
        kind = "video" if mime.startswith("video/") else "audio" if mime.startswith("audio/") else "image" if mime.startswith("image/") else "output"
        if kind == "video" and _job_requests_film_noir(job):
            _render_film_noir(source_path, destination)
        else:
            shutil.copy2(source_path, destination)
        relative = destination.relative_to(data_root).as_posix()
        connection.execute("INSERT INTO assets(id,owner_id,project_id,kind,filename,relative_path,mime_type,bytes,source,created_at,generation_seconds) VALUES(?,?,?,?,?,?,?,?,?,?,?)", (asset_id, job["owner_id"], job["project_id"], kind, original, relative, mime, destination.stat().st_size, job["prompt_id"], timestamp, generation_seconds))
        imported += 1
    return imported


def _request_settings(job: sqlite3.Row) -> dict:
    try:
        value = json.loads(job["request_json"] or "{}")
        return value if isinstance(value, dict) else {}
    except (KeyError, TypeError, ValueError):
        return {}


def _estimate_key(job: sqlite3.Row) -> tuple[str, str, int]:
    settings = _request_settings(job)
    duration = int(round(float(settings.get("duration") or 0))) if job["preset"] in {"minimax-h3", "minimax-h3-faster", "minimax-h3-superfast", "minimax-h3-best", "minimax-h3-teacache", "minimax-h3-pdd", "ltx-2.5", "seedvr-upscale", "realesrgan-video-fast"} else 0
    return job["preset"], str(settings.get("resolution") or ""), duration


def _default_estimate(job: sqlite3.Row) -> float:
    settings = _request_settings(job)
    preset = job["preset"]
    resolution = str(settings.get("resolution") or "")
    scale = {"240p": 0.35, "360p": 0.5, "480p": 0.65, "720p": 0.8, "1080p": 1.0, "1440p": 1.7, "2160p": 3.2}.get(resolution, 1.0)
    duration = max(1.0, float(settings.get("duration") or 5))
    if preset == "indicf5-tts":
        return 45
    if preset == "flux-image":
        return 90 * scale
    if preset == "ltx-2.5":
        return max(45, duration * 45) * scale
    if preset in {"minimax-h3", "minimax-h3-faster", "minimax-h3-superfast", "minimax-h3-best", "minimax-h3-teacache", "minimax-h3-pdd"}:
        quality_scale = 1.25 if preset == "minimax-h3-best" else 0.55 if preset == "minimax-h3-teacache" else 0.45 if preset == "minimax-h3-pdd" else 0.35 if preset == "minimax-h3-superfast" else 0.72 if preset == "minimax-h3-faster" else 1.0
        return max(180, duration * 75) * scale * quality_scale
    if preset == "seedvr-upscale":
        return max(120, duration * 45) * scale
    if preset == "realesrgan-video-fast":
        return max(45, duration * 10) * scale
    if preset.startswith("realesrgan-"):
        return 25 * scale
    return 90 * scale


def _elapsed_seconds(created_at: str, timestamp: str) -> float:
    try:
        return max(0.0, (datetime.fromisoformat(timestamp) - datetime.fromisoformat(created_at)).total_seconds())
    except (TypeError, ValueError):
        return 0.0


def _was_cached(record: dict | None, duration: float | None) -> bool:
    if not record or duration is None or duration >= 0.1:
        return False
    messages = ((record.get("status") or {}).get("messages") or [])
    return any(isinstance(message, list) and message and message[0] == "execution_cached" for message in messages)


def _estimated_progress(elapsed: float, estimate: float) -> int:
    estimate = max(1.0, estimate)
    if elapsed <= estimate:
        return max(1, min(95, int(math.floor(95 * elapsed / estimate))))
    overtime = (elapsed - estimate) / estimate
    return min(99, 95 + int(math.floor(4 * (1 - math.exp(-overtime)))))


def refresh_jobs(db_path: Path, data_root: Path, output_root: Path, comfy_url: str, owner_id: str, project_id: str, timestamp: str) -> list[dict]:
    connection = sqlite3.connect(db_path, timeout=30)
    connection.row_factory = sqlite3.Row
    try:
        jobs = connection.execute("SELECT * FROM jobs WHERE owner_id=? AND project_id=? ORDER BY created_at DESC LIMIT 50", (owner_id, project_id)).fetchall()
        try:
            queue = _fetch_json(f"{comfy_url}/queue")
            queue_available = True
        except (HTTPError, URLError, TimeoutError, ValueError):
            queue = {}
            queue_available = False
        queue_items = list(queue.get("queue_running", [])) + list(queue.get("queue_pending", []))
        running = {str(item[1]) for item in queue.get("queue_running", []) if len(item) > 1}
        pending = {str(item[1]) for item in queue.get("queue_pending", []) if len(item) > 1}
        for item in queue_items:
            extra = item[3] if len(item) > 3 and isinstance(item[3], dict) else {}
            start_listener(comfy_url, str(extra.get("client_id") or ""))

        records: dict[str, dict | None] = {}
        for job in jobs:
            try:
                history = _fetch_json(f"{comfy_url}/history/{job['prompt_id']}")
            except (HTTPError, URLError, TimeoutError, ValueError):
                history = {}
            records[job["prompt_id"]] = history.get(job["prompt_id"])

        keyed_samples: dict[tuple[str, str, int], list[float]] = {}
        preset_samples: dict[str, list[float]] = {}
        for job in jobs:
            record = records.get(job["prompt_id"])
            duration = _generation_seconds(record)
            if duration is None or duration < 1 or _was_cached(record, duration):
                continue
            keyed_samples.setdefault(_estimate_key(job), []).append(duration)
            preset_samples.setdefault(job["preset"], []).append(duration)

        refreshed = []
        for job in jobs:
            status, error = job["status"], job["error"]
            record = records.get(job["prompt_id"])
            actual_seconds = _generation_seconds(record)
            if job["preset"] != "indicf5-tts" and status not in {"completed", "failed"}:
                if record:
                    status_info = record.get("status") or {}
                    completed = bool(status_info.get("completed"))
                    status_text = str(status_info.get("status_str", ""))
                    messages = status_info.get("messages") or []
                    if completed:
                        _import_outputs(connection, job, data_root, output_root, record, timestamp, actual_seconds)
                        status = "completed"
                    elif "error" in status_text.lower() or any(isinstance(message, list) and message and message[0] == "execution_error" for message in messages):
                        status = "failed"
                        error = "ComfyUI generation failed"
                elif job["prompt_id"] in running:
                    status = "running"
                elif job["prompt_id"] in pending:
                    status = "queued"
                elif queue_available and _elapsed_seconds(job["created_at"], timestamp) > 120:
                    status = "failed"
                    error = "Generation left the ComfyUI queue"
            if status != job["status"] or error != job["error"]:
                connection.execute("UPDATE jobs SET status=?,error=?,updated_at=? WHERE id=?", (status, error, timestamp, job["id"]))

            cached = _was_cached(record, actual_seconds)
            samples = keyed_samples.get(_estimate_key(job)) or preset_samples.get(job["preset"]) or []
            estimated_total = float(median(samples[:7])) if samples else _default_estimate(job)
            elapsed = actual_seconds if actual_seconds is not None else _elapsed_seconds(job["created_at"], timestamp)
            step = step_for(job["prompt_id"])
            if status == "completed":
                progress, phase, eta = 100, "Cached" if cached else "Completed", None
            elif status == "failed":
                progress, phase, eta = 100, "Failed", None
            elif status == "queued":
                progress, phase, eta = 0, "Queued", round(estimated_total)
            else:
                if step and step[1] > 0:
                    step_ratio = max(0.0, min(1.0, step[0] / step[1]))
                    progress = max(1, min(98, int(round(step_ratio * 98))))
                    eta_value = elapsed * (1.0 - step_ratio) / step_ratio if step_ratio > 0 else estimated_total
                    eta = round(eta_value) if eta_value >= 1 else None
                    phase = "Generating"
                else:
                    progress = _estimated_progress(elapsed, estimated_total)
                    eta_value = max(0.0, estimated_total - elapsed)
                    eta = round(eta_value) if eta_value >= 1 else None
                    phase = "Preparing" if progress < 95 else "Finishing"

            refreshed.append({
                "id": job["id"], "prompt_id": job["prompt_id"], "preset": job["preset"],
                "status": status, "progress": progress, "phase": phase,
                "step_current": step[0] if step else None, "step_max": step[1] if step else None,
                "elapsed_seconds": round(elapsed, 1), "estimated_total_seconds": round(estimated_total, 1),
                "eta_seconds": eta, "generation_seconds": actual_seconds, "cached": cached,
                "error": error, "created_at": job["created_at"],
                "updated_at": timestamp if status != job["status"] else job["updated_at"],
            })
        connection.execute("UPDATE projects SET updated_at=? WHERE id=? AND EXISTS(SELECT 1 FROM assets WHERE project_id=?)", (timestamp, project_id, project_id))
        connection.commit()
        return refreshed
    finally:
        connection.close()
