from __future__ import annotations

import json
import mimetypes
import os
import re
import shutil
import sqlite3
import subprocess
import threading
import time
import urllib.error
import urllib.request
import uuid
import psutil
from datetime import datetime, timezone
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


ROOT = Path(__file__).resolve().parent
WEB_ROOT = ROOT / "web"
DATA_ROOT = ROOT / "data"
LOG_ROOT = ROOT / "logs"
MODEL_ROOT = ROOT / "models"
RUNTIME_ROOT = ROOT / "runtime"
DB_PATH = DATA_ROOT / "agent.db"
LLAMA_URL = "http://127.0.0.1:8091"
HOST = "127.0.0.1"
PORT = 8092
MODEL = str(MODEL_ROOT / "Qwen3.5-35B-A3B-Q3_K_S.gguf")
MMPROJ = str(MODEL_ROOT / "mmproj-F16.gguf")
WORKSPACE_ROOTS = [ROOT.parent]
PROCESS_LOCK = threading.Lock()
LLM_CHAT_LOCK = threading.Lock()
ENGINE_ENABLED = False

DIRECTOR_SYSTEM = """You are the private AI Cinema Director inside Tanjir Creator Studio. Reply in the user's language; understand Bangladeshi Bangla and English fluently. Behave like a flexible creative assistant, not an assistant permanently trapped inside the selected project. You work only on filmmaking and media creation: standalone image/video requests as well as stories, screenplays, acts, sequences, scenes, shots, dialogue, continuity, characters, costumes, props, locations, cinematography, blocking, editing, sound direction, asset planning and production prompts. The Studio supplies a SCOPE object. When scope.mode is general, handle the request independently and never assume characters, continuity, story or assets from the project currently open in the website; generated media goes to Quick Creations. When scope.mode is project, use only that explicitly requested project's supplied context and preserve its identities and continuity. Never provide software coding, terminal, filesystem administration, website maintenance or unrelated assistant work. Never claim that media was queued unless the Studio reports it. For large film work use Act > Sequence > Scene > Shot, but do not force that structure onto a simple one-off image or video request. When the user explicitly asks to generate, create, start, render or queue an image/video, call queue_generation with a complete model-ready prompt and sensible defaults. Use real character/asset IDs only when they exist in the supplied project context. If the user is only planning or discussing, do not call the tool. Do not invent existing assets."""

WORKBENCH_SYSTEM = """You are a private local AI Workbench running only on this Windows PC. You are a capable coding and operations agent for Tanjir Creator Studio, ComfyUI, local files, scripts and general technical work. Reply in the user's language and understand Bangladeshi Bangla and English. Inspect before editing, preserve existing work, keep backups, and use tools when evidence is needed. You may read/write allowed local workspaces and run commands. Never run destructive actions (delete, recursive move, disk/registry/system shutdown, credential changes) without explicit approval. Explain completed results accurately; never claim success without tool evidence."""


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def estimate_tokens(text: str) -> int:
    # Bengali and other non-ASCII scripts commonly consume roughly one to two
    # tokens per visible character in this model; English is much denser.
    return max(1, int(sum(1.55 if ord(char) > 127 else 0.32 for char in str(text))))


def initialize() -> None:
    for path in (DATA_ROOT, LOG_ROOT, WEB_ROOT):
        path.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(DB_PATH) as connection:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS conversations(
              id TEXT PRIMARY KEY, mode TEXT NOT NULL, title TEXT NOT NULL,
              project_id TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS messages(
              id INTEGER PRIMARY KEY AUTOINCREMENT, conversation_id TEXT NOT NULL,
              role TEXT NOT NULL, content TEXT NOT NULL, created_at TEXT NOT NULL,
              FOREIGN KEY(conversation_id) REFERENCES conversations(id) ON DELETE CASCADE
            );
            """
        )


def llama_health() -> bool:
    try:
        with urllib.request.urlopen(f"{LLAMA_URL}/health", timeout=2) as response:
            return response.status == 200
    except Exception:
        return False


def system_metrics() -> dict:
    memory = psutil.virtual_memory()
    metrics = {"cpu": round(psutil.cpu_percent(interval=None), 1), "ram": round(memory.percent, 1), "ram_used_gb": round(memory.used / (1024 ** 3), 1), "ram_total_gb": round(memory.total / (1024 ** 3), 1), "gpu": 0.0, "vram": 0.0, "vram_used_gb": 0.0, "vram_total_gb": 0.0, "engine": llama_health(), "qwen_ram_gb": 0.0}
    expected = str(RUNTIME_ROOT / "llama-server.exe").lower()
    for process in psutil.process_iter(["exe", "memory_info"]):
        try:
            if str(process.info.get("exe") or "").lower() == expected:
                metrics["qwen_ram_gb"] += round(process.info["memory_info"].rss / (1024 ** 3), 2)
        except (psutil.NoSuchProcess, psutil.AccessDenied, TypeError):
            continue
    try:
        flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        result = subprocess.run(["nvidia-smi", "--query-gpu=utilization.gpu,memory.used,memory.total", "--format=csv,noheader,nounits"], capture_output=True, text=True, timeout=3, check=True, creationflags=flags)
        gpu, used, total = [float(value.strip()) for value in result.stdout.splitlines()[0].split(",")]
        metrics.update(gpu=round(gpu, 1), vram=round(used * 100 / total, 1) if total else 0.0, vram_used_gb=round(used / 1024, 1), vram_total_gb=round(total / 1024, 1))
    except (FileNotFoundError, subprocess.SubprocessError, ValueError, IndexError):
        pass
    return metrics


def ensure_llama() -> None:
    if llama_health():
        return
    with PROCESS_LOCK:
        if llama_health():
            return
        command = [
            str(RUNTIME_ROOT / "llama-server.exe"), "-m", MODEL, "--mmproj", MMPROJ,
            "--host", "127.0.0.1", "--port", "8091", "-c", "8192", "-ngl", "45",
            "-fa", "on", "-ctk", "q8_0", "-ctv", "q8_0", "--jinja", "--sleep-idle-seconds", "45",
        ]
        flags = subprocess.CREATE_NO_WINDOW | subprocess.DETACHED_PROCESS if os.name == "nt" else 0
        out = (LOG_ROOT / "llama-server.out.log").open("a", encoding="utf-8")
        err = (LOG_ROOT / "llama-server.err.log").open("a", encoding="utf-8")
        subprocess.Popen(command, stdout=out, stderr=err, creationflags=flags, cwd=RUNTIME_ROOT)
        deadline = time.monotonic() + 180
        while time.monotonic() < deadline:
            if llama_health():
                return
            time.sleep(2)
        raise RuntimeError("Local Qwen engine did not become ready")


def release_llama() -> int:
    stopped = 0
    expected = str(RUNTIME_ROOT / "llama-server.exe").lower()
    for process in psutil.process_iter(["pid", "exe"]):
        try:
            if str(process.info.get("exe") or "").lower() == expected:
                process.terminate()
                process.wait(timeout=10)
                stopped += 1
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.TimeoutExpired):
            continue
    return stopped


def llama_chat(messages: list[dict], tools: list[dict] | None = None, max_tokens: int = 900) -> dict:
    # This model becomes dramatically slower when the desktop workbench and the
    # website Director use separate llama.cpp slots at the same time. Serialize
    # generation so each caller gets the full GPU and a predictable response.
    with LLM_CHAT_LOCK:
        if not ENGINE_ENABLED:
            raise RuntimeError("Local Qwen is OFF. Turn Qwen ON before sending a message.")
        ensure_llama()
        body = {
            "model": "Qwen3.5-35B-A3B-Q3_K_S.gguf",
            "messages": messages,
            "temperature": 0.25,
            "max_tokens": max_tokens,
            "chat_template_kwargs": {"enable_thinking": False},
        }
        if tools:
            body["tools"] = tools
            body["tool_choice"] = "auto"
        request = urllib.request.Request(
            f"{LLAMA_URL}/v1/chat/completions",
            data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=600) as response:
            result = json.loads(response.read())
    choice = result["choices"][0]
    message = dict(choice["message"])
    message["_finish_reason"] = choice.get("finish_reason")
    message["_usage"] = result.get("usage") or {}
    return message


def resolved_allowed(path_value: str, *, write: bool = False) -> Path:
    path = Path(path_value).expanduser()
    if not path.is_absolute():
        path = WORKSPACE_ROOTS[0] / path
    path = path.resolve()
    allowed = any(path == root.resolve() or root.resolve() in path.parents for root in WORKSPACE_ROOTS)
    if not allowed:
        raise ValueError("Path is outside the allowed local workspaces")
    if write and path.exists() and path.is_dir():
        raise ValueError("Expected a file path, received a directory")
    return path


def backup_file(path: Path) -> None:
    if not path.is_file():
        return
    relative = re.sub(r"[^A-Za-z0-9._-]+", "_", str(path))[-140:]
    target = DATA_ROOT / "backups" / f"{datetime.now():%Y%m%d-%H%M%S}-{relative}"
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(path, target)


def tool_list_files(arguments: dict) -> str:
    root = resolved_allowed(str(arguments.get("path", WORKSPACE_ROOTS[0])))
    pattern = str(arguments.get("pattern", "*"))
    limit = min(500, max(1, int(arguments.get("limit", 120))))
    if not root.exists() or not root.is_dir():
        return "Directory not found"
    rows = []
    for item in root.rglob(pattern):
        rows.append({"path": str(item), "type": "dir" if item.is_dir() else "file", "bytes": item.stat().st_size if item.is_file() else None})
        if len(rows) >= limit:
            break
    return json.dumps(rows, ensure_ascii=False)


def tool_read_file(arguments: dict) -> str:
    path = resolved_allowed(str(arguments.get("path", "")))
    start = max(1, int(arguments.get("start_line", 1)))
    maximum = min(1500, max(1, int(arguments.get("max_lines", 300))))
    if not path.is_file():
        return "File not found"
    text = path.read_text(encoding="utf-8", errors="replace").splitlines()
    return "\n".join(f"{index + 1}: {line}" for index, line in enumerate(text[start - 1:start - 1 + maximum], start - 1))


def tool_search_text(arguments: dict) -> str:
    root = resolved_allowed(str(arguments.get("path", WORKSPACE_ROOTS[0])))
    query = str(arguments.get("query", ""))
    if not query:
        return "query is required"
    command = ["rg", "-n", "--no-heading", "--max-count", "200", query, str(root)]
    result = subprocess.run(command, capture_output=True, text=True, timeout=30, creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
    return (result.stdout or result.stderr or "No matches")[:30000]


def tool_write_file(arguments: dict) -> str:
    path = resolved_allowed(str(arguments.get("path", "")), write=True)
    content = str(arguments.get("content", ""))
    if path.exists():
        backup_file(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return f"Wrote {len(content)} characters to {path}"


def tool_replace_text(arguments: dict) -> str:
    path = resolved_allowed(str(arguments.get("path", "")), write=True)
    old, new = str(arguments.get("old", "")), str(arguments.get("new", ""))
    if not path.is_file() or not old:
        return "File or old text was not found"
    content = path.read_text(encoding="utf-8", errors="strict")
    count = content.count(old)
    expected = int(arguments.get("expected_count", 1))
    if count != expected:
        return f"Refused: expected {expected} exact match(es), found {count}"
    backup_file(path)
    path.write_text(content.replace(old, new), encoding="utf-8")
    return f"Replaced {count} occurrence(s) in {path}"


DESTRUCTIVE = re.compile(r"\b(remove-item|del\b|erase\b|rmdir\b|rm\s+-|format\b|clear-disk|shutdown\b|restart-computer|reg\s+(delete|add)|git\s+reset\s+--hard)\b", re.I)


def tool_run_command(arguments: dict) -> str:
    command = str(arguments.get("command", "")).strip()
    if not command:
        return "command is required"
    if DESTRUCTIVE.search(command) and not bool(arguments.get("approved", False)):
        return "APPROVAL_REQUIRED: This command may delete or materially alter data. Ask the user to approve the exact command, then call again with approved=true."
    workdir = resolved_allowed(str(arguments.get("workdir", WORKSPACE_ROOTS[0])))
    timeout = min(300, max(1, int(arguments.get("timeout", 60))))
    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-Command", command], cwd=workdir,
        capture_output=True, text=True, timeout=timeout,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )
    return json.dumps({"exit_code": result.returncode, "stdout": result.stdout[-24000:], "stderr": result.stderr[-12000:]}, ensure_ascii=False)


TOOL_HANDLERS = {
    "list_files": tool_list_files,
    "read_file": tool_read_file,
    "search_text": tool_search_text,
    "write_file": tool_write_file,
    "replace_text": tool_replace_text,
    "run_command": tool_run_command,
}


def schema(name: str, description: str, properties: dict, required: list[str] | None = None) -> dict:
    return {"type": "function", "function": {"name": name, "description": description, "parameters": {"type": "object", "properties": properties, "required": required or []}}}


WORKBENCH_TOOLS = [
    schema("list_files", "List files recursively in an allowed workspace", {"path": {"type": "string"}, "pattern": {"type": "string"}, "limit": {"type": "integer"}}),
    schema("read_file", "Read a UTF-8 text file with line numbers", {"path": {"type": "string"}, "start_line": {"type": "integer"}, "max_lines": {"type": "integer"}}, ["path"]),
    schema("search_text", "Search text in files using ripgrep", {"path": {"type": "string"}, "query": {"type": "string"}}, ["path", "query"]),
    schema("write_file", "Create or overwrite a text file; an automatic backup is made", {"path": {"type": "string"}, "content": {"type": "string"}}, ["path", "content"]),
    schema("replace_text", "Safely replace an exact text fragment; an automatic backup is made", {"path": {"type": "string"}, "old": {"type": "string"}, "new": {"type": "string"}, "expected_count": {"type": "integer"}}, ["path", "old", "new"]),
    schema("run_command", "Run a PowerShell command in an allowed workspace", {"command": {"type": "string"}, "workdir": {"type": "string"}, "timeout": {"type": "integer"}, "approved": {"type": "boolean"}}, ["command"]),
]

DIRECTOR_TOOLS = [
    schema("queue_generation", "Queue one standalone or project-scoped Studio image or video generation in the destination selected by the Studio request scope", {
        "prompt": {"type": "string", "description": "Complete production-ready generation prompt"},
        "preset": {"type": "string", "description": "An available model id from project context"},
        "ratio": {"type": "string", "enum": ["16:9", "9:16", "1:1", "4:3", "3:4", "21:9"]},
        "resolution": {"type": "string", "enum": ["240p", "360p", "480p", "720p", "1080p", "1440p", "2160p"]},
        "duration": {"type": "number", "description": "Video seconds; ignored for images"},
        "character_ids": {"type": "array", "items": {"type": "string"}},
        "image_refs": {"type": "array", "items": {"type": "string"}, "description": "Existing image asset IDs only"},
        "audio_refs": {"type": "array", "items": {"type": "string"}, "description": "Existing audio asset IDs only"},
    }, ["prompt", "preset", "ratio", "resolution"]),
]


def conversation(mode: str, conversation_id: str | None, project_id: str = "") -> tuple[str, list[dict]]:
    with sqlite3.connect(DB_PATH) as connection:
        if conversation_id:
            row = connection.execute("SELECT id FROM conversations WHERE id=? AND mode=?", (conversation_id, mode)).fetchone()
        else:
            row = None
        if not row:
            conversation_id = str(uuid.uuid4())
            stamp = now()
            connection.execute("INSERT INTO conversations VALUES(?,?,?,?,?,?)", (conversation_id, mode, "New conversation", project_id, stamp, stamp))
        rows = connection.execute("SELECT role,content FROM messages WHERE conversation_id=? ORDER BY id DESC LIMIT 40", (conversation_id,)).fetchall()
    return conversation_id, [{"role": role, "content": content} for role, content in reversed(rows)]


def save_message(conversation_id: str, role: str, content: str) -> None:
    with sqlite3.connect(DB_PATH) as connection:
        connection.execute("INSERT INTO messages(conversation_id,role,content,created_at) VALUES(?,?,?,?)", (conversation_id, role, content, now()))
        connection.execute("UPDATE conversations SET updated_at=?,title=CASE WHEN title='New conversation' AND ?='user' THEN substr(?,1,72) ELSE title END WHERE id=?", (now(), role, content, conversation_id))


def run_workbench(history: list[dict]) -> str:
    messages = [{"role": "system", "content": WORKBENCH_SYSTEM}, *history]
    for _ in range(8):
        message = llama_chat(messages, WORKBENCH_TOOLS)
        tool_calls = message.get("tool_calls") or []
        if not tool_calls:
            return str(message.get("content", ""))
        messages.append(message)
        for call in tool_calls:
            name = call.get("function", {}).get("name", "")
            try:
                arguments = json.loads(call.get("function", {}).get("arguments") or "{}")
                output = TOOL_HANDLERS[name](arguments) if name in TOOL_HANDLERS else "Unknown tool"
            except Exception as error:
                output = f"Tool error: {error}"
            messages.append({"role": "tool", "tool_call_id": call.get("id", name), "name": name, "content": output})
    return "I stopped after eight tool rounds to avoid an unintended loop."


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(WEB_ROOT), **kwargs)

    def log_message(self, format: str, *args) -> None:
        return

    def end_headers(self) -> None:
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        super().end_headers()

    def send_json(self, status: int, payload: dict) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def read_json(self) -> dict:
        length = int(self.headers.get("Content-Length", "0"))
        return json.loads(self.rfile.read(length) or b"{}")

    def do_GET(self) -> None:
        if self.path == "/api/health":
            return self.send_json(200, {"ok": True, "enabled": ENGINE_ENABLED, "engine": llama_health()})
        if self.path == "/api/metrics":
            return self.send_json(200, system_metrics())
        if self.path == "/api/director/conversations":
            with sqlite3.connect(DB_PATH) as connection:
                connection.row_factory = sqlite3.Row
                rows = connection.execute("SELECT id,title,project_id,created_at,updated_at FROM conversations WHERE mode='director' ORDER BY updated_at DESC LIMIT 100").fetchall()
            return self.send_json(200, {"conversations": [dict(row) for row in rows]})
        if self.path.startswith("/api/director/conversations/"):
            conversation_id = self.path.rsplit("/", 1)[-1]
            with sqlite3.connect(DB_PATH) as connection:
                connection.row_factory = sqlite3.Row
                thread = connection.execute("SELECT id,title,project_id,created_at,updated_at FROM conversations WHERE id=? AND mode='director'", (conversation_id,)).fetchone()
                if not thread:
                    return self.send_json(404, {"error": "Conversation not found"})
                messages = connection.execute("SELECT role,content,created_at FROM messages WHERE conversation_id=? ORDER BY id", (conversation_id,)).fetchall()
            return self.send_json(200, {"conversation": dict(thread), "messages": [dict(row) for row in messages]})
        return super().do_GET()

    def do_POST(self) -> None:
        global ENGINE_ENABLED
        try:
            payload = self.read_json()
            if self.path == "/api/engine/on":
                ENGINE_ENABLED = True
                ensure_llama()
                return self.send_json(200, {"ok": True, "enabled": True, "engine": llama_health()})
            if self.path == "/api/engine/off":
                ENGINE_ENABLED = False
                return self.send_json(200, {"ok": True, "enabled": False, "engine": False, "stopped": release_llama()})
            if self.path == "/api/engine/release":
                return self.send_json(200, {"ok": True, "stopped": release_llama()})
            if self.path == "/api/chat":
                text = str(payload.get("message", "")).strip()
                cid, history = conversation("workbench", payload.get("conversation_id"))
                save_message(cid, "user", text)
                answer = run_workbench([*history, {"role": "user", "content": text}])
                save_message(cid, "assistant", answer)
                return self.send_json(200, {"conversation_id": cid, "answer": answer})
            if self.path == "/api/director/chat":
                text = str(payload.get("message", "")).strip()
                project_id = str(payload.get("project_id", ""))
                cid, history = conversation("director", payload.get("conversation_id"), project_id)
                raw_context = payload.get("project_context", {}) or {}
                compact_characters = []
                for item in list(raw_context.get("characters_elements_props") or [])[:30]:
                    compact_characters.append({key: item.get(key) for key in ("id", "name", "kind", "description", "features", "locks") if item.get(key) not in (None, "", [], {})})
                compact_assets = []
                all_assets = list(raw_context.get("recent_assets") or [])
                for item in all_assets[:16]:
                    compact_assets.append({key: item.get(key) for key in ("id", "kind", "filename", "source", "created_at") if item.get(key) not in (None, "")})
                compact_jobs = []
                all_jobs = list(raw_context.get("recent_jobs") or [])
                for item in all_jobs[:6]:
                    compact_jobs.append({key: item.get(key) for key in ("id", "preset", "status", "error", "created_at", "updated_at") if item.get(key) not in (None, "")})
                compact_context = {
                    "scope": raw_context.get("scope", {"mode": "general"}),
                    "project": raw_context.get("project", {}),
                    "available_projects": list(raw_context.get("available_projects") or [])[:30],
                    "characters_elements_props": compact_characters,
                    "asset_count": len(all_assets),
                    "recent_assets": compact_assets,
                    "job_count": len(all_jobs),
                    "recent_jobs": compact_jobs,
                    "available_models": list(raw_context.get("available_models") or [])[:20],
                }
                context = json.dumps(compact_context, ensure_ascii=False)
                save_message(cid, "user", text)
                # Keep recent turns while reserving space for project context and a complete answer.
                trimmed_history = list(history)
                base_prompt = DIRECTOR_SYSTEM + "\nREQUEST SCOPE AND AVAILABLE CONTEXT:\n" + context
                # Reserve about 2,000 tokens for the answer and template overhead.
                input_budget = 5600
                while trimmed_history and estimate_tokens(base_prompt) + estimate_tokens(text) + sum(estimate_tokens(item.get("content", "")) + 8 for item in trimmed_history) > input_budget:
                    trimmed_history.pop(0)
                messages = [{"role": "system", "content": base_prompt}, *trimmed_history, {"role": "user", "content": text}]
                # Greetings and short questions should not reserve a screenplay-sized
                # answer; a smaller ceiling keeps the public request below proxy timeout.
                director_max_tokens = 480 if len(text) <= 100 else 1200
                try:
                    reply = llama_chat(messages, DIRECTOR_TOOLS, max_tokens=director_max_tokens)
                except urllib.error.HTTPError as error:
                    detail = error.read().decode("utf-8", errors="replace")
                    if error.code in {400, 500} and "exceeds the available context size" in detail:
                        messages = [{"role": "system", "content": base_prompt}, {"role": "user", "content": text}]
                        reply = llama_chat(messages, DIRECTOR_TOOLS, max_tokens=director_max_tokens)
                    else:
                        raise
                actions = []
                for call in reply.get("tool_calls") or []:
                    if call.get("function", {}).get("name") != "queue_generation":
                        continue
                    try:
                        action = json.loads(call.get("function", {}).get("arguments") or "{}")
                    except ValueError:
                        continue
                    if isinstance(action, dict) and str(action.get("prompt", "")).strip():
                        actions.append(action)
                answer_parts = [str(reply.get("content", "")).strip()]
                usage = dict(reply.get("_usage") or {})
                completion_total = int(usage.get("completion_tokens", 0) or 0)
                # If the model reaches its output ceiling, continue seamlessly instead of
                # making the user understand or manage token limits.
                continuation_count = 0
                while reply.get("_finish_reason") == "length" and not actions and continuation_count < 3:
                    partial = answer_parts[-1]
                    messages.extend([
                        {"role": "assistant", "content": partial},
                        {"role": "user", "content": "Continue exactly from where you stopped. Do not repeat earlier text. Finish the requested answer."},
                    ])
                    reply = llama_chat(messages, None, max_tokens=1600)
                    answer_parts.append(str(reply.get("content", "")).strip())
                    next_usage = dict(reply.get("_usage") or {})
                    completion_total += int(next_usage.get("completion_tokens", 0) or 0)
                    usage = next_usage or usage
                    continuation_count += 1
                answer = "\n".join(part for part in answer_parts if part).strip()
                if actions and not answer:
                    answer = f"Studio-তে {len(actions)}টি generation request প্রস্তুত করেছি। Queue নিশ্চিত হলে নিচে status দেখাবে।"
                save_message(cid, "assistant", answer)
                prompt_tokens = int(usage.get("prompt_tokens", 0) or 0)
                used_tokens = min(8192, prompt_tokens + int(usage.get("completion_tokens", 0) or 0))
                token_status = {"used": used_tokens, "remaining": max(0, 8192 - used_tokens), "total": 8192, "answer_tokens": completion_total, "auto_continued": continuation_count}
                return self.send_json(200, {"conversation_id": cid, "answer": answer, "actions": actions, "token_status": token_status})
            return self.send_json(404, {"error": "Not found"})
        except Exception as error:
            return self.send_json(500, {"error": str(error)})


if __name__ == "__main__":
    initialize()
    print(f"Local AI Workbench listening on http://{HOST}:{PORT}", flush=True)
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()
