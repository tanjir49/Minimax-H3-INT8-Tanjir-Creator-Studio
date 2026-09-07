from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import json
import os
import sqlite3
import subprocess
import threading
import uuid


TTS_ROOT = Path(os.environ.get("TTS_ROOT", Path(__file__).resolve().parent / "optional" / "indicf5"))
TTS_PYTHON = Path(os.environ.get("TTS_PYTHON", str(TTS_ROOT / ".venv" / "Scripts" / "python.exe")))
TTS_REPO = TTS_ROOT / "IndicF5"
TTS_MODEL = TTS_ROOT / "model_40000.pt"
TTS_VOCAB = TTS_ROOT / "vocab.txt"
TTS_LOCK = threading.Lock()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe_error(output: str) -> str:
    lines = [line.strip() for line in output.splitlines() if line.strip()]
    return (lines[-1] if lines else "IndicF5 generation failed")[:500]


def run_tts_job(
    db_path: Path,
    data_root: Path,
    job_id: str,
    prompt_id: str,
    owner_id: str,
    project_id: str,
    text: str,
    reference_audio: Path,
    reference_text: str,
    speed: float,
) -> None:
    with TTS_LOCK:
        with sqlite3.connect(db_path, timeout=30) as connection:
            connection.execute("UPDATE jobs SET status='running',updated_at=? WHERE id=?", (_now(), job_id))
            connection.commit()

        asset_id = str(uuid.uuid4())
        filename = f"IndicF5_TTS_{asset_id[:8]}.wav"
        relative = Path("media") / owner_id / project_id / f"{asset_id}-{filename}"
        output_path = data_root / relative
        output_path.parent.mkdir(parents=True, exist_ok=True)
        env = os.environ.copy()
        env["PYTHONUTF8"] = "1"
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONPATH"] = str(TTS_REPO)
        command = [
            str(TTS_PYTHON), "-m", "f5_tts.infer.infer_cli",
            "-m", "F5-TTS",
            "-p", str(TTS_MODEL),
            "-v", str(TTS_VOCAB),
            "-r", str(reference_audio),
            "-s", reference_text,
            "-t", text,
            "-o", str(output_path.parent),
            "-w", output_path.name,
            "--speed", str(speed),
        ]
        creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        try:
            result = subprocess.run(
                command,
                cwd=TTS_REPO,
                env=env,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=1800,
                creationflags=creation_flags,
            )
            if result.returncode != 0 or not output_path.is_file() or output_path.stat().st_size == 0:
                raise RuntimeError(_safe_error(result.stderr + "\n" + result.stdout))
            timestamp = _now()
            request_json = json.dumps(
                {"preset": "indicf5-tts", "prompt": text, "ref_text": reference_text, "speed": speed, "project_id": project_id},
                ensure_ascii=False,
                separators=(",", ":"),
            )
            with sqlite3.connect(db_path, timeout=30) as connection:
                connection.execute(
                    "INSERT INTO assets(id,owner_id,project_id,kind,filename,relative_path,mime_type,bytes,source,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (asset_id, owner_id, project_id, "audio", filename, relative.as_posix(), "audio/wav", output_path.stat().st_size, prompt_id, timestamp),
                )
                connection.execute("UPDATE jobs SET status='completed',error='',updated_at=?,request_json=? WHERE id=?", (timestamp, request_json, job_id))
                connection.commit()
        except Exception as error:
            output_path.unlink(missing_ok=True)
            with sqlite3.connect(db_path, timeout=30) as connection:
                connection.execute("UPDATE jobs SET status='failed',error=?,updated_at=? WHERE id=?", (str(error)[:500], _now(), job_id))
                connection.commit()


def queue_tts_job(**kwargs) -> None:
    threading.Thread(target=run_tts_job, kwargs=kwargs, name=f"indicf5-{kwargs['job_id'][:8]}", daemon=True).start()
