"""Convert saved ComfyUI UI workflows into queueable API prompts."""

from __future__ import annotations

from pathlib import Path
from urllib.request import Request, urlopen
import json
import secrets

IGNORED_TYPES = {"MarkdownNote", "Note", "Reroute"}
LINK_TYPES = {
    "IMAGE", "MASK", "LATENT", "CONDITIONING", "MODEL", "CLIP", "VAE",
    "AUDIO", "VIDEO", "NOISE", "GUIDER", "SAMPLER", "SIGMAS",
    "SEEDVR2_DIT", "SEEDVR2_VAE", "VHS_BATCH_MANAGER", "VHS_VIDEOINFO",
}


def fetch_object_info(comfy_url: str) -> dict:
    with urlopen(Request(f"{comfy_url}/object_info", headers={"Accept": "application/json"}), timeout=15) as response:
        return json.loads(response.read())


def load_workflow(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def _link_parts(link) -> tuple[int, int, int, int]:
    if isinstance(link, dict):
        return int(link["origin_id"]), int(link["origin_slot"]), int(link["target_id"]), int(link["target_slot"])
    return int(link[1]), int(link[2]), int(link[3]), int(link[4])


def _widget_names(schema: dict) -> list[str]:
    names: list[str] = []
    inputs = schema.get("input", {})
    for section in ("required", "optional"):
        for name, spec in (inputs.get(section) or {}).items():
            type_name = spec[0] if isinstance(spec, list) and spec else spec
            if isinstance(type_name, str) and type_name in LINK_TYPES:
                continue
            if type_name == "COMFY_AUTOGROW_V3":
                continue
            names.append(name)
    return names


def ui_to_api(workflow: dict, object_info: dict, *, remove_nodes: set[int] | None = None) -> dict:
    """Convert ordinary non-subgraph UI nodes into an API prompt."""
    remove = remove_nodes or set()
    nodes = {int(node["id"]): node for node in workflow.get("nodes", []) if isinstance(node.get("id"), int)}
    incoming: dict[tuple[int, int], tuple[int, int]] = {}
    for link in workflow.get("links", []):
        source, source_slot, target, target_slot = _link_parts(link)
        if source not in remove and target not in remove:
            incoming[(target, target_slot)] = (source, source_slot)

    prompt: dict[str, dict] = {}
    for node_id, node in nodes.items():
        node_type = node.get("type", "")
        if node_id in remove or node_type in IGNORED_TYPES or node_type not in object_info:
            continue
        values = list(node.get("widgets_values") or [])
        inputs: dict = {}
        for slot, item in enumerate(node.get("inputs") or []):
            link = incoming.get((node_id, slot))
            if link and link[0] in nodes and link[0] not in remove:
                inputs[item["name"]] = [str(link[0]), link[1]]
        for index, name in enumerate(_widget_names(object_info[node_type])):
            if index < len(values) and name not in inputs:
                value = values[index]
                if not isinstance(value, dict):
                    inputs[name] = value
        prompt[str(node_id)] = {"class_type": node_type, "inputs": inputs, "_meta": {"title": node.get("title") or node_type}}
    return prompt


def set_inputs(graph: dict, node_id: int, **values) -> None:
    target = graph.get(str(node_id))
    if not target:
        raise ValueError(f"Workflow node {node_id} is unavailable")
    target["inputs"].update(values)


def queue_prompt(comfy_url: str, prompt: dict, client_id: str | None = None) -> dict:
    body = json.dumps({"prompt": prompt, "client_id": client_id or secrets.token_hex(16)}).encode()
    request = Request(f"{comfy_url}/prompt", data=body, headers={"Content-Type": "application/json"}, method="POST")
    with urlopen(request, timeout=30) as response:
        return json.loads(response.read())
