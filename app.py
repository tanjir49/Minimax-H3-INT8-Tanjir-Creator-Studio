from __future__ import annotations

from datetime import datetime, timedelta, timezone
from hashlib import pbkdf2_hmac, sha256
from http.cookies import SimpleCookie
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError, URLError
from generation import GenerationError, GenerationService
from job_tracker import refresh_jobs
from progress_tracker import start_listener
from urllib.request import Request, urlopen
from dotenv import load_dotenv
load_dotenv(Path(__file__).resolve().parent / ".env")
from tts_service import queue_tts_job, TTS_MODEL, TTS_VOCAB, TTS_PYTHON

import base64
import av
from PIL import Image
import json
import mimetypes
import os
import re
import shutil
import subprocess
import threading
import time

import psutil
import secrets
import sqlite3
import sys
import traceback
import uuid


ROOT = Path(__file__).resolve().parent
WEB_ROOT = ROOT / "web"
DATA_ROOT = Path(os.environ.get("STUDIO_DATA_ROOT", ROOT / "data")).resolve()
MEDIA_ROOT = DATA_ROOT / "media"
THUMB_ROOT = DATA_ROOT / "thumbnails"
DB_PATH = DATA_ROOT / "studio.db"
BOOTSTRAP_TOKEN_PATH = DATA_ROOT / "bootstrap-token.txt"
HOST = os.environ.get("STUDIO_HOST", "127.0.0.1")
PORT = int(os.environ.get("STUDIO_PORT", "8765"))
COMFY_URL = os.environ.get("COMFY_URL", "http://127.0.0.1:8188").rstrip("/")
DIRECTOR_URL = os.environ.get("DIRECTOR_URL", "http://127.0.0.1:8092").rstrip("/")
WORKFLOW_ROOT = ROOT / "workflows"
COMFY_INPUT = Path(os.environ.get("COMFY_INPUT", ROOT / "comfyui" / "input")).resolve()
COMFY_OUTPUT = Path(os.environ.get("COMFY_OUTPUT", ROOT / "comfyui" / "output")).resolve()
MAX_UPLOAD = 512 * 1024 * 1024
USERNAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{2,31}$")
SAFE_FILE_RE = re.compile(r"[^A-Za-z0-9._ -]+")
METRICS_LOCK = threading.Lock()
THUMB_LOCK = threading.Lock()
METRICS_CACHE: dict = {"at": 0.0, "data": None}

DATA_ROOT.mkdir(parents=True, exist_ok=True)
MEDIA_ROOT.mkdir(parents=True, exist_ok=True)
THUMB_ROOT.mkdir(parents=True, exist_ok=True)

# pythonw.exe intentionally has no visible console. Give the HTTP server and
# exception reporter a persistent background stream so request logging cannot
# abort responses when stdout/stderr are unavailable.
_BACKGROUND_LOG = None
if sys.stdout is None or sys.stderr is None:
    _BACKGROUND_LOG = (DATA_ROOT / "server-background.log").open("a", encoding="utf-8", buffering=1)
    if sys.stdout is None:
        sys.stdout = _BACKGROUND_LOG
    if sys.stderr is None:
        sys.stderr = _BACKGROUND_LOG


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def system_metrics() -> dict:
    with METRICS_LOCK:
        current = time.monotonic()
        if METRICS_CACHE["data"] is not None and current - METRICS_CACHE["at"] < 1.5:
            return METRICS_CACHE["data"]
        memory = psutil.virtual_memory()
        data = {
            "cpu": {"percent": round(psutil.cpu_percent(interval=None), 1)},
            "ram": {"percent": round(memory.percent, 1), "used_gb": round(memory.used / (1024 ** 3), 1), "total_gb": round(memory.total / (1024 ** 3), 1)},
            "gpu": {"percent": None, "memory_percent": None, "used_gb": None, "total_gb": None},
        }
        try:
            flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
            result = subprocess.run(
                ["nvidia-smi", "--query-gpu=utilization.gpu,memory.used,memory.total", "--format=csv,noheader,nounits"],
                capture_output=True, text=True, timeout=2, check=True, creationflags=flags,
            )
            gpu_usage, memory_used, memory_total = [float(value.strip()) for value in result.stdout.splitlines()[0].split(",")]
            data["gpu"] = {
                "percent": round(gpu_usage, 1),
                "memory_percent": round(memory_used * 100 / memory_total, 1) if memory_total else None,
                "used_gb": round(memory_used / 1024, 1),
                "total_gb": round(memory_total / 1024, 1),
            }
        except (FileNotFoundError, subprocess.SubprocessError, ValueError, IndexError):
            pass
        METRICS_CACHE.update(at=current, data=data)
        return data


def connect() -> sqlite3.Connection:
    connection = sqlite3.connect(DB_PATH, timeout=30)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA journal_mode = WAL")
    return connection


def initialize_database() -> None:
    with connect() as connection:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
              id TEXT PRIMARY KEY,
              username TEXT NOT NULL UNIQUE COLLATE NOCASE,
              role TEXT NOT NULL CHECK(role IN ('admin','member')),
              password_hash TEXT NOT NULL,
              enabled INTEGER NOT NULL DEFAULT 1,
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS sessions (
              token_hash TEXT PRIMARY KEY,
              user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
              csrf_token TEXT NOT NULL,
              expires_at TEXT NOT NULL,
              created_at TEXT NOT NULL,
              last_seen TEXT NOT NULL DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS projects (
              id TEXT PRIMARY KEY,
              owner_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
              name TEXT NOT NULL,
              description TEXT NOT NULL DEFAULT '',
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS assets (
              id TEXT PRIMARY KEY,
              owner_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
              project_id TEXT REFERENCES projects(id) ON DELETE SET NULL,
              kind TEXT NOT NULL,
              filename TEXT NOT NULL,
              relative_path TEXT NOT NULL,
              mime_type TEXT NOT NULL,
              bytes INTEGER NOT NULL,
              source TEXT NOT NULL DEFAULT 'upload',
              created_at TEXT NOT NULL,
              favorite INTEGER NOT NULL DEFAULT 0,
              published INTEGER NOT NULL DEFAULT 0,
              generation_seconds REAL
            );
            CREATE TABLE IF NOT EXISTS characters (
              id TEXT PRIMARY KEY,
              owner_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
              project_id TEXT REFERENCES projects(id) ON DELETE SET NULL,
              name TEXT NOT NULL COLLATE NOCASE,
              description TEXT NOT NULL DEFAULT '',
              features TEXT NOT NULL DEFAULT '',
              locks_json TEXT NOT NULL DEFAULT '{}',
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS character_references (
              id TEXT PRIMARY KEY,
              character_id TEXT NOT NULL REFERENCES characters(id) ON DELETE CASCADE,
              asset_id TEXT NOT NULL REFERENCES assets(id) ON DELETE CASCADE,
              role TEXT NOT NULL CHECK(role IN ('identity','face','full_body','costume','hairstyle','prop')),
              identity_lock INTEGER NOT NULL DEFAULT 1,
              notes TEXT NOT NULL DEFAULT '',
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL,
              UNIQUE(character_id,asset_id)
            );            CREATE TABLE IF NOT EXISTS asset_favorites (
              user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
              asset_id TEXT NOT NULL REFERENCES assets(id) ON DELETE CASCADE,
              created_at TEXT NOT NULL,
              PRIMARY KEY(user_id,asset_id)
            );
            CREATE TABLE IF NOT EXISTS jobs (
              id TEXT PRIMARY KEY,
              owner_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
              created_by TEXT REFERENCES users(id) ON DELETE SET NULL,
              project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
              prompt_id TEXT NOT NULL UNIQUE,
              preset TEXT NOT NULL,
              status TEXT NOT NULL CHECK(status IN ('queued','running','completed','failed')),
              error TEXT NOT NULL DEFAULT '',
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL,
              request_json TEXT NOT NULL DEFAULT '{}'
            );
            CREATE TABLE IF NOT EXISTS user_model_access (
              user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
              preset TEXT NOT NULL,
              enabled INTEGER NOT NULL DEFAULT 1,
              updated_at TEXT NOT NULL,
              PRIMARY KEY(user_id,preset)
            );
            CREATE TABLE IF NOT EXISTS user_resolution_access (
              user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
              resolution TEXT NOT NULL,
              enabled INTEGER NOT NULL DEFAULT 1,
              updated_at TEXT NOT NULL,
              PRIMARY KEY(user_id,resolution)
            );
            CREATE TABLE IF NOT EXISTS user_model_resolution_access (
              user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
              preset TEXT NOT NULL,
              resolution TEXT NOT NULL,
              enabled INTEGER NOT NULL DEFAULT 1,
              updated_at TEXT NOT NULL,
              PRIMARY KEY(user_id,preset,resolution)
            );
            CREATE INDEX IF NOT EXISTS idx_projects_owner ON projects(owner_id, updated_at DESC);
            CREATE INDEX IF NOT EXISTS idx_assets_project ON assets(project_id, created_at DESC);
            CREATE INDEX IF NOT EXISTS idx_characters_project ON characters(project_id, updated_at DESC);
            CREATE INDEX IF NOT EXISTS idx_characters_owner ON characters(owner_id, updated_at DESC);
            CREATE INDEX IF NOT EXISTS idx_character_references_character ON character_references(character_id, created_at);
            CREATE INDEX IF NOT EXISTS idx_jobs_project ON jobs(project_id, created_at DESC);
            CREATE INDEX IF NOT EXISTS idx_jobs_owner_status ON jobs(owner_id, status);
            """
        )
        session_columns = {row["name"] for row in connection.execute("PRAGMA table_info(sessions)")}
        if "last_seen" not in session_columns:
            connection.execute("ALTER TABLE sessions ADD COLUMN last_seen TEXT NOT NULL DEFAULT ''")
            connection.execute("UPDATE sessions SET last_seen=created_at WHERE last_seen='' ")
        character_columns = {row["name"] for row in connection.execute("PRAGMA table_info(characters)")}
        if "kind" not in character_columns:
            connection.execute("ALTER TABLE characters ADD COLUMN kind TEXT NOT NULL DEFAULT 'character'")
        job_columns = {row["name"] for row in connection.execute("PRAGMA table_info(jobs)")}
        if "request_json" not in job_columns:
            connection.execute("ALTER TABLE jobs ADD COLUMN request_json TEXT NOT NULL DEFAULT '{}'")
        if "created_by" not in job_columns:
            connection.execute("ALTER TABLE jobs ADD COLUMN created_by TEXT REFERENCES users(id) ON DELETE SET NULL")
        asset_columns = {row["name"] for row in connection.execute("PRAGMA table_info(assets)")}
        if "favorite" not in asset_columns:
            connection.execute("ALTER TABLE assets ADD COLUMN favorite INTEGER NOT NULL DEFAULT 0")
        if "published" not in asset_columns:
            connection.execute("ALTER TABLE assets ADD COLUMN published INTEGER NOT NULL DEFAULT 0")
        if "generation_seconds" not in asset_columns:
            connection.execute("ALTER TABLE assets ADD COLUMN generation_seconds REAL")
        connection.execute("INSERT OR IGNORE INTO asset_favorites(user_id,asset_id,created_at) SELECT owner_id,id,created_at FROM assets WHERE favorite=1")


def migrate_global_character_library() -> None:
    """Detach reusable identities from projects without losing their references."""
    connection = sqlite3.connect(DB_PATH, timeout=30)
    try:
        columns = {row[1]: row for row in connection.execute("PRAGMA table_info(characters)")}
        table_sql = (connection.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='characters'").fetchone() or [""])[0]
        if not columns or (not columns["project_id"][3] and "ON DELETE SET NULL" in table_sql.upper()):
            return
        connection.execute("PRAGMA foreign_keys=OFF")
        connection.executescript(
            """
            BEGIN IMMEDIATE;
            CREATE TABLE characters_global (
              id TEXT PRIMARY KEY,
              owner_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
              project_id TEXT REFERENCES projects(id) ON DELETE SET NULL,
              kind TEXT NOT NULL DEFAULT 'character',
              name TEXT NOT NULL COLLATE NOCASE,
              description TEXT NOT NULL DEFAULT '',
              features TEXT NOT NULL DEFAULT '',
              locks_json TEXT NOT NULL DEFAULT '{}',
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL
            );
            CREATE TABLE character_references_global (
              id TEXT PRIMARY KEY,
              character_id TEXT NOT NULL REFERENCES characters_global(id) ON DELETE CASCADE,
              asset_id TEXT NOT NULL REFERENCES assets(id) ON DELETE CASCADE,
              role TEXT NOT NULL,
              identity_lock INTEGER NOT NULL DEFAULT 1,
              notes TEXT NOT NULL DEFAULT '',
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL,
              UNIQUE(character_id,asset_id)
            );
            INSERT INTO characters_global(id,owner_id,project_id,kind,name,description,features,locks_json,created_at,updated_at)
              SELECT id,owner_id,NULL,kind,name,description,features,locks_json,created_at,updated_at FROM characters;
            INSERT INTO character_references_global SELECT * FROM character_references;
            DROP TABLE character_references;
            DROP TABLE characters;
            ALTER TABLE characters_global RENAME TO characters;
            ALTER TABLE character_references_global RENAME TO character_references;
            CREATE INDEX idx_characters_project ON characters(project_id,updated_at DESC);
            CREATE INDEX idx_characters_owner ON characters(owner_id,updated_at DESC);
            CREATE INDEX idx_character_references_character ON character_references(character_id,created_at);
            COMMIT;
            """
        )
    finally:
        connection.close()


def hash_password(password: str) -> str:
    rounds = 600_000
    salt = secrets.token_bytes(16)
    digest = pbkdf2_hmac("sha256", password.encode(), salt, rounds)
    return f"pbkdf2_sha256${rounds}${base64.b64encode(salt).decode()}${base64.b64encode(digest).decode()}"


def check_password(password: str, encoded: str) -> bool:
    try:
        algorithm, rounds, salt, expected = encoded.split("$", 3)
        if algorithm != "pbkdf2_sha256":
            return False
        digest = pbkdf2_hmac("sha256", password.encode(), base64.b64decode(salt), int(rounds))
        return secrets.compare_digest(digest, base64.b64decode(expected))
    except (ValueError, TypeError):
        return False


def public_user(row: sqlite3.Row) -> dict:
    return {"id": row["id"], "username": row["username"], "role": row["role"], "enabled": bool(row["enabled"]), "created_at": row["created_at"]}


CHARACTER_REFERENCE_ROLES = {"character_sheet", "identity", "face", "eyes", "lips", "side_profile", "hairstyle", "full_body", "body_shape", "costume", "hands", "footwear", "prop"}
CHARACTER_LOCK_KEYS = ("face", "hairstyle", "age", "body", "costume", "accessories")


def character_locks(value: object) -> dict[str, bool]:
    source = value if isinstance(value, dict) else {}
    return {key: bool(source.get(key, key in {"face", "hairstyle", "age", "body"})) for key in CHARACTER_LOCK_KEYS}


def character_payload(row: sqlite3.Row, references: list[sqlite3.Row] | None = None) -> dict:
    try:
        locks = character_locks(json.loads(row["locks_json"] or "{}"))
    except (TypeError, ValueError):
        locks = character_locks({})
    item = {"id": row["id"], "project_id": row["project_id"], "kind": (row["kind"] if "kind" in row.keys() else "character"), "name": row["name"], "description": row["description"], "features": row["features"], "locks": locks, "created_at": row["created_at"], "updated_at": row["updated_at"]}
    item["references"] = [{"id": ref["id"], "asset_id": ref["asset_id"], "role": ref["role"], "identity_lock": bool(ref["identity_lock"]), "notes": ref["notes"], "filename": ref["filename"], "mime_type": ref["mime_type"], "url": f"/media/{ref['asset_id']}", "thumbnail_url": f"/thumbnail/{ref['asset_id']}"} for ref in (references or [])]
    return item


def load_project_characters(connection: sqlite3.Connection, project_id: str, ids: list[str] | None = None) -> list[dict]:
    project = connection.execute("SELECT owner_id FROM projects WHERE id=?", (project_id,)).fetchone()
    if not project:
        return []
    values: list[object] = [project["owner_id"]]
    where = "c.owner_id=?"
    if ids:
        marks = ",".join("?" for _ in ids)
        where += f" AND c.id IN ({marks})"
        values.extend(ids)
    rows = connection.execute(f"SELECT c.* FROM characters c WHERE {where} ORDER BY c.updated_at DESC,c.name COLLATE NOCASE", values).fetchall()
    by_character: dict[str, list[sqlite3.Row]] = {row["id"]: [] for row in rows}
    if rows:
        marks = ",".join("?" for _ in rows)
        refs = connection.execute(f"SELECT cr.*,a.filename,a.mime_type FROM character_references cr JOIN assets a ON a.id=cr.asset_id WHERE cr.character_id IN ({marks}) AND a.kind='image' ORDER BY cr.created_at", tuple(row["id"] for row in rows)).fetchall()
        for ref in refs:
            by_character.setdefault(ref["character_id"], []).append(ref)
    mapped = {row["id"]: character_payload(row, by_character.get(row["id"], [])) for row in rows}
    return [mapped[character_id] for character_id in ids or mapped if character_id in mapped]


def compile_character_identity(connection: sqlite3.Connection, project_id: str, payload: dict, max_images: int = 9, reference_style: str = "pictures") -> None:
    requested = list(dict.fromkeys(str(value) for value in (payload.get("character_ids") or []) if value))[:6]
    if not requested:
        return
    characters = load_project_characters(connection, project_id, requested)
    if len(characters) != len(requested):
        raise ApiError(400, "One or more selected characters are unavailable")
    available = max(0, max_images - int(bool(payload.get("start_frame"))) - int(bool(payload.get("end_frame"))))
    image_refs = list(dict.fromkeys(str(value) for value in (payload.get("image_refs") or []) if value))
    for character in characters:
        priority = {"character_sheet": 0, "identity": 1, "face": 2, "eyes": 3, "lips": 4, "side_profile": 5, "full_body": 6, "body_shape": 7, "hairstyle": 8, "costume": 9, "hands": 10, "footwear": 11, "prop": 12}
        for ref in sorted(character["references"], key=lambda item: priority.get(item["role"], 99)):
            if ref["asset_id"] not in image_refs and len(image_refs) < available:
                image_refs.append(ref["asset_id"])
    payload["image_refs"] = image_refs[:available]
    offset = int(bool(payload.get("start_frame"))) + int(bool(payload.get("end_frame")))
    role_text = {"character_sheet": "master character turnaround sheet; treat every panel and view as the same person and consistently preserve the face, profile, body proportions, hairstyle and costume", "identity": "overall identity and appearance", "face": "face, facial structure and skin details", "eyes": "eyes, eye shape and eye color", "lips": "lips, mouth shape and mouth details", "side_profile": "side profile and facial silhouette", "hairstyle": "hairstyle and hair details", "full_body": "body proportions and full-body appearance", "body_shape": "body build, silhouette and proportions", "costume": "wardrobe and costume", "hands": "hands and hand details", "footwear": "footwear and lower-body styling", "prop": "associated prop or accessory"}
    directions = []
    prompt = str(payload.get("prompt", ""))
    payload["_character_source_prompt"] = prompt
    for character in characters:
        name = character["name"]
        prompt = re.sub(r"@" + re.escape(name) + r"\b", name, prompt, flags=re.IGNORECASE)
        refs = []
        for ref in character["references"]:
            if ref["asset_id"] not in image_refs:
                continue
            picture = offset + image_refs.index(ref["asset_id"]) + 1
            detail = role_text.get(ref["role"], "appearance")
            note = f"; {ref['notes']}" if ref.get("notes") else ""
            if reference_style == "pictures":
                refs.append(f"use <Picture {picture}> for {detail}{note}")
            elif reference_style == "sheet":
                refs.append(f"use the supplied reference sheet for {detail}{note}")
            else:
                refs.append(f"use the supplied reference image for {detail}{note}")
        locks = [key.replace("body", "body appearance") for key, enabled in character["locks"].items() if enabled]
        profile = "; ".join(value for value in (character["description"], character["features"]) if value)
        kind = character.get("kind", "character")
        subject_label = "character" if kind == "character" else ("scene element" if kind == "element" else "prop/object")
        direction = f"{name} is the same persistent {subject_label}"
        if refs:
            direction += "; " + ", and ".join(refs)
        if profile:
            direction += f"; {subject_label} profile: {profile}"
        if locks:
            direction += "; strictly preserve " + ", ".join(locks)
        directions.append(direction)
    payload["prompt"] = prompt + "\n\nCharacter identity direction:\n" + "\n".join(f"- {item}" for item in directions)
    payload["character_ids"] = requested

def workflow_presets() -> list[dict]:
    ratios = ["16:9", "9:16", "1:1", "4:3", "3:4", "21:9"]
    video_resolutions = ["240p", "360p", "480p", "720p", "1080p"]
    image_resolutions = video_resolutions + ["1440p", "2160p"]
    return [
        {"id": "flux-image", "label": "Flux Image", "mode": "image", "ratios": ratios, "resolutions": image_resolutions, "references": {"images": 9}, "all_references_optional": True},
        {"id": "z-image-turbo", "label": "Z-Image Turbo — BF16 Quality · 8 Steps", "mode": "image", "ratios": ratios, "resolutions": image_resolutions, "references": {}, "all_references_optional": True},
        {"id": "minimax-h3", "label": "MiniMax H3 — Fast (16 steps)", "mode": "video", "ratios": ratios, "resolutions": video_resolutions, "durations": list(range(1, 31)), "custom_duration": True, "references": {"images": 9, "videos": 2, "audios": 2, "video_audios": 2, "start_frame": 1, "end_frame": 1}, "all_references_optional": True},
        {"id": "minimax-h3-faster", "label": "MiniMax H3 — Faster (10 steps)", "mode": "video", "ratios": ratios, "resolutions": video_resolutions, "durations": list(range(1, 31)), "custom_duration": True, "references": {"images": 9, "videos": 2, "audios": 2, "video_audios": 2, "start_frame": 1, "end_frame": 1}, "all_references_optional": True},
        {"id": "minimax-h3-superfast", "label": "MiniMax H3 — Superfast (4 steps)", "mode": "video", "ratios": ratios, "resolutions": video_resolutions, "durations": list(range(1, 31)), "custom_duration": True, "references": {"images": 9, "videos": 2, "audios": 2, "video_audios": 2, "start_frame": 1, "end_frame": 1}, "all_references_optional": True},
        {"id": "minimax-h3-best", "label": "MiniMax H3 — Best (20 steps)", "mode": "video", "ratios": ratios, "resolutions": video_resolutions, "durations": list(range(1, 31)), "custom_duration": True, "references": {"images": 9, "videos": 2, "audios": 2, "video_audios": 2, "start_frame": 1, "end_frame": 1}, "all_references_optional": True},
        {"id": "minimax-h3-bf16", "label": "MiniMax H3 — REF2VA BF16 Identity (20 steps)", "mode": "video", "ratios": ratios, "resolutions": video_resolutions, "durations": list(range(1, 31)), "custom_duration": True, "references": {"images": 9, "videos": 2, "audios": 2, "video_audios": 2, "start_frame": 1, "end_frame": 1}, "all_references_optional": True},
        {"id": "minimax-h3-teacache", "label": "MiniMax H3 — TeaCache Balanced (20 steps)", "mode": "video", "ratios": ratios, "resolutions": video_resolutions, "durations": list(range(1, 31)), "custom_duration": True, "references": {"images": 9, "videos": 2, "audios": 2, "video_audios": 2, "start_frame": 1, "end_frame": 1}, "all_references_optional": True},
        {"id": "minimax-h3-pdd", "label": "MiniMax H3 — PDD Acc (8 steps)", "mode": "video", "ratios": ratios, "resolutions": video_resolutions, "durations": list(range(1, 31)), "custom_duration": True, "references": {"images": 9, "videos": 2, "audios": 2, "video_audios": 2, "start_frame": 1, "end_frame": 1}, "all_references_optional": True},
        {"id": "minimax-h3-video-turbo", "label": "MiniMax H3 — Video Turbo HD (8-step + Latent Upscale)", "mode": "video", "ratios": ratios, "resolutions": ["480p", "720p", "1080p"], "durations": list(range(1, 31)), "custom_duration": True, "references": {"images": 9, "videos": 2, "audios": 2, "video_audios": 2, "start_frame": 1, "end_frame": 1}, "all_references_optional": True},
        {"id": "minimax-h3-cinema-lab", "label": "MiniMax H3 — Cinema Lab · Native 720p + 1080p Finish", "mode": "video", "ratios": ["16:9"], "resolutions": ["720p"], "durations": list(range(1, 31)), "custom_duration": True, "references": {"images": 9, "videos": 2, "audios": 2, "video_audios": 2, "start_frame": 1, "end_frame": 1}, "all_references_optional": True},
        {"id": "minimax-h3-cinema-consistency", "label": "Cinema Consistency — PDD8 · Native 720p / 1080p Finish", "mode": "video", "ratios": ["16:9"], "resolutions": ["720p", "1080p"], "durations": [3], "custom_duration": False, "references": {"images": 9, "videos": 2, "audios": 2, "video_audios": 2, "start_frame": 1, "end_frame": 1}, "all_references_optional": True},
        {"id": "indicf5-tts", "label": "IndicF5 — Bangla & English Voice Clone", "mode": "tts", "ratios": [], "resolutions": [], "references": {"reference_audio": 1}, "all_references_optional": False},
        {"id": "acestep-music", "label": "ACE-Step 1.5 — Local Music", "mode": "music", "ratios": ["N/A"], "resolutions": ["Standard"], "durations": list(range(5, 181, 5)), "custom_duration": True, "references": {}, "all_references_optional": True},
        {"id": "minimax-h3-edit", "label": "MiniMax H3 Video Edit — Fast", "mode": "edit", "ratios": ratios, "resolutions": video_resolutions, "durations": list(range(1, 31)), "custom_duration": True, "references": {"images": 9, "source_media": 1}, "all_references_optional": False},
        {"id": "minimax-h3-edit-best", "label": "MiniMax H3 Video Edit — Best", "mode": "edit", "ratios": ratios, "resolutions": video_resolutions, "durations": list(range(1, 31)), "custom_duration": True, "references": {"images": 9, "source_media": 1}, "all_references_optional": False},
        {"id": "ltx-2.5", "label": "LTX 2.5 — Local · Audio + Video", "mode": "video", "ratios": ratios, "resolutions": video_resolutions, "durations": list(range(1, 31)), "custom_duration": True, "references": {"images": 1, "start_frame": 1}, "all_references_optional": True},
        {"id": "realesrgan-photo", "label": "[Image] Real-ESRGAN Photo — Fast · Low VRAM · Default", "mode": "upscale", "media": ["image"], "resolutions": ["1080p", "1440p", "2160p"], "references": {"source_media": 1}, "all_references_optional": False},
        {"id": "realesrgan-anime", "label": "[Image] Real-ESRGAN Anime — Fast · Low VRAM", "mode": "upscale", "media": ["image"], "resolutions": ["1080p", "1440p", "2160p"], "references": {"source_media": 1}, "all_references_optional": False},
        {"id": "realesrgan-video-fast", "label": "[Video] Real-ESRGAN — Fast · Low VRAM · Default", "mode": "upscale", "media": ["video"], "resolutions": ["1080p", "1440p", "2160p"], "references": {"source_media": 1}, "all_references_optional": False},
        {"id": "seedvr-upscale", "label": "[Image + Video] SeedVR2 — Best Quality · Slow · High VRAM", "mode": "upscale", "media": ["image", "video"], "resolutions": ["1080p", "1440p", "2160p"], "references": {"source_media": 1}, "all_references_optional": False},
    ]


RESTRICTED_MODELS: set[str] = set()
ALL_RESOLUTIONS = ("240p", "360p", "480p", "720p", "1080p", "1440p", "2160p")
DEFAULT_MEMBER_RESOLUTIONS = {"240p", "360p", "480p", "720p", "1080p"}


def model_access_for(user: sqlite3.Row) -> set[str]:
    model_ids = {preset["id"] for preset in workflow_presets()}
    if user["role"] == "admin":
        return model_ids
    with connect() as connection:
        rows = connection.execute("SELECT preset,enabled FROM user_model_access WHERE user_id=?", (user["id"],)).fetchall()
    overrides = {row["preset"]: bool(row["enabled"]) for row in rows}
    return {preset for preset in model_ids if overrides.get(preset, preset not in RESTRICTED_MODELS)}


def resolution_access_for(user: sqlite3.Row) -> set[str]:
    if user["role"] == "admin":
        return set(ALL_RESOLUTIONS)
    with connect() as connection:
        rows = connection.execute("SELECT resolution,enabled FROM user_resolution_access WHERE user_id=?", (user["id"],)).fetchall()
    overrides = {row["resolution"]: bool(row["enabled"]) for row in rows}
    return {resolution for resolution in ALL_RESOLUTIONS if overrides.get(resolution, resolution in DEFAULT_MEMBER_RESOLUTIONS)}


def model_resolution_matrix_for(user: sqlite3.Row) -> dict[str, set[str]]:
    """Resolve every model's effective resolutions with legacy permissions as defaults."""
    presets = workflow_presets()
    if user["role"] == "admin":
        return {preset["id"]: set(preset.get("resolutions") or []) for preset in presets}
    legacy = resolution_access_for(user)
    with connect() as connection:
        rows = connection.execute("SELECT preset,resolution,enabled FROM user_model_resolution_access WHERE user_id=?", (user["id"],)).fetchall()
    overrides = {(row["preset"], row["resolution"]): bool(row["enabled"]) for row in rows}
    return {
        preset["id"]: {
            resolution for resolution in (preset.get("resolutions") or [])
            if overrides.get((preset["id"], resolution), resolution in legacy)
        }
        for preset in presets
    }


def model_resolution_access_for(user: sqlite3.Row, preset: dict) -> set[str]:
    return model_resolution_matrix_for(user).get(preset["id"], set())


class ApiError(Exception):
    def __init__(self, status: int, message: str):
        self.status, self.message = status, message


class StudioHandler(SimpleHTTPRequestHandler):
    server_version = "TanjirCreatorStudio/0.1"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(WEB_ROOT), **kwargs)

    def end_headers(self) -> None:
        clean_path = self.path.split("?", 1)[0]
        if clean_path in {"/", "/index.html"} or clean_path.endswith((".js", ".css")):
            self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
            self.send_header("Pragma", "no-cache")
            self.send_header("Expires", "0")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "same-origin")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
        self.send_header("Content-Security-Policy", "default-src 'self'; img-src 'self' data: blob:; media-src 'self' blob:; style-src 'self'; script-src 'self'; connect-src 'self'")
        super().end_headers()

    def do_GET(self) -> None:
        self.dispatch(self.get_request)

    def do_POST(self) -> None:
        self.dispatch(self.post_request)

    def do_PATCH(self) -> None:
        self.dispatch(self.patch_request)

    def do_DELETE(self) -> None:
        self.dispatch(self.delete_request)

    def dispatch(self, callback) -> None:
        try:
            callback()
        except ApiError as error:
            self.send_json(error.status, {"error": error.message})
        except (BrokenPipeError, ConnectionResetError):
            return
        except Exception as error:
            error_id = uuid.uuid4().hex[:12]
            detail = "".join(traceback.format_exception(type(error), error, error.__traceback__))
            print(f"Unexpected request error [{error_id}] on {self.command} {self.path}:\n{detail}", file=sys.stderr, flush=True)
            try:
                with (DATA_ROOT / "generation-errors.log").open("a", encoding="utf-8") as log:
                    log.write(f"[{now()}] [{error_id}] {self.command} {self.path}\n{detail}\n")
            except OSError:
                pass
            self.send_json(500, {"error": f"Unexpected server error (reference: {error_id})"})

    def get_request(self) -> None:
        clean_path = self.path.split("?", 1)[0]
        if clean_path.startswith("/assets/cinema/"):
            content = (WEB_ROOT / "preview-placeholder.svg").read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "image/svg+xml")
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)
            return
        if clean_path == "/api/setup/status":
            with connect() as connection:
                ready = connection.execute("SELECT 1 FROM users WHERE role='admin' LIMIT 1").fetchone() is not None
            return self.send_json(200, {"ready": ready, "requires_bootstrap": (not ready and BOOTSTRAP_TOKEN_PATH.exists())})
        if clean_path == "/api/me":
            user, csrf = self.require_auth()
            return self.send_json(200, {"user": public_user(user), "csrf": csrf})
        if clean_path == "/api/presets":
            user, _ = self.require_auth()
            allowed = model_access_for(user)
            resolution_matrix = model_resolution_matrix_for(user)
            presets = []
            for preset in workflow_presets():
                if preset["id"] not in allowed:
                    continue
                item = dict(preset)
                item["resolutions"] = [value for value in preset.get("resolutions", []) if value in resolution_matrix.get(preset["id"], set())]
                if preset.get("resolutions") and not item["resolutions"]:
                    continue
                presets.append(item)
            return self.send_json(200, {"presets": presets})
        if clean_path == "/api/projects":
            user, _ = self.require_auth()
            with connect() as connection:
                if connection.execute("SELECT 1 FROM projects WHERE owner_id=? LIMIT 1", (user["id"],)).fetchone() is None:
                    timestamp = now()
                    connection.execute("INSERT INTO projects VALUES(?,?,?,'',?,?)", (str(uuid.uuid4()), user["id"], "My First Project", timestamp, timestamp))
                if user["role"] == "admin":
                    online_cutoff = (datetime.now(timezone.utc) - timedelta(seconds=45)).isoformat()
                    rows = connection.execute("SELECT p.*,u.username owner_username,COUNT(a.id) asset_count,EXISTS(SELECT 1 FROM sessions active_session WHERE active_session.user_id=p.owner_id AND active_session.expires_at>? AND active_session.last_seen>=?) owner_online FROM projects p JOIN users u ON u.id=p.owner_id LEFT JOIN assets a ON a.project_id=p.id GROUP BY p.id ORDER BY (p.owner_id=?) DESC,u.username COLLATE NOCASE,p.updated_at DESC", (now(), online_cutoff, user["id"])).fetchall()
                else:
                    rows = connection.execute("SELECT p.*,u.username owner_username,COUNT(a.id) asset_count FROM projects p JOIN users u ON u.id=p.owner_id LEFT JOIN assets a ON a.project_id=p.id WHERE p.owner_id=? GROUP BY p.id ORDER BY p.updated_at DESC", (user["id"],)).fetchall()
            return self.send_json(200, {"projects": [{**dict(row), "can_edit": row["owner_id"] == user["id"] or user["role"] == "admin"} for row in rows]})
        if clean_path == "/api/director/conversations" or clean_path.startswith("/api/director/conversations/"):
            user, _ = self.require_auth()
            self.require_admin(user)
            try:
                request = Request(f"{DIRECTOR_URL}{clean_path}", headers={"Accept": "application/json"})
                with urlopen(request, timeout=10) as response:
                    return self.send_json(200, json.loads(response.read()))
            except HTTPError as error:
                raise ApiError(error.code, "Director conversation was not found")
            except (URLError, TimeoutError, ValueError) as error:
                raise ApiError(503, f"Director history is unavailable: {error}")
        if clean_path == "/api/characters":
            user, _ = self.require_auth()
            project_id = self.query("project_id")
            self.project_for(user, project_id)
            with connect() as connection:
                characters = load_project_characters(connection, str(project_id))
            return self.send_json(200, {"characters": characters})
        if clean_path == "/api/gallery":
            user, _ = self.require_auth()
            scope = self.query("scope")
            project_id = self.query("project_id")
            if scope != "mine":
                self.project_for(user, project_id)
            with connect() as connection:
                if scope == "mine":
                    rows = connection.execute("SELECT a.*,j.request_json,u.username owner_username,p.name project_name,EXISTS(SELECT 1 FROM asset_favorites af WHERE af.asset_id=a.id AND af.user_id=?) favorite_for_user FROM assets a JOIN users u ON u.id=a.owner_id JOIN projects p ON p.id=a.project_id LEFT JOIN jobs j ON j.prompt_id=a.source AND j.project_id=a.project_id WHERE a.owner_id=? ORDER BY a.created_at DESC", (user["id"], user["id"])).fetchall()
                else:
                    rows = connection.execute("SELECT a.*,j.request_json,u.username owner_username,p.name project_name,EXISTS(SELECT 1 FROM asset_favorites af WHERE af.asset_id=a.id AND af.user_id=?) favorite_for_user FROM assets a JOIN users u ON u.id=a.owner_id JOIN projects p ON p.id=a.project_id LEFT JOIN jobs j ON j.prompt_id=a.source AND j.project_id=a.project_id WHERE a.project_id=? ORDER BY a.created_at DESC", (user["id"], project_id)).fetchall()
            assets = []
            for row in rows:
                item = dict(row)
                raw_request = item.pop("request_json", None)
                try:
                    recreate = json.loads(raw_request) if raw_request else None
                except (TypeError, ValueError):
                    recreate = None
                item["recreate"] = recreate if isinstance(recreate, dict) and item["source"] != "upload" else None
                item["favorite"] = bool(item.pop("favorite_for_user", item.get("favorite")))
                item["published"] = bool(item.get("published"))
                item["is_shared"] = False
                item["can_manage"] = item["owner_id"] == user["id"] or user["role"] == "admin"
                item["can_publish"] = item["can_manage"] and item["source"] != "upload" and item["kind"] in {"image", "video"}
                item["url"] = f"/media/{row['id']}"
                item["thumbnail_url"] = f"/thumbnail/{row['id']}" if item["kind"] in {"image", "video"} else item["url"]
                assets.append(item)
            return self.send_json(200, {"assets": assets})
        if clean_path == "/api/public-gallery":
            user, _ = self.require_auth()
            with connect() as connection:
                rows = connection.execute("SELECT a.*,j.request_json,u.username owner_username,EXISTS(SELECT 1 FROM asset_favorites af WHERE af.asset_id=a.id AND af.user_id=?) favorite_for_user FROM assets a JOIN users u ON u.id=a.owner_id LEFT JOIN jobs j ON j.prompt_id=a.source AND j.project_id=a.project_id WHERE a.published=1 AND a.source<>'upload' AND a.kind IN ('image','video') ORDER BY a.created_at DESC", (user["id"],)).fetchall()
            assets = []
            for row in rows:
                item = dict(row)
                raw_request = item.pop("request_json", None)
                try:
                    recreate = json.loads(raw_request) if raw_request else None
                except (TypeError, ValueError):
                    recreate = None
                item["recreate"] = recreate if isinstance(recreate, dict) else None
                item["favorite"] = bool(item.pop("favorite_for_user", False))
                item["published"] = True
                item["is_shared"] = item["owner_id"] != user["id"]
                item["can_manage"] = item["owner_id"] == user["id"] or user["role"] == "admin"
                item["can_publish"] = item["can_manage"]
                item["url"] = f"/media/{row['id']}"
                item["thumbnail_url"] = f"/thumbnail/{row['id']}" if item["kind"] in {"image", "video"} else item["url"]
                assets.append(item)
            return self.send_json(200, {"assets": assets})
        if clean_path == "/api/team/jobs":
            user, _ = self.require_auth()
            with connect() as connection:
                groups = connection.execute("SELECT j.owner_id,j.project_id,MAX(j.created_at) latest,MAX(CASE WHEN j.status IN ('queued','running') THEN 1 ELSE 0 END) active FROM jobs j GROUP BY j.owner_id,j.project_id ORDER BY active DESC,latest DESC LIMIT 12").fetchall()
            jobs = []
            for group in groups:
                jobs.extend(refresh_jobs(DB_PATH, DATA_ROOT, COMFY_OUTPUT, COMFY_URL, group["owner_id"], group["project_id"], now()))
            if jobs:
                marks = ",".join("?" for _ in jobs)
                with connect() as connection:
                    details = connection.execute(f"SELECT j.id,j.project_id,COALESCE(j.created_by,j.owner_id) created_by,p.name project_name,u.username FROM jobs j JOIN projects p ON p.id=j.project_id JOIN users u ON u.id=COALESCE(j.created_by,j.owner_id) WHERE j.id IN ({marks})", tuple(job["id"] for job in jobs)).fetchall()
                by_id = {row["id"]: dict(row) for row in details}
                for job in jobs:
                    detail = by_id.get(job["id"], {})
                    job.update({"project_id": detail.get("project_id"), "project_name": detail.get("project_name", "Project"), "username": detail.get("username", "User"), "can_cancel": user["role"] == "admin" or detail.get("created_by") == user["id"]})
            jobs.sort(key=lambda job: (job["status"] in {"queued", "running"}, job["created_at"]), reverse=True)
            return self.send_json(200, {"jobs": jobs[:10]})
        if clean_path == "/api/jobs":
            user, _ = self.require_auth()
            project_id = self.query("project_id")
            project = self.project_for(user, project_id)
            jobs = refresh_jobs(DB_PATH, DATA_ROOT, COMFY_OUTPUT, COMFY_URL, project["owner_id"], project["id"], now())
            return self.send_json(200, {"jobs": jobs})
        if clean_path == "/api/admin/users":
            user, _ = self.require_auth()
            self.require_admin(user)
            with connect() as connection:
                rows = connection.execute("SELECT * FROM users ORDER BY role, username COLLATE NOCASE").fetchall()
            presets = workflow_presets()
            users = [{**public_user(row), "model_access": sorted(model_access_for(row)), "model_resolution_access": {key: sorted(value) for key, value in model_resolution_matrix_for(row).items()}} for row in rows]
            models = [{"id": preset["id"], "label": preset["label"], "mode": preset["mode"], "media": preset.get("media", []), "resolutions": preset.get("resolutions", [])} for preset in presets]
            resolutions = [{"id": value, "label": "4K" if value == "2160p" else value} for value in ALL_RESOLUTIONS]
            return self.send_json(200, {"users": users, "models": models, "resolutions": resolutions})
        if clean_path == "/api/system/metrics":
            self.require_auth()
            return self.send_json(200, system_metrics())
        if clean_path == "/api/director/engine":
            user, _ = self.require_auth()
            self.require_admin(user)
            try:
                with urlopen(Request(f"{DIRECTOR_URL}/api/health", headers={"Accept": "application/json"}), timeout=5) as response:
                    return self.send_json(200, json.loads(response.read()))
            except (HTTPError, URLError, TimeoutError, ValueError) as error:
                raise ApiError(503, f"Local Qwen controller is unavailable: {error}")
        if clean_path == "/api/comfy/status":
            self.require_auth()
            try:
                with urlopen(Request(f"{COMFY_URL}/system_stats", headers={"Accept": "application/json"}), timeout=2) as response:
                    stats = json.loads(response.read(1_000_000))
                return self.send_json(200, {"online": True, "stats": stats})
            except (URLError, TimeoutError, ValueError):
                return self.send_json(200, {"online": False})
        if clean_path.startswith("/thumbnail/"):
            return self.serve_thumbnail(clean_path.rsplit("/", 1)[-1])
        if clean_path.startswith("/media/"):
            return self.serve_media(clean_path.rsplit("/", 1)[-1])
        if clean_path.startswith("/api/"):
            raise ApiError(404, "Not found")
        return super().do_GET()

    def post_request(self) -> None:
        clean_path = self.path.split("?", 1)[0]
        if clean_path == "/api/setup/admin":
            payload = self.read_json()
            with connect() as connection:
                if connection.execute("SELECT 1 FROM users WHERE role='admin' LIMIT 1").fetchone():
                    raise ApiError(409, "Studio is already configured")
                if not BOOTSTRAP_TOKEN_PATH.exists():
                    raise ApiError(403, "Administrator setup is locked")
                expected_token = BOOTSTRAP_TOKEN_PATH.read_text(encoding="utf-8").strip()
                supplied_token = str(payload.get("bootstrap_token", "")).strip()
                if not expected_token or not secrets.compare_digest(supplied_token, expected_token):
                    raise ApiError(403, "Invalid setup code")
                username, password = self.credentials(payload)
                timestamp, user_id = now(), str(uuid.uuid4())
                connection.execute("INSERT INTO users VALUES(?,?,'admin',?,1,?,?)", (user_id, username, hash_password(password), timestamp, timestamp))
                connection.execute("INSERT INTO projects VALUES(?,?,?,'',?,?)", (str(uuid.uuid4()), user_id, "My First Project", timestamp, timestamp))
            BOOTSTRAP_TOKEN_PATH.unlink(missing_ok=True)
            return self.create_session(user_id)
        if clean_path == "/api/login":
            payload = self.read_json()
            with connect() as connection:
                user = connection.execute("SELECT * FROM users WHERE username=? COLLATE NOCASE", (str(payload.get("username", "")).strip(),)).fetchone()
            if not user or not user["enabled"] or not check_password(str(payload.get("password", "")), user["password_hash"]):
                raise ApiError(401, "Invalid username or password")
            return self.create_session(user["id"], remember=bool(payload.get("remember_me")))

        user, csrf = self.require_auth(csrf=True)
        if clean_path == "/api/director/engine":
            self.require_admin(user)
            payload = self.read_json()
            endpoint = "on" if bool(payload.get("enabled")) else "off"
            try:
                request = Request(f"{DIRECTOR_URL}/api/engine/{endpoint}", data=b"{}", headers={"Content-Type": "application/json"}, method="POST")
                with urlopen(request, timeout=240) as response:
                    return self.send_json(200, json.loads(response.read()))
            except HTTPError as error:
                try:
                    detail = json.loads(error.read()).get("error", str(error))
                except (ValueError, AttributeError):
                    detail = str(error)
                raise ApiError(503, f"Local Qwen controller is unavailable: {detail}")
            except (URLError, TimeoutError, ValueError) as error:
                raise ApiError(503, f"Local Qwen controller is unavailable: {error}")
        if clean_path == "/api/logout":
            token = self.session_token()
            with connect() as connection:
                connection.execute("DELETE FROM sessions WHERE token_hash=?", (sha256(token.encode()).hexdigest(),))
            return self.clear_session()
        if clean_path == "/api/director/chat":
            self.require_admin(user)
            payload = self.read_json()
            message = str(payload.get("message", "")).strip()
            project_id = str(payload.get("project_id", "")).strip()
            if not 1 <= len(message) <= 12000:
                raise ApiError(400, "Director message must be 1-12000 characters")
            selected_project = self.project_for(user, project_id)
            scope_project_id = str(payload.get("scope_project_id", "")).strip()
            message_folded = message.casefold()
            scope_mode = "general"
            with connect() as connection:
                available_projects = connection.execute(
                    "SELECT * FROM projects WHERE owner_id=? ORDER BY updated_at DESC",
                    (user["id"],),
                ).fetchall()
                project = self.project_for(user, scope_project_id) if scope_project_id else None
                if project is not None:
                    scope_mode = "project"
                for candidate in ([] if project is not None else sorted(available_projects, key=lambda item: len(str(item["name"])), reverse=True)):
                    name = str(candidate["name"]).strip()
                    if len(name) >= 3 and name.casefold() in message_folded:
                        project = candidate
                        scope_mode = "project"
                        break
                current_markers = ("current project", "selected project", "এই প্রজেক্ট", "এই project", "বর্তমান প্রজেক্ট", "বর্তমান project")
                if project is None and any(marker in message_folded for marker in current_markers):
                    project = selected_project
                    scope_mode = "project"
                if project is None:
                    project = next((item for item in available_projects if str(item["name"]).casefold() == "quick creations"), None)
                    if project is None:
                        timestamp = now()
                        quick = {
                            "id": str(uuid.uuid4()), "owner_id": user["id"], "name": "Quick Creations",
                            "description": "Standalone images and videos created by AI Cinema Director.",
                            "created_at": timestamp, "updated_at": timestamp,
                        }
                        connection.execute("INSERT INTO projects VALUES(:id,:owner_id,:name,:description,:created_at,:updated_at)", quick)
                        project = connection.execute("SELECT * FROM projects WHERE id=?", (quick["id"],)).fetchone()
            with connect() as connection:
                characters = load_project_characters(connection, project["id"]) if scope_mode == "project" else []
                asset_rows = connection.execute("SELECT id,kind,filename,source,created_at FROM assets WHERE project_id=? ORDER BY created_at DESC LIMIT 60", (project["id"],)).fetchall() if scope_mode == "project" else []
                job_rows = connection.execute("SELECT id,preset,status,error,created_at,updated_at FROM jobs WHERE project_id=? ORDER BY created_at DESC LIMIT 15", (project["id"],)).fetchall() if scope_mode == "project" else []
            project_context = {
                "scope": {"mode": scope_mode, "destination": project["name"]},
                "project": dict(project),
                "available_projects": [{"id": item["id"], "name": item["name"]} for item in available_projects if str(item["name"]).casefold() != "quick creations"],
                "characters_elements_props": characters,
                "recent_assets": [dict(row) for row in asset_rows],
                "recent_jobs": [dict(row) for row in job_rows],
                "available_models": [{"id": item["id"], "label": item["label"], "mode": item["mode"]} for item in workflow_presets()],
            }
            try:
                comfy_queue = json.loads(urlopen(Request(f"{COMFY_URL}/queue", headers={"Accept": "application/json"}), timeout=3).read())
                if comfy_queue.get("queue_running"):
                    raise ApiError(409, "A ComfyUI generation is using the GPU. The Cinema Director will be ready automatically when generation finishes; the live GPU graph shows its load.")
            except ApiError:
                raise
            except (HTTPError, URLError, TimeoutError, ValueError):
                pass
            director_payload = json.dumps({"message": message, "conversation_id": str(payload.get("conversation_id", "")) or None, "project_id": project["id"], "project_context": project_context}, ensure_ascii=False).encode("utf-8")
            try:
                request = Request(f"{DIRECTOR_URL}/api/director/chat", data=director_payload, headers={"Content-Type": "application/json"}, method="POST")
                with urlopen(request, timeout=900) as response:
                    result = json.loads(response.read())
            except HTTPError as error:
                try:
                    detail = json.loads(error.read()).get("error", str(error))
                except (ValueError, AttributeError):
                    detail = str(error)
                raise ApiError(503, f"Local Cinema Director is unavailable: {detail}")
            except (URLError, TimeoutError, ValueError) as error:
                raise ApiError(503, f"Local Cinema Director is unavailable: {error}")
            if result.get("error"):
                raise ApiError(503, str(result["error"]))
            queued = []
            safe_fields = {"prompt", "preset", "ratio", "resolution", "duration", "character_ids", "image_refs", "audio_refs"}
            if result.get("actions"):
                try:
                    release_request = Request(f"{DIRECTOR_URL}/api/engine/release", data=b"{}", headers={"Content-Type": "application/json"}, method="POST")
                    with urlopen(release_request, timeout=20):
                        pass
                except (HTTPError, URLError, TimeoutError):
                    pass
            for raw_action in list(result.get("actions") or [])[:8]:
                action = {key: value for key, value in dict(raw_action).items() if key in safe_fields}
                action.update({
                    "project_id": project["id"],
                    "duration": max(1, min(30, float(action.get("duration", 5) or 5))),
                    "start_frame": None, "end_frame": None, "source_media": None,
                    "edit_mask": None, "edit_placement_mask": None,
                    "video_refs": [], "video_audio_refs": [],
                    "reference_order": list(action.get("image_refs") or []),
                })
                generate_body = json.dumps(action, ensure_ascii=False).encode("utf-8")
                generate_request = Request(
                    f"http://127.0.0.1:{PORT}/api/generate",
                    data=generate_body,
                    headers={"Content-Type": "application/json", "Cookie": self.headers.get("Cookie", ""), "X-CSRF-Token": csrf},
                    method="POST",
                )
                try:
                    with urlopen(generate_request, timeout=120) as response:
                        queued.append(json.loads(response.read()))
                except HTTPError as error:
                    try:
                        detail = json.loads(error.read()).get("error", str(error))
                    except (ValueError, AttributeError):
                        detail = str(error)
                    queued.append({"error": detail})
            if queued:
                successes = sum(1 for item in queued if not item.get("error"))
                failures = [item["error"] for item in queued if item.get("error")]
                result["queued"] = queued
                if successes:
                    result["answer"] = (str(result.get("answer", "")).strip() + f"\n\n✓ Studio queue-তে {successes}টি generation যোগ হয়েছে.").strip()
                if failures:
                    result["answer"] = (str(result.get("answer", "")).strip() + "\n\nQueue error: " + "; ".join(failures)).strip()
            return self.send_json(200, result)
        if clean_path == "/api/projects":
            payload = self.read_json()
            name = str(payload.get("name", "")).strip()
            if not 1 <= len(name) <= 80:
                raise ApiError(400, "Project name must be 1-80 characters")
            timestamp = now()
            project = {"id": str(uuid.uuid4()), "owner_id": user["id"], "name": name, "description": str(payload.get("description", ""))[:500], "created_at": timestamp, "updated_at": timestamp}
            with connect() as connection:
                connection.execute("INSERT INTO projects VALUES(:id,:owner_id,:name,:description,:created_at,:updated_at)", project)
            return self.send_json(201, {"project": project})
        if clean_path == "/api/admin/users":
            self.require_admin(user)
            payload = self.read_json()
            username, password = self.credentials(payload)
            timestamp = now()
            target = (str(uuid.uuid4()), username, "member", hash_password(password), 1, timestamp, timestamp)
            try:
                with connect() as connection:
                    connection.execute("INSERT INTO users VALUES(?,?,?,?,?,?,?)", target)
                    connection.execute("INSERT INTO projects VALUES(?,?,?,'',?,?)", (str(uuid.uuid4()), target[0], "My First Project", timestamp, timestamp))
            except sqlite3.IntegrityError:
                raise ApiError(409, "Username already exists")
            return self.send_json(201, {"user": {"id": target[0], "username": username, "role": "member", "enabled": True, "created_at": timestamp}})
        if clean_path == "/api/characters":
            payload = self.read_json()
            project = self.project_for(user, str(payload.get("project_id", "")))
            name = str(payload.get("name", "")).strip()
            if not 1 <= len(name) <= 60:
                raise ApiError(400, "Character name must be 1-60 characters")
            timestamp = now()
            character_id = str(uuid.uuid4())
            locks = character_locks(payload.get("locks"))
            kind = str(payload.get("kind", "character")).strip().lower()
            if kind not in {"character", "element", "prop"}:
                raise ApiError(400, "Asset type must be character, element, or prop")
            try:
                with connect() as connection:
                    if connection.execute("SELECT 1 FROM characters WHERE owner_id=? AND name=? COLLATE NOCASE", (project["owner_id"], name)).fetchone():
                        raise ApiError(409, "A reusable library item with this name already exists")
                    connection.execute("INSERT INTO characters(id,owner_id,project_id,kind,name,description,features,locks_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)", (character_id, project["owner_id"], None, kind, name, str(payload.get("description", ""))[:1000], str(payload.get("features", ""))[:1000], json.dumps(locks, separators=(",", ":")), timestamp, timestamp))
                    character = load_project_characters(connection, project["id"], [character_id])[0]
            except sqlite3.IntegrityError:
                raise ApiError(409, "A reusable library item with this name already exists")
            return self.send_json(201, {"character": character})
        if clean_path == "/api/character-references":
            payload = self.read_json()
            project = self.project_for(user, str(payload.get("project_id", "")))
            character_id, asset_id = str(payload.get("character_id", "")), str(payload.get("asset_id", ""))
            role = str(payload.get("role", "identity"))
            if role not in CHARACTER_REFERENCE_ROLES:
                raise ApiError(400, "Invalid character reference role")
            timestamp, reference_id = now(), str(uuid.uuid4())
            with connect() as connection:
                character = connection.execute("SELECT id FROM characters WHERE id=? AND owner_id=?", (character_id, project["owner_id"])).fetchone()
                asset = connection.execute("SELECT id FROM assets WHERE id=? AND project_id=? AND kind='image'", (asset_id, project["id"])).fetchone()
                if not character or not asset:
                    raise ApiError(404, "Character or image reference not found")
                connection.execute("INSERT INTO character_references(id,character_id,asset_id,role,identity_lock,notes,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(character_id,asset_id) DO UPDATE SET role=excluded.role,identity_lock=excluded.identity_lock,notes=excluded.notes,updated_at=excluded.updated_at", (reference_id, character_id, asset_id, role, 1 if payload.get("identity_lock", True) else 0, str(payload.get("notes", ""))[:500], timestamp, timestamp))
                saved = connection.execute("SELECT cr.*,a.filename,a.mime_type FROM character_references cr JOIN assets a ON a.id=cr.asset_id WHERE cr.character_id=? AND cr.asset_id=?", (character_id, asset_id)).fetchone()
            return self.send_json(201, {"reference": {"id": saved["id"], "asset_id": saved["asset_id"], "role": saved["role"], "identity_lock": bool(saved["identity_lock"]), "notes": saved["notes"], "filename": saved["filename"], "mime_type": saved["mime_type"], "url": f"/media/{saved['asset_id']}", "thumbnail_url": f"/thumbnail/{saved['asset_id']}"}})
        if clean_path == "/api/uploads":
            project_id = self.query("project_id")
            project = self.project_for(user, project_id)
            return self.receive_upload(user, project_id, owner_id=project["owner_id"])
        if clean_path.startswith("/api/jobs/") and clean_path.endswith("/cancel"):
            job_id = clean_path.split("/")[-2]
            with connect() as connection:
                job = connection.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            if not job:
                raise ApiError(404, "Job not found")
            self.project_for(user, job["project_id"])
            if job["status"] not in {"queued", "running"}:
                raise ApiError(409, "This generation is no longer running")
            try:
                queue = json.loads(urlopen(Request(f"{COMFY_URL}/queue", headers={"Accept": "application/json"}), timeout=5).read())
                running_ids = {str(item[1]) for item in queue.get("queue_running", []) if len(item) > 1}
                pending_ids = {str(item[1]) for item in queue.get("queue_pending", []) if len(item) > 1}
                if job["prompt_id"] in pending_ids:
                    body = json.dumps({"delete": [job["prompt_id"]]}).encode()
                    urlopen(Request(f"{COMFY_URL}/queue", data=body, headers={"Content-Type": "application/json"}, method="POST"), timeout=10).read()
                elif job["prompt_id"] in running_ids:
                    urlopen(Request(f"{COMFY_URL}/interrupt", data=b"{}", headers={"Content-Type": "application/json"}, method="POST"), timeout=10).read()
                else:
                    timestamp = now()
                    with connect() as connection:
                        connection.execute("UPDATE jobs SET status='failed',error='Generation left the ComfyUI queue',updated_at=? WHERE id=?", (timestamp, job["id"]))
                    return self.send_json(200, {"ok": True, "cancelled": False, "reconciled": True, "prompt_id": job["prompt_id"]})
            except ApiError:
                raise
            except (HTTPError, URLError, TimeoutError, ValueError) as error:
                raise ApiError(502, f"Could not stop ComfyUI generation: {error}")
            timestamp = now()
            with connect() as connection:
                connection.execute("UPDATE jobs SET status='failed',error='Cancelled by user',updated_at=? WHERE id=?", (timestamp, job["id"]))
            return self.send_json(200, {"ok": True, "cancelled": True, "prompt_id": job["prompt_id"]})
        if clean_path == "/api/tts":
            payload = self.read_json()
            project = self.project_for(user, str(payload.get("project_id", "")))
            if "indicf5-tts" not in model_access_for(user):
                raise ApiError(403, "You do not have access to IndicF5 TTS")
            missing = [str(path) for path in (TTS_MODEL, TTS_VOCAB, TTS_PYTHON) if not path.is_file()]
            if missing:
                raise ApiError(503, "IndicF5 is not fully installed: " + ", ".join(missing))
            text = str(payload.get("text", "")).strip()
            reference_text = str(payload.get("reference_text", "")).strip()
            if not 1 <= len(text) <= 3000:
                raise ApiError(400, "Dialogue must be 1-3000 characters")
            if not 3 <= len(reference_text) <= 1200:
                raise ApiError(400, "Reference transcript must be 3-1200 characters")
            if re.search(r"@\w+|character identity direction:|cinematic (?:medium |close-up |wide )?shot|no dialogue|shot mode:|camera direction:", reference_text, re.IGNORECASE):
                raise ApiError(400, "Reference transcript looks like a video prompt. Enter only the exact words spoken in the reference audio.")
            try:
                speed = max(0.6, min(1.5, float(payload.get("speed", 1))))
            except (TypeError, ValueError):
                raise ApiError(400, "Invalid voice speed")
            reference_id = str(payload.get("reference_audio_id", ""))
            with connect() as connection:
                reference = connection.execute("SELECT * FROM assets WHERE id=? AND project_id=? AND owner_id=? AND kind='audio'", (reference_id, project["id"], project["owner_id"])).fetchone()
            if not reference:
                raise ApiError(404, "Reference audio was not found in this project")
            if not reference["filename"].lower().endswith(".wav"):
                raise ApiError(400, "IndicF5 reference audio must be a WAV file")
            reference_path = DATA_ROOT / reference["relative_path"]
            if not reference_path.is_file():
                raise ApiError(404, "Reference WAV file is missing")
            timestamp, job_id, prompt_id = now(), str(uuid.uuid4()), "tts-" + str(uuid.uuid4())
            request_json = json.dumps({"preset": "indicf5-tts", "prompt": text, "reference_audio_id": reference_id, "reference_text": reference_text, "speed": speed, "project_id": project["id"]}, ensure_ascii=False, separators=(",", ":"))
            with connect() as connection:
                connection.execute("INSERT INTO jobs(id,owner_id,created_by,project_id,prompt_id,preset,status,error,created_at,updated_at,request_json) VALUES(?,?,?,?,?,?,'queued','',?,?,?)", (job_id, project["owner_id"], user["id"], project["id"], prompt_id, "indicf5-tts", timestamp, timestamp, request_json))
            queue_tts_job(db_path=DB_PATH, data_root=DATA_ROOT, job_id=job_id, prompt_id=prompt_id, owner_id=project["owner_id"], project_id=project["id"], text=text, reference_audio=reference_path, reference_text=reference_text, speed=speed)
            return self.send_json(202, {"queued": True, "job_id": job_id, "prompt_id": prompt_id})
        if clean_path == "/api/generate":
            payload = self.read_json()
            project = self.project_for(user, str(payload.get("project_id", "")))
            preset = str(payload.get("preset", ""))
            preset_config = next((item for item in workflow_presets() if item["id"] == preset), None)
            if not preset_config:
                raise ApiError(400, "Unknown generation model")
            if preset not in model_access_for(user):
                raise ApiError(403, "You do not have access to this model. Ask an administrator.")
            resolution = str(payload.get("resolution", ""))
            if resolution not in preset_config.get("resolutions", []):
                raise ApiError(400, "This resolution is not supported by the selected model")
            if resolution not in model_resolution_access_for(user, preset_config):
                raise ApiError(403, "You do not have access to this resolution for the selected model. Ask an administrator.")
            if payload.get("character_ids"):
                character_modes = {
                    "minimax-h3": (9, "pictures"),
                    "minimax-h3-faster": (9, "pictures"),
                    "minimax-h3-superfast": (9, "pictures"),
                    "minimax-h3-best": (9, "pictures"),
                    "minimax-h3-bf16": (9, "pictures"),
                    "minimax-h3-teacache": (9, "pictures"),
                    "minimax-h3-pdd": (9, "pictures"),
                    "minimax-h3-cinema-lab": (9, "pictures"),
                    "minimax-h3-cinema-consistency": (9, "pictures"),
                    "minimax-h3-video-turbo": (9, "pictures"),
                    "minimax-h3-edit": (9, "pictures"),
                    "minimax-h3-edit-best": (9, "pictures"),
                    "flux-image": (9, "sheet"),
                    "ltx-2.5": (1, "image"),
                }
                if preset not in character_modes:
                    raise ApiError(400, "Character Identity is not applicable to this model")
                max_character_images, reference_style = character_modes[preset]
                with connect() as connection:
                    compile_character_identity(connection, project["id"], payload, max_character_images, reference_style)
            if preset == "ltx-2.5":
                image_refs = list(payload.get("image_refs") or [])
                if payload.get("end_frame") or payload.get("video_refs") or payload.get("audio_refs") or payload.get("video_audio_refs"):
                    raise ApiError(400, "LTX 2.5 supports prompt plus one optional start/reference image; end, video and audio references are not supported")
                if len(image_refs) + (1 if payload.get("start_frame") else 0) > 1:
                    raise ApiError(400, "LTX 2.5 supports only one start/reference image")
            fields = ("start_frame", "end_frame", "source_media", "edit_mask", "edit_placement_mask")
            requested = {str(payload[key]) for key in fields if payload.get(key)}
            for key in ("image_refs", "video_refs", "audio_refs", "video_audio_refs"):
                requested.update(str(value) for value in (payload.get(key) or []))
            assets = {}
            if requested:
                marks = ",".join("?" for _ in requested)
                with connect() as connection:
                    # Uploads/My Generations expose the signed-in user's entire
                    # library, so references may belong to another own project.
                    rows = connection.execute(f"SELECT * FROM assets WHERE id IN ({marks}) AND ((owner_id=? AND project_id=?) OR owner_id=? OR published=1)", (*requested, project["owner_id"], project["id"], user["id"])).fetchall()
                assets = {row["id"]: row for row in rows}
                if set(assets) != requested:
                    raise ApiError(400, "One or more references are unavailable")
            try:
                client_id = secrets.token_hex(16)
                start_listener(COMFY_URL, client_id)
                result = GenerationService(COMFY_URL, WORKFLOW_ROOT, DATA_ROOT, COMFY_INPUT).queue(payload, assets, project["owner_id"], project["id"], client_id=client_id)
            except GenerationError as error:
                raise ApiError(400, str(error))
            except (HTTPError, URLError, TimeoutError, ValueError) as error:
                detail = error.read().decode(errors="replace")[:500] if isinstance(error, HTTPError) else str(error)
                raise ApiError(502, f"ComfyUI rejected the job: {detail}")
            prompt_id = str(result.get("prompt_id", ""))
            if not prompt_id:
                raise ApiError(502, "ComfyUI did not return a prompt ID")
            timestamp, job_id = now(), str(uuid.uuid4())
            recreate_keys = ("preset", "prompt", "ratio", "resolution", "duration", "lyrics", "instrumental", "vocal_language", "bpm", "keyscale", "start_frame", "end_frame", "source_media", "image_refs", "video_refs", "audio_refs", "video_audio_refs", "reference_order", "character_ids", "video_edit", "edit_mask", "edit_placement_mask")
            recreate_payload = {key: payload.get(key) for key in recreate_keys}
            recreate_payload["prompt"] = payload.get("_character_source_prompt", payload.get("prompt", ""))
            recreate_payload["project_id"] = project["id"]
            request_json = json.dumps(recreate_payload, ensure_ascii=False, separators=(",", ":"))
            with connect() as connection:
                connection.execute("INSERT INTO jobs(id,owner_id,created_by,project_id,prompt_id,preset,status,error,created_at,updated_at,request_json) VALUES(?,?,?,?,?,?,'queued','',?,?,?)", (job_id, project["owner_id"], user["id"], project["id"], prompt_id, str(payload.get("preset", "")), timestamp, timestamp, request_json))
            return self.send_json(202, {"queued": True, "job_id": job_id, **result})
        raise ApiError(404, "Not found")

    def patch_request(self) -> None:
        user, _ = self.require_auth(csrf=True)
        clean_path = self.path.split("?", 1)[0]
        if clean_path == "/api/account/password":
            payload = self.read_json()
            current_password = str(payload.get("current_password", ""))
            new_password = str(payload.get("new_password", ""))
            if not check_password(current_password, user["password_hash"]):
                raise ApiError(401, "Current password is incorrect")
            if len(new_password) < 10:
                raise ApiError(400, "New password must be at least 10 characters")
            if current_password == new_password:
                raise ApiError(400, "New password must be different")
            with connect() as connection:
                connection.execute("UPDATE users SET password_hash=?,updated_at=? WHERE id=?", (hash_password(new_password), now(), user["id"]))
            return self.send_json(200, {"ok": True})
        if clean_path.startswith("/api/assets/") and clean_path.endswith("/publish"):
            asset_id = clean_path.split("/")[-2]
            payload = self.read_json()
            if not isinstance(payload.get("published"), bool):
                raise ApiError(400, "published must be true or false")
            published = payload["published"]
            with connect() as connection:
                asset = connection.execute("SELECT * FROM assets WHERE id=?", (asset_id,)).fetchone()
                if not asset:
                    raise ApiError(404, "Asset not found")
                if asset["owner_id"] != user["id"] and user["role"] != "admin":
                    raise ApiError(403, "Only the owner or admin can publish this asset")
                if asset["source"] == "upload" or asset["kind"] not in {"image", "video"}:
                    raise ApiError(400, "Only generated images and videos can be published")
                connection.execute("UPDATE assets SET published=? WHERE id=?", (1 if published else 0, asset_id))
            return self.send_json(200, {"ok": True, "published": published})
        if clean_path.startswith("/api/assets/") and clean_path.endswith("/favorite"):
            asset_id = clean_path.split("/")[-2]
            payload = self.read_json()
            if not isinstance(payload.get("favorite"), bool):
                raise ApiError(400, "favorite must be true or false")
            favorite = payload["favorite"]
            with connect() as connection:
                asset = connection.execute("SELECT * FROM assets WHERE id=?", (asset_id,)).fetchone()
                if not asset:
                    raise ApiError(404, "Asset not found")
                if asset["owner_id"] != user["id"] and user["role"] != "admin" and not bool(asset["published"]):
                    raise ApiError(404, "Asset not found")
                if favorite:
                    connection.execute("INSERT OR IGNORE INTO asset_favorites(user_id,asset_id,created_at) VALUES(?,?,?)", (user["id"], asset_id, now()))
                else:
                    connection.execute("DELETE FROM asset_favorites WHERE user_id=? AND asset_id=?", (user["id"], asset_id))
            return self.send_json(200, {"ok": True, "favorite": favorite})
        if clean_path.startswith("/api/characters/"):
            character_id = clean_path.rsplit("/", 1)[-1]
            payload = self.read_json()
            with connect() as connection:
                character = connection.execute("SELECT * FROM characters WHERE id=?", (character_id,)).fetchone()
                if not character:
                    raise ApiError(404, "Character not found")
                if character["owner_id"] != user["id"] and user["role"] != "admin":
                    raise ApiError(404, "Character not found")
                name = str(payload.get("name", character["name"])).strip()
                if not 1 <= len(name) <= 60:
                    raise ApiError(400, "Character name must be 1-60 characters")
                try:
                    kind = str(payload.get("kind", character["kind"] if "kind" in character.keys() else "character")).strip().lower()
                    if kind not in {"character", "element", "prop"}:
                        raise ApiError(400, "Asset type must be character, element, or prop")
                    duplicate = connection.execute("SELECT 1 FROM characters WHERE owner_id=? AND id<>? AND name=? COLLATE NOCASE", (character["owner_id"], character_id, name)).fetchone()
                    if duplicate:
                        raise ApiError(409, "A reusable library item with this name already exists")
                    connection.execute("UPDATE characters SET kind=?,name=?,description=?,features=?,locks_json=?,updated_at=? WHERE id=?", (kind, name, str(payload.get("description", character["description"]))[:1000], str(payload.get("features", character["features"]))[:1000], json.dumps(character_locks(payload.get("locks")), separators=(",", ":")), now(), character_id))
                    owner_project = connection.execute("SELECT id FROM projects WHERE owner_id=? ORDER BY updated_at DESC LIMIT 1", (character["owner_id"],)).fetchone()
                    updated = load_project_characters(connection, owner_project["id"], [character_id])[0] if owner_project else character_payload(connection.execute("SELECT * FROM characters WHERE id=?", (character_id,)).fetchone())
                except sqlite3.IntegrityError:
                    raise ApiError(409, "A reusable library item with this name already exists")
            return self.send_json(200, {"character": updated})
        if clean_path.startswith("/api/character-references/"):
            reference_id = clean_path.rsplit("/", 1)[-1]
            payload = self.read_json()
            role = str(payload.get("role", "identity"))
            if role not in CHARACTER_REFERENCE_ROLES:
                raise ApiError(400, "Invalid character reference role")
            with connect() as connection:
                reference = connection.execute("SELECT cr.*,c.owner_id FROM character_references cr JOIN characters c ON c.id=cr.character_id WHERE cr.id=?", (reference_id,)).fetchone()
                if not reference:
                    raise ApiError(404, "Character reference not found")
                if reference["owner_id"] != user["id"] and user["role"] != "admin":
                    raise ApiError(404, "Character reference not found")
                connection.execute("UPDATE character_references SET role=?,identity_lock=?,notes=?,updated_at=? WHERE id=?", (role, 1 if payload.get("identity_lock", True) else 0, str(payload.get("notes", reference["notes"]))[:500], now(), reference_id))
            return self.send_json(200, {"ok": True})
        if clean_path.startswith("/api/projects/"):
            project_id = clean_path.rsplit("/", 1)[-1]
            project = self.project_for(user, project_id)
            payload = self.read_json()
            name = str(payload.get("name", "")).strip()
            if not 1 <= len(name) <= 80:
                raise ApiError(400, "Project name must be 1-80 characters")
            with connect() as connection:
                connection.execute("UPDATE projects SET name=?,updated_at=? WHERE id=?", (name, now(), project["id"]))
                updated = connection.execute("SELECT * FROM projects WHERE id=?", (project["id"],)).fetchone()
            return self.send_json(200, {"project": dict(updated)})
        if not clean_path.startswith("/api/admin/users/"):
            raise ApiError(404, "Not found")
        self.require_admin(user)
        target_id = clean_path.rsplit("/", 1)[-1]
        payload = self.read_json()
        changes, values = [], []
        access_update = payload.get("model_access")
        resolution_update = payload.get("resolution_access")
        model_resolution_update = payload.get("model_resolution_access")
        if access_update is not None and not isinstance(access_update, dict):
            raise ApiError(400, "model_access must be an object")
        if resolution_update is not None and not isinstance(resolution_update, dict):
            raise ApiError(400, "resolution_access must be an object")
        if model_resolution_update is not None and not isinstance(model_resolution_update, dict):
            raise ApiError(400, "model_resolution_access must be an object")
        if "enabled" in payload:
            if target_id == user["id"] and not payload["enabled"]:
                raise ApiError(400, "Admin cannot disable the active account")
            changes.append("enabled=?")
            values.append(1 if payload["enabled"] else 0)
        if payload.get("password"):
            password = str(payload["password"])
            if len(password) < 10:
                raise ApiError(400, "Password must be at least 10 characters")
            changes.append("password_hash=?")
            values.append(hash_password(password))
        if not changes and access_update is None and resolution_update is None and model_resolution_update is None:
            raise ApiError(400, "Nothing to update")
        with connect() as connection:
            target = connection.execute("SELECT role FROM users WHERE id=?", (target_id,)).fetchone()
            if not target:
                raise ApiError(404, "User not found")
            if changes:
                changes.append("updated_at=?")
                values.extend([now(), target_id])
                connection.execute(f"UPDATE users SET {','.join(changes)} WHERE id=?", values)
            if access_update is not None:
                if target["role"] == "admin":
                    raise ApiError(400, "Administrator access is always enabled")
                for preset, enabled in access_update.items():
                    if preset not in {item["id"] for item in workflow_presets()} or not isinstance(enabled, bool):
                        raise ApiError(400, "Invalid model access setting")
                    connection.execute("INSERT INTO user_model_access(user_id,preset,enabled,updated_at) VALUES(?,?,?,?) ON CONFLICT(user_id,preset) DO UPDATE SET enabled=excluded.enabled,updated_at=excluded.updated_at", (target_id, preset, 1 if enabled else 0, now()))
            if resolution_update is not None:
                if target["role"] == "admin":
                    raise ApiError(400, "Administrator access is always enabled")
                for resolution, enabled in resolution_update.items():
                    if resolution not in ALL_RESOLUTIONS or not isinstance(enabled, bool):
                        raise ApiError(400, "Invalid resolution access setting")
                    connection.execute("INSERT INTO user_resolution_access(user_id,resolution,enabled,updated_at) VALUES(?,?,?,?) ON CONFLICT(user_id,resolution) DO UPDATE SET enabled=excluded.enabled,updated_at=excluded.updated_at", (target_id, resolution, 1 if enabled else 0, now()))
            if model_resolution_update is not None:
                if target["role"] == "admin":
                    raise ApiError(400, "Administrator access is always enabled")
                preset_map = {item["id"]: item for item in workflow_presets()}
                for preset_id, resolution_changes in model_resolution_update.items():
                    preset = preset_map.get(preset_id)
                    if not preset or not isinstance(resolution_changes, dict):
                        raise ApiError(400, "Invalid model resolution setting")
                    supported = set(preset.get("resolutions") or [])
                    for resolution, enabled in resolution_changes.items():
                        if resolution not in supported or not isinstance(enabled, bool):
                            raise ApiError(400, "Invalid resolution for this model")
                        connection.execute("INSERT INTO user_model_resolution_access(user_id,preset,resolution,enabled,updated_at) VALUES(?,?,?,?,?) ON CONFLICT(user_id,preset,resolution) DO UPDATE SET enabled=excluded.enabled,updated_at=excluded.updated_at", (target_id, preset_id, resolution, 1 if enabled else 0, now()))
            if payload.get("password") or payload.get("logout_all"):
                connection.execute("DELETE FROM sessions WHERE user_id=?", (target_id,))
        self.send_json(200, {"ok": True})

    def delete_request(self) -> None:
        user, _ = self.require_auth(csrf=True)
        clean_path = self.path.split("?", 1)[0]
        if clean_path.startswith("/api/character-references/"):
            reference_id = clean_path.rsplit("/", 1)[-1]
            with connect() as connection:
                reference = connection.execute("SELECT cr.id,c.owner_id FROM character_references cr JOIN characters c ON c.id=cr.character_id WHERE cr.id=?", (reference_id,)).fetchone()
                if not reference:
                    raise ApiError(404, "Character reference not found")
                if reference["owner_id"] != user["id"] and user["role"] != "admin":
                    raise ApiError(404, "Character reference not found")
                connection.execute("DELETE FROM character_references WHERE id=?", (reference_id,))
            return self.send_json(200, {"ok": True})
        if clean_path.startswith("/api/characters/"):
            character_id = clean_path.rsplit("/", 1)[-1]
            with connect() as connection:
                character = connection.execute("SELECT id,owner_id FROM characters WHERE id=?", (character_id,)).fetchone()
                if not character:
                    raise ApiError(404, "Character not found")
                if character["owner_id"] != user["id"] and user["role"] != "admin":
                    raise ApiError(404, "Character not found")
                connection.execute("DELETE FROM characters WHERE id=?", (character_id,))
            return self.send_json(200, {"ok": True})
        if clean_path.startswith("/api/assets/"):
            asset_id = clean_path.rsplit("/", 1)[-1]
            with connect() as connection:
                asset = connection.execute("SELECT * FROM assets WHERE id=?", (asset_id,)).fetchone() if user["role"] == "admin" else connection.execute("SELECT * FROM assets WHERE id=? AND owner_id=?", (asset_id, user["id"])).fetchone()
                if not asset:
                    raise ApiError(404, "Asset not found")
                connection.execute("DELETE FROM assets WHERE id=?", (asset_id,))
            path = (DATA_ROOT / asset["relative_path"]).resolve()
            if DATA_ROOT in path.parents and path.is_file():
                path.unlink()
            thumbnail = THUMB_ROOT / f"{asset_id}.jpg"
            thumbnail.unlink(missing_ok=True)
            return self.send_json(200, {"ok": True})
        if clean_path.startswith("/api/projects/"):
            project = self.project_for(user, clean_path.rsplit("/", 1)[-1])
            with connect() as connection:
                protected_ids = {row["asset_id"] for row in connection.execute("SELECT DISTINCT cr.asset_id FROM character_references cr JOIN characters c ON c.id=cr.character_id JOIN assets a ON a.id=cr.asset_id WHERE c.owner_id=? AND a.project_id=?", (project["owner_id"], project["id"])).fetchall()}
                if protected_ids:
                    marks = ",".join("?" for _ in protected_ids)
                    asset_rows = connection.execute(f"SELECT id,relative_path FROM assets WHERE project_id=? AND id NOT IN ({marks})", (project["id"], *protected_ids)).fetchall()
                    connection.execute(f"DELETE FROM assets WHERE project_id=? AND id NOT IN ({marks})", (project["id"], *protected_ids))
                else:
                    asset_rows = connection.execute("SELECT id,relative_path FROM assets WHERE project_id=?", (project["id"],)).fetchall()
                    connection.execute("DELETE FROM assets WHERE project_id=?", (project["id"],))
                connection.execute("DELETE FROM projects WHERE id=?", (project["id"],))
            for asset in asset_rows:
                path = (DATA_ROOT / asset["relative_path"]).resolve()
                if DATA_ROOT in path.parents and path.is_file():
                    path.unlink(missing_ok=True)
                (THUMB_ROOT / f"{asset['id']}.jpg").unlink(missing_ok=True)
            return self.send_json(200, {"ok": True})
        if clean_path.startswith("/api/admin/users/"):
            self.require_admin(user)
            target_id = clean_path.rsplit("/", 1)[-1]
            if target_id == user["id"]:
                raise ApiError(400, "Admin cannot delete the active account")
            with connect() as connection:
                target = connection.execute("SELECT id,username,role FROM users WHERE id=?", (target_id,)).fetchone()
                if not target:
                    raise ApiError(404, "User not found")
                if target["role"] != "member":
                    raise ApiError(400, "Administrator accounts cannot be deleted here")
                connection.execute("DELETE FROM users WHERE id=?", (target_id,))
            media_folder = (MEDIA_ROOT / target_id).resolve()
            if MEDIA_ROOT in media_folder.parents and media_folder.is_dir():
                shutil.rmtree(media_folder)
            return self.send_json(200, {"ok": True, "deleted": target["username"]})
        raise ApiError(404, "Not found")

    def receive_upload(self, user: sqlite3.Row, project_id: str, owner_id: str | None = None) -> None:
        owner_id = owner_id or user["id"]
        length = self.content_length(MAX_UPLOAD)
        original = SAFE_FILE_RE.sub("_", Path(self.headers.get("X-Filename", "upload.bin")).name).strip(" .") or "upload.bin"
        mime_type = self.headers.get("Content-Type", "application/octet-stream").split(";", 1)[0]
        asset_id = str(uuid.uuid4())
        folder = MEDIA_ROOT / owner_id / project_id
        folder.mkdir(parents=True, exist_ok=True)
        destination = folder / f"{asset_id}-{original}"
        remaining = length
        with destination.open("wb") as output:
            while remaining:
                chunk = self.rfile.read(min(1024 * 1024, remaining))
                if not chunk:
                    raise ApiError(400, "Upload ended unexpectedly")
                output.write(chunk)
                remaining -= len(chunk)
        kind = "video" if mime_type.startswith("video/") else "audio" if mime_type.startswith("audio/") else "image" if mime_type.startswith("image/") else "upload"
        timestamp = now()
        relative = destination.relative_to(DATA_ROOT).as_posix()
        with connect() as connection:
            connection.execute("INSERT INTO assets(id,owner_id,project_id,kind,filename,relative_path,mime_type,bytes,source,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)", (asset_id, owner_id, project_id, kind, original, relative, mime_type, length, "upload", timestamp))
            connection.execute("UPDATE projects SET updated_at=? WHERE id=?", (timestamp, project_id))
        self.send_json(201, {"asset": {"id": asset_id, "filename": original, "kind": kind, "mime_type": mime_type, "bytes": length, "url": f"/media/{asset_id}", "created_at": timestamp}})

    def serve_thumbnail(self, asset_id: str) -> None:
        user, _ = self.require_auth()
        with connect() as connection:
            asset = connection.execute("SELECT * FROM assets WHERE id=?", (asset_id,)).fetchone()
        if not asset or asset["kind"] not in {"image", "video"} or (asset["owner_id"] != user["id"] and user["role"] != "admin" and not bool(asset["published"])):
            raise ApiError(404, "Asset not found")
        source = (DATA_ROOT / asset["relative_path"]).resolve()
        if DATA_ROOT not in source.parents or not source.is_file():
            raise ApiError(404, "Asset file missing")
        thumbnail = THUMB_ROOT / f"{asset_id}.jpg"
        try:
            with THUMB_LOCK:
                if not thumbnail.is_file() or thumbnail.stat().st_mtime < source.stat().st_mtime:
                    if asset["kind"] == "image":
                        with Image.open(source) as opened:
                            preview = opened.convert("RGB")
                            preview.thumbnail((960, 960), Image.Resampling.LANCZOS)
                            preview.save(thumbnail, "JPEG", quality=78, optimize=True)
                    else:
                        with av.open(str(source)) as container:
                            stream = container.streams.video[0]
                            frame = next(container.decode(stream))
                            preview = frame.to_image().convert("RGB")
                            preview.thumbnail((960, 960), Image.Resampling.LANCZOS)
                            preview.save(thumbnail, "JPEG", quality=76, optimize=True)
        except Exception:
            return self.serve_media(asset_id)
        self.send_response(200)
        self.send_header("Content-Type", "image/jpeg")
        self.send_header("Content-Length", str(thumbnail.stat().st_size))
        self.send_header("Cache-Control", "private, max-age=86400")
        self.end_headers()
        with thumbnail.open("rb") as source_file:
            while chunk := source_file.read(256 * 1024):
                self.wfile.write(chunk)

    def serve_media(self, asset_id: str) -> None:
        user, _ = self.require_auth()
        with connect() as connection:
            asset = connection.execute("SELECT * FROM assets WHERE id=?", (asset_id,)).fetchone()
        if not asset or (asset["owner_id"] != user["id"] and user["role"] != "admin" and not bool(asset["published"])):
            raise ApiError(404, "Asset not found")
        path = (DATA_ROOT / asset["relative_path"]).resolve()
        if DATA_ROOT not in path.parents or not path.is_file():
            raise ApiError(404, "Asset file missing")
        file_size = path.stat().st_size
        start, end = 0, file_size - 1
        range_header = self.headers.get("Range", "")
        if range_header.startswith("bytes="):
            match = re.fullmatch(r"bytes=(\d*)-(\d*)", range_header[6:].strip())
            if match:
                first, last = match.groups()
                if first:
                    start = int(first)
                    end = min(int(last), file_size - 1) if last else file_size - 1
                elif last:
                    start = max(0, file_size - int(last))
                if start >= file_size or end < start:
                    self.send_response(416)
                    self.send_header("Content-Range", f"bytes */{file_size}")
                    self.end_headers()
                    return
        partial = start != 0 or end != file_size - 1
        content_length = end - start + 1
        self.send_response(206 if partial else 200)
        self.send_header("Content-Type", asset["mime_type"] or mimetypes.guess_type(path.name)[0] or "application/octet-stream")
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(content_length))
        if partial:
            self.send_header("Content-Range", f"bytes {start}-{end}/{file_size}")
        self.send_header("Cache-Control", "private, max-age=3600")
        self.end_headers()
        with path.open("rb") as source:
            source.seek(start)
            remaining = content_length
            while remaining > 0 and (chunk := source.read(min(1024 * 1024, remaining))):
                self.wfile.write(chunk)
                remaining -= len(chunk)

    def create_session(self, user_id: str, remember: bool = False) -> None:
        token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(24)
        session_days = 365 if remember else 30
        max_age = session_days * 86400
        expires = (datetime.now(timezone.utc) + timedelta(days=session_days)).isoformat()
        with connect() as connection:
            connection.execute("DELETE FROM sessions WHERE expires_at < ?", (now(),))
            timestamp = now()
            connection.execute("INSERT INTO sessions(token_hash,user_id,csrf_token,expires_at,created_at,last_seen) VALUES(?,?,?,?,?,?)", (sha256(token.encode()).hexdigest(), user_id, csrf, expires, timestamp, timestamp))
        secure = "; Secure" if self.headers.get("X-Forwarded-Proto") == "https" else ""
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Set-Cookie", f"studio_session={token}; Path=/; HttpOnly; SameSite=Strict; Max-Age={max_age}{secure}")
        self.end_headers()
        self.wfile.write(b'{"ok":true}')

    def clear_session(self) -> None:
        self.send_response(204)
        self.send_header("Set-Cookie", "studio_session=; Path=/; HttpOnly; SameSite=Strict; Max-Age=0")
        self.end_headers()

    def require_auth(self, csrf: bool = False) -> tuple[sqlite3.Row, str]:
        token = self.session_token()
        if not token:
            raise ApiError(401, "Authentication required")
        with connect() as connection:
            token_hash = sha256(token.encode()).hexdigest()
            row = connection.execute("SELECT u.*,s.csrf_token,s.expires_at FROM sessions s JOIN users u ON u.id=s.user_id WHERE s.token_hash=?", (token_hash,)).fetchone()
            if row and row["enabled"] and row["expires_at"] > now():
                connection.execute("UPDATE sessions SET last_seen=? WHERE token_hash=?", (now(), token_hash))
        if not row or not row["enabled"] or row["expires_at"] <= now():
            raise ApiError(401, "Session expired")
        if csrf and not secrets.compare_digest(self.headers.get("X-CSRF-Token", ""), row["csrf_token"]):
            raise ApiError(403, "Invalid security token")
        return row, row["csrf_token"]

    @staticmethod
    def require_admin(user: sqlite3.Row) -> None:
        if user["role"] != "admin":
            raise ApiError(403, "Admin access required")

    def project_for(self, user: sqlite3.Row, project_id: str | None) -> sqlite3.Row:
        if not project_id:
            raise ApiError(400, "project_id is required")
        with connect() as connection:
            row = connection.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone()
        if not row or (row["owner_id"] != user["id"] and user["role"] != "admin"):
            raise ApiError(404, "Project not found")
        return row

    @staticmethod
    def credentials(payload: dict) -> tuple[str, str]:
        username, password = str(payload.get("username", "")).strip(), str(payload.get("password", ""))
        if not USERNAME_RE.fullmatch(username):
            raise ApiError(400, "Username must be 3-32 letters, numbers, dots, dashes or underscores")
        if len(password) < 10:
            raise ApiError(400, "Password must be at least 10 characters")
        return username, password

    def session_token(self) -> str:
        cookie = SimpleCookie(self.headers.get("Cookie", ""))
        return cookie["studio_session"].value if "studio_session" in cookie else ""

    def read_json(self) -> dict:
        length = self.content_length(1_000_000)
        try:
            return json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            raise ApiError(400, "Invalid JSON")

    def content_length(self, maximum: int) -> int:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            raise ApiError(400, "Invalid content length")
        if length < 0 or length > maximum:
            raise ApiError(413, "Request is too large")
        return length

    def query(self, name: str) -> str | None:
        from urllib.parse import parse_qs, urlsplit
        values = parse_qs(urlsplit(self.path).query).get(name)
        return values[0] if values else None

    def send_json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload, separators=(",", ":")).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)


if __name__ == "__main__":
    initialize_database()
    with connect() as connection:
        needs_admin = connection.execute("SELECT 1 FROM users WHERE role='admin' LIMIT 1").fetchone() is None
    if needs_admin and not BOOTSTRAP_TOKEN_PATH.exists():
        with BOOTSTRAP_TOKEN_PATH.open("x", encoding="utf-8") as token_file:
            token_file.write(secrets.token_urlsafe(32))
    if needs_admin:
        print(f"First-run setup code is stored in: {BOOTSTRAP_TOKEN_PATH}", flush=True)
    migrate_global_character_library()
    server = ThreadingHTTPServer((HOST, PORT), StudioHandler)
    print(f"Tanjir Creator Studio running at http://{HOST}:{PORT}", flush=True)
    server.serve_forever()




















