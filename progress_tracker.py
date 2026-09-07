from __future__ import annotations

import asyncio
import json
import threading
import time
from urllib.parse import urlparse

import aiohttp


_lock = threading.Lock()
_progress: dict[str, int] = {}
_phases: dict[str, str] = {}
_steps: dict[str, tuple[int, int]] = {}
_listeners: set[str] = set()


def _set(prompt_id: str, percent: float, phase: str | None = None) -> None:
    if not prompt_id:
        return
    value = max(0, min(100, int(round(percent))))
    with _lock:
        _progress[prompt_id] = max(_progress.get(prompt_id, 0), value)
        if phase:
            _phases[prompt_id] = phase


def step_for(prompt_id: str) -> tuple[int, int] | None:
    with _lock:
        return _steps.get(prompt_id)


def progress_for(prompt_id: str, status: str) -> int:
    if status == "completed":
        return 100
    if status == "queued":
        return 0
    with _lock:
        # Comfy reports sampler completion before image/video encoding ends.
        return min(_progress.get(prompt_id, 1), 98)


def phase_for(prompt_id: str, status: str) -> str:
    if status == "completed":
        return "Completed"
    if status == "failed":
        return "Failed"
    if status == "queued":
        return "Queued"
    with _lock:
        progress = _progress.get(prompt_id, 1)
        return "Finalizing" if progress >= 98 else _phases.get(prompt_id, "Starting")


def start_listener(comfy_url: str, client_id: str) -> None:
    if not client_id:
        return
    with _lock:
        if client_id in _listeners:
            return
        _listeners.add(client_id)
    thread = threading.Thread(target=_run, args=(comfy_url, client_id), daemon=True)
    thread.start()


def _run(comfy_url: str, client_id: str) -> None:
    # A completed prompt must not stop updates for every later Studio job.
    while True:
        try:
            asyncio.run(_listen(comfy_url, client_id))
        except Exception:
            time.sleep(2)


async def _listen(comfy_url: str, client_id: str) -> None:
    parsed = urlparse(comfy_url)
    scheme = "wss" if parsed.scheme == "https" else "ws"
    ws_url = f"{scheme}://{parsed.netloc}/ws?clientId={client_id}"
    timeout = aiohttp.ClientTimeout(total=None, connect=5, sock_read=3600)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.ws_connect(ws_url, heartbeat=30) as socket:
            async for message in socket:
                if message.type != aiohttp.WSMsgType.TEXT:
                    continue
                event = json.loads(message.data)
                event_type = event.get("type")
                data = event.get("data") or {}
                prompt_id = str(data.get("prompt_id") or "")
                if event_type == "progress_state" and prompt_id:
                    nodes = data.get("nodes") or {}
                    totals = [(float(node.get("value", 0)), float(node.get("max", 0))) for node in nodes.values()]
                    current = sum(value for value, maximum in totals if maximum > 0)
                    maximum = sum(maximum for value, maximum in totals if maximum > 0)
                    if maximum > 0:
                        with _lock:
                            _steps[prompt_id] = (
                                max(0, int(round(current))),
                                max(1, int(round(maximum))),
                            )
                        _set(prompt_id, current * 100 / maximum, "Generating")
                elif event_type == "progress" and prompt_id:
                    maximum = float(data.get("max") or 0)
                    value = float(data.get("value") or 0)
                    if maximum > 0:
                        with _lock:
                            _steps[prompt_id] = (max(0, int(round(value))), max(1, int(round(maximum))))
                        _set(prompt_id, value * 100 / maximum, "Generating")
                elif event_type == "execution_start" and prompt_id:
                    _set(prompt_id, 1, "Starting")
                elif event_type in {"execution_success", "execution_cached"} and prompt_id:
                    _set(prompt_id, 100, "Completed")
                elif event_type in {"execution_error", "execution_interrupted"} and prompt_id:
                    _set(prompt_id, 100, "Failed")
