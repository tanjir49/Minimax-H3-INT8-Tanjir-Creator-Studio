from __future__ import annotations

from pathlib import Path
import math
import av
import re
import shutil
import uuid

from PIL import Image, ImageOps

from comfy_adapter import fetch_object_info, load_workflow, queue_prompt, set_inputs, ui_to_api


DIMENSIONS = {
    "16:9": {"240p": (432, 240), "360p": (640, 360), "480p": (864, 480), "720p": (1280, 720), "1080p": (1920, 1080), "1440p": (2560, 1440), "2160p": (3840, 2160)},
    "9:16": {"240p": (240, 432), "360p": (360, 640), "480p": (480, 864), "720p": (720, 1280), "1080p": (1080, 1920), "1440p": (1440, 2560), "2160p": (2160, 3840)},
    "1:1": {"240p": (240, 240), "360p": (360, 360), "480p": (480, 480), "720p": (720, 720), "1080p": (1080, 1080), "1440p": (1440, 1440), "2160p": (2160, 2160)},
    "4:3": {"240p": (320, 240), "360p": (480, 360), "480p": (640, 480), "720p": (960, 720), "1080p": (1440, 1080), "1440p": (1920, 1440), "2160p": (2880, 2160)},
    "3:4": {"240p": (240, 320), "360p": (360, 480), "480p": (480, 640), "720p": (720, 960), "1080p": (1080, 1440), "1440p": (1440, 1920), "2160p": (2160, 2880)},
    "21:9": {"240p": (560, 240), "360p": (840, 360), "480p": (1120, 480), "720p": (1680, 720), "1080p": (2560, 1080), "1440p": (3440, 1440), "2160p": (5120, 2160)},
}


# Keep legacy on-disk video libraries stable when a Studio project has been
# recreated with a new database ID. This changes only the ComfyUI output
# subfolder; Studio jobs and imported media continue to use the live project ID.
VIDEO_OUTPUT_PROJECT_ALIASES = {}


def video_output_project_id(owner_id: str, project_id: str) -> str:
    return VIDEO_OUTPUT_PROJECT_ALIASES.get((owner_id, project_id), project_id)


REFERENCE_TEXT_PRESERVATION = (
    "REFERENCE TEXT PRESERVATION — HIGHEST PRIORITY: Preserve every visible word, letter, glyph, number, logo and sign from all reference images exactly as shown. "
    "Never translate, transliterate, respell, reinterpret, replace or invent text. Bengali/Bangla writing must remain Bengali with the exact original spelling, glyph order, colors, typography and sign layout; never convert it to Hindi, Devanagari, English or any other script. "
    "Keep signs and printed text stable, legible and unchanged across every video frame; do not morph, flicker or redraw them."
)


def preserve_reference_text(text: str) -> str:
    return f"{text}\n\n{REFERENCE_TEXT_PRESERVATION}"


SFX_ONLY_AUDIO_POLICY = (
    "AUDIO MODE — ABSOLUTE SFX ONLY, HIGHEST PRIORITY: The finished audio track may contain only minimal realistic diegetic environmental and action sound effects that are visibly justified by this scene, for example footsteps, clothing movement, object contact, vehicle movement, traffic, wind, rain or room tone. "
    "ZERO HUMAN VOICE: every person remains completely silent. Generate no spoken word, dialogue, conversation, whisper, murmur, mumble, chant, exclamation, gasp, moan, laugh, cry, vocal reaction, voice-over, narration, singing or other human vocal sound in any language. Do not animate mouths as if speaking. "
    "ZERO MUSIC: generate no song, melody, instrumental music, score, background music, radio music or rhythmic soundtrack. Ignore any conflicting request for speech, vocals or music elsewhere in the prompt. Do not invent off-screen sounds or events. If a sound is not clearly required by a visible action, omit it. If uncertain, output silence instead of voice or music."
)


REQUESTED_VOICE_AUDIO_POLICY = (
    "AUDIO MODE — REQUESTED VOICE ONLY: Generate only the dialogue, narration, singing or other human voice explicitly requested in the user's scene prompt. "
    "Do not invent extra speakers, words, whispers, murmurs, crowd chatter, vocal reactions, voice-over, radio speech or background conversation. "
    "Keep the requested voice synchronized with the intended visible speaker. Add only restrained, realistic diegetic ambience and action sound effects that naturally belong to the visible scene. "
    "Generate no background music unless the user explicitly requests music."
)


EXPLICIT_VOICE_RE = re.compile(
    r"(?:\bdialogue\b|\bspeaks?\b|\bsays?\b|\btalks?\b|\bvoice[- ]?over\b|\bnarrat(?:e|es|ion)\b|\bsing(?:s|ing)?\b|\bwhispers?\b|\bshouts?\b|\bspoken\b|\blip[- ]?sync\b|\btts\b|\bvoice\b|কথা\s*(?:বলে|বলছে|বলবে)|বলছে|সংলাপ|ডায়লগ|ডায়লগ|কণ্ঠ|ভয়েস|ভয়েস|গান\s*(?:গায়|গায়|গাইছে))",
    re.IGNORECASE,
)


def requests_human_voice(text: str) -> bool:
    cleaned = re.sub(
        r"(?:\bno\s+(?:dialogue|speech|voice|voice[- ]?over|narration|singing|human vocals?)\b|\bzero\s+(?:dialogue|speech|voice|human vocals?)\b|কোনো\s+(?:কথা|সংলাপ|ডায়লগ|ডায়লগ|কণ্ঠ|ভয়েস|ভয়েস)\s*(?:না|নয়|নয়)?|কথা\s*বলবে\s*না)",
        "",
        text,
        flags=re.IGNORECASE,
    )
    return bool(EXPLICIT_VOICE_RE.search(cleaned))


def apply_scene_audio_policy(text: str) -> str:
    if requests_human_voice(text):
        return f"{REQUESTED_VOICE_AUDIO_POLICY}\n\nSCENE DIRECTION:\n{text}\n\nFINAL AUDIO CHECK: include only the specifically requested voice and natural visible-scene sound; invent no other speech or music."
    return f"{SFX_ONLY_AUDIO_POLICY}\n\nVISUAL SCENE DIRECTION:\n{text}\n\nFINAL AUDIO CHECK — SFX ONLY: zero speech, zero human vocalization, zero music; retain only necessary visible-scene ambience and effects."


class GenerationError(Exception):
    pass


class GenerationService:
    def __init__(self, comfy_url: str, workflow_root: Path, data_root: Path, comfy_input: Path):
        self.comfy_url = comfy_url
        self.workflow_root = workflow_root
        self.data_root = data_root
        self.comfy_input = comfy_input
        self.object_info = fetch_object_info(comfy_url)

    def _copy_asset(self, row) -> str:
        source = (self.data_root / row["relative_path"]).resolve()
        if self.data_root not in source.parents or not source.is_file():
            raise GenerationError("Reference file is missing")
        folder = self.comfy_input / "tanjir-studio"
        folder.mkdir(parents=True, exist_ok=True)
        name = f"{uuid.uuid4().hex}{source.suffix.lower()}"
        shutil.copy2(source, folder / name)
        return f"tanjir-studio/{name}"

    def queue(self, payload: dict, assets: dict[str, object], owner_id: str, project_id: str, client_id: str | None = None) -> dict:
        preset = str(payload.get("preset", ""))
        if preset == "lustify-remix-image":
            self.object_info = fetch_object_info(self.comfy_url)
            unets = self.object_info.get("UNETLoader", {}).get("input", {}).get("required", {}).get("unet_name", [[]])[0]
            vaes = self.object_info.get("VAELoader", {}).get("input", {}).get("required", {}).get("vae_name", [[]])[0]
            encoders = self.object_info.get("CLIPLoader", {}).get("input", {}).get("required", {}).get("clip_name", [[]])[0]
            if "lustifyNSFWCheckpoint_v10Krea2.safetensors" not in unets or "qwen_image_vae.safetensors" not in vaes or "qwen3vl_4b_fp8_scaled.safetensors" not in encoders:
                raise GenerationError("Lustify setup is incomplete. Finish the Qwen3-VL encoder download and install it in models/text_encoders.")
        if preset in {"minimax-h3-edit", "minimax-h3-edit-best"}:
            checkpoints = self.object_info.get("CheckpointLoaderSimple", {}).get("input", {}).get("required", {}).get("ckpt_name", [[]])[0]
            if "sam3.1_multiplex_fp16.safetensors" not in checkpoints:
                raise GenerationError("Video Edit needs the SAM3.1 tracking model. Install sam3.1_multiplex_fp16.safetensors in ComfyUI models/checkpoints.")
            prompt = self._minimax_edit(payload, assets, owner_id, project_id, preset)
        elif preset == "minimax-h3-cinema-lab":
            prompt = self._cinema_lab(payload, assets, owner_id, project_id)
        elif preset == "minimax-h3-cinema-consistency":
            prompt = self._cinema_lab(payload, assets, owner_id, project_id, "cinema-consistency-profile.json")
        elif preset in {"minimax-h3", "minimax-h3-faster", "minimax-h3-superfast", "minimax-h3-best", "minimax-h3-bf16", "minimax-h3-teacache", "minimax-h3-pdd", "minimax-h3-video-turbo"}:
            prompt = self._minimax(payload, assets, owner_id, project_id, preset)
        elif preset == "ltx-2.5":
            prompt = self._ltx(payload, assets, owner_id, project_id)
        elif preset == "realesrgan-video-fast":
            prompt = self._realesrgan_video(payload, assets, owner_id, project_id)
        elif preset == "seedvr-upscale":
            prompt = self._seedvr(payload, assets, owner_id, project_id)
        elif preset == "flux-image":
            prompt = self._flux(payload, assets, owner_id, project_id)
        elif preset == "z-image-turbo":
            prompt = self._zimage(payload, owner_id, project_id)
        elif preset == "lustify-remix-image":
            prompt = self._lustify_remix(payload, owner_id, project_id)
        elif preset == "wan22-remix-video":
            prompt = self._wan22_remix(payload, assets, owner_id, project_id)
        elif preset in {"realesrgan-photo", "realesrgan-anime"}:
            prompt = self._realesrgan(payload, assets, owner_id, project_id, preset)
        elif preset == "acestep-music":
            prompt = self._acestep_music(payload, owner_id, project_id)
        else:
            raise GenerationError("Unknown generation model")
        missing = {str(value[0]) for node in prompt.values() for value in node["inputs"].values()
                   if isinstance(value, list) and len(value) == 2 and str(value[0]) not in prompt}
        if missing:
            raise GenerationError("The generation engine could not load required workflow nodes. Restart ComfyUI to refresh its model folders, then retry.")
        return queue_prompt(self.comfy_url, prompt, client_id=client_id)

    def _acestep_music(self, payload: dict, owner_id: str, project_id: str) -> dict:
        caption = str(payload.get("prompt", "")).strip()
        if not caption:
            raise GenerationError("Describe the music you want to create")
        duration = max(5.0, min(180.0, float(payload.get("duration") or 30)))
        lyrics = str(payload.get("lyrics", "")).strip()
        instrumental = bool(payload.get("instrumental"))
        if instrumental and not lyrics:
            lyrics = "[Instrumental]"
        return {
            "1": {"class_type": "AcestepCPPModelLoader", "inputs": {"lm_model": "acestep-5Hz-lm-1.7B-Q8_0.gguf", "text_encoder_model": "Qwen3-Embedding-0.6B-Q8_0.gguf", "dit_model": "acestep-v15-turbo-Q8_0.gguf", "vae_model": "vae-BF16.gguf"}},
            "2": {"class_type": "AcestepCPPGenerate", "inputs": {"models": ["1", 0], "caption": caption, "lyrics": lyrics, "instrumental": instrumental, "vocal_language": str(payload.get("vocal_language") or ""), "duration": duration, "bpm": max(0, min(300, int(payload.get("bpm") or 0))), "keyscale": str(payload.get("keyscale") or ""), "timesignature": "4", "inference_steps": 8, "guidance_scale": 1.0, "shift": 3.0, "seed": int(payload.get("seed") or -1), "lm_temperature": 0.85, "lm_cfg_scale": 2.0, "lm_top_p": 0.9, "lm_top_k": "0", "use_cot_caption": True}},
            "3": {"class_type": "AcestepCPPAudioPlayer", "inputs": {"filepath": ["2", 0]}},
        }

    def _zimage(self, payload: dict, owner_id: str, project_id: str) -> dict:
        ratio, resolution = str(payload.get("ratio", "1:1")), str(payload.get("resolution", "720p"))
        width, height = DIMENSIONS[ratio][resolution]
        text = str(payload.get("prompt", "")).strip()
        seed = int(payload.get("seed") or 42)
        return {
            "1": {"class_type": "UNETLoader", "inputs": {"unet_name": "Official_ZImage\\z_image_turbo_bf16.safetensors", "weight_dtype": "default"}},
            "2": {"class_type": "CLIPLoader", "inputs": {"clip_name": "Official_ZImage\\qwen_3_4b.safetensors", "type": "lumina2", "device": "default"}},
            "3": {"class_type": "VAELoader", "inputs": {"vae_name": "Official_ZImage\\ae.safetensors"}},
            "4": {"class_type": "CLIPTextEncode", "inputs": {"text": text, "clip": ["2", 0]}},
            "5": {"class_type": "ConditioningZeroOut", "inputs": {"conditioning": ["4", 0]}},
            "6": {"class_type": "EmptySD3LatentImage", "inputs": {"width": width, "height": height, "batch_size": 1}},
            "7": {"class_type": "ModelSamplingAuraFlow", "inputs": {"model": ["1", 0], "shift": 3.0}},
            "8": {"class_type": "KSampler", "inputs": {"model": ["7", 0], "seed": seed, "steps": 8, "cfg": 1.0, "sampler_name": "res_multistep", "scheduler": "simple", "positive": ["4", 0], "negative": ["5", 0], "latent_image": ["6", 0], "denoise": 1.0}},
            "9": {"class_type": "VAEDecode", "inputs": {"samples": ["8", 0], "vae": ["3", 0]}},
            "10": {"class_type": "SaveImage", "inputs": {"images": ["9", 0], "filename_prefix": f"Tanjir_Studio/{owner_id}/{project_id}/Z_Image_Turbo_BF16"}},
        }

    def _flux_reference_sheet(self, rows: list[object]) -> str:
        folder = self.comfy_input / "tanjir-studio"
        folder.mkdir(parents=True, exist_ok=True)
        if len(rows) == 1:
            return self._copy_asset(rows[0])
        count = min(len(rows), 9)
        columns = math.ceil(math.sqrt(count))
        rows_count = math.ceil(count / columns)
        cell = 768
        sheet = Image.new("RGB", (columns * cell, rows_count * cell), (18, 18, 18))
        for index, row in enumerate(rows[:9]):
            source = (self.data_root / row["relative_path"]).resolve()
            if self.data_root not in source.parents or not source.is_file():
                raise GenerationError("Reference file is missing")
            with Image.open(source) as image:
                prepared = ImageOps.contain(image.convert("RGB"), (cell, cell), Image.Resampling.LANCZOS)
                x = (index % columns) * cell + (cell - prepared.width) // 2
                y = (index // columns) * cell + (cell - prepared.height) // 2
                sheet.paste(prepared, (x, y))
        filename = f"flux-references-{uuid.uuid4().hex}.jpg"
        sheet.save(folder / filename, quality=95, subsampling=0)
        return f"tanjir-studio/{filename}"

    def _flux(self, payload: dict, assets: dict[str, object], owner_id: str, project_id: str) -> dict:
        workflow = load_workflow(self.workflow_root / "flux-image.json")
        subgraphs = (workflow.get("definitions") or {}).get("subgraphs") or []
        if not subgraphs:
            raise GenerationError("Flux workflow subgraph is missing")
        subgraph = subgraphs[0]
        prompt = ui_to_api({"nodes": subgraph.get("nodes", []), "links": subgraph.get("links", [])}, self.object_info)
        ratio, resolution = str(payload.get("ratio", "16:9")), str(payload.get("resolution", "1080p"))
        try:
            width, height = DIMENSIONS[ratio][resolution]
        except KeyError as error:
            raise GenerationError("Unsupported ratio or resolution") from error
        text = str(payload.get("prompt", "")).strip()
        if not text:
            raise GenerationError("Prompt is required")
        ref_ids = list(payload.get("image_refs") or [])[:9]
        if ref_ids:
            text = preserve_reference_text(text)
        set_inputs(prompt, 6, text=text)
        set_inputs(prompt, 47, width=width, height=height, batch_size=1)
        set_inputs(prompt, 48, width=width, height=height)
        if "94" in prompt:
            set_inputs(prompt, 94, value=False)

        if ref_ids:
            reference = self._flux_reference_sheet([assets[asset_id] for asset_id in ref_ids])
            prompt["900"] = {"class_type": "LoadImage", "inputs": {"image": reference}, "_meta": {"title": "Flux references"}}
            set_inputs(prompt, 44, pixels=["900", 0])
            set_inputs(prompt, 72, image=["900", 0])
        else:
            prompt.pop("44", None)
            prompt.pop("43", None)
            prompt.pop("72", None)
            set_inputs(prompt, 22, conditioning=["26", 0])

        prompt["999"] = {"class_type": "SaveImage", "inputs": {"images": ["8", 0], "filename_prefix": f"Tanjir_Studio/{owner_id}/{project_id}/Flux_Image"}, "_meta": {"title": "Save Flux image"}}
        return prompt

    def _minimax(self, payload: dict, assets: dict[str, object], owner_id: str, project_id: str, preset: str = "minimax-h3") -> dict:
        workflow = load_workflow(self.workflow_root / "minimax-h3.json")
        prompt = ui_to_api(workflow, self.object_info, remove_nodes=set(range(100, 111)))
        profiles = {
            "minimax-h3": {"steps": 16, "cache_mode": "H3 Fast — 0.10 / max 2"},
            "minimax-h3-faster": {"steps": 10, "cache_mode": "H3 Aggressive — 0.12 / max 2"},
            "minimax-h3-superfast": {"steps": 4, "cache_mode": None},
            "minimax-h3-best": {"steps": 20, "cache_mode": "H3 Safe — 0.08 / max 2"},
            "minimax-h3-bf16": {"steps": 20, "cache_mode": None},
            "minimax-h3-teacache": {"steps": 20, "cache_mode": None},
            "minimax-h3-pdd": {"steps": 8, "cache_mode": None},
            "minimax-h3-video-turbo": {"steps": 8, "cache_mode": None},
        }
        profile = profiles[preset]
        if preset == "minimax-h3-bf16":
            prompt["1"]["inputs"]["unet_name"] = "minimax_h3_ref2va_bf16.safetensors"
            prompt["4"]["inputs"]["model"] = ["1", 0]
            prompt.pop("3", None)
        elif preset == "minimax-h3-superfast":
            # Keep the proven INT8 Ref2VA base model, but use the official
            # four-step Ref2V Turbo LoRA for the Superfast profile.
            prompt["2"] = {"class_type": "LoraLoaderModelOnly", "inputs": {"model": ["1", 0], "lora_name": "minimax_h3_ref2v_turbo_4step_v0.1_comfyui_bf16.safetensors", "strength_model": 1.0}, "_meta": {"title": "MiniMax H3 INT8 Ref2VA + Ref2V Turbo — 4 Steps"}}
            prompt["4"]["inputs"]["model"] = ["2", 0]
            prompt.pop("3", None)
        elif preset == "minimax-h3-teacache":
            prompt["3"] = {
                "class_type": "MiniMaxH3TeaCache",
                "inputs": {
                    "model": ["1", 0],
                    "rel_l1_thresh": 0.15,
                    "start_step": 2,
                    "end_step": -2,
                    "total_steps": 20,
                },
                "_meta": {"title": "MiniMax H3 TeaCache — Balanced"},
            }
        elif preset in {"minimax-h3-pdd", "minimax-h3-video-turbo"}:
            # PDD is a distilled 8-evaluation recipe. It must use its own
            # trained sigma grid, Euler sampler and CFG 1.0, with no cache.
            prompt.pop("3", None)
            prompt["4"]["inputs"]["model"] = ["1", 0]
            # Use the same quality-preserving memory-efficient attention path
            # already proven by the Cinema Consistency PDD8 profile.  The plain
            # PDD preset previously skipped this node, which made an otherwise
            # identical 8-step render substantially slower on the RTX 5080.
            prompt["90"] = {
                "class_type": "MiniMaxH3MemoryEfficientSageAttentionPatch",
                "inputs": {"model": ["4", 0]},
                "_meta": {"title": "MiniMax H3 Memory-Efficient SageAttention"},
            }
            prompt["19"] = {
                "class_type": "MiniMaxH3PDDAccApply",
                "inputs": {
                    "model": ["90", 0],
                    "pdd_file": "minimax_h3_ref2va_pdd_acc_8step_comfyui.safetensors",
                    "nfe": "8",
                    "lora_strength": 1.0,
                    "head_strength": 1.0,
                    "on_off_grid": "error",
                    "partition": "",
                    "enabled": True,
                    "partition_check": "error",
                },
                "_meta": {"title": "MiniMax H3 Ref2VA PDD Acc — 8 Steps"},
            }
            prompt["10"]["inputs"]["model"] = ["19", 0]
            prompt["11"]["inputs"]["sampler_name"] = "euler"
            prompt["14"]["inputs"]["sigmas"] = ["19", 1]
            prompt.pop("12", None)
            if preset == "minimax-h3-video-turbo":
                # Preserve exact attention math in smaller head groups, then let
                # SageAttention accelerate dense work and SolAttn skip low-value
                # video-token blocks. Text, references and audio rows stay exact.
                prompt["20"] = {
                    "class_type": "MiniMaxLowVRAMAttention",
                    "inputs": {"model": ["4", 0], "head_chunks": 4},
                    "_meta": {"title": "MiniMax H3 Low-VRAM Attention"},
                }
                prompt["21"] = {
                    "class_type": "PathchSageAttentionKJ",
                    "inputs": {
                        "model": ["20", 0],
                        "sage_attention": "sageattn_qk_int8_pv_fp8_cuda",
                        "allow_compile": False,
                    },
                    "_meta": {"title": "SageAttention 2.2 — Blackwell FP8"},
                }
                prompt["22"] = {
                    "class_type": "SolAttnPatch",
                    "inputs": {
                        "model": ["21", 0],
                        "tau": 1.0,
                        "start_percent": 0.25,
                        "end_percent": 0.85,
                        "min_tokens": 8192,
                        "int8_qk": True,
                        "sink_conditioning": "exact_kv_and_rows",
                        "morton": False,
                        "morton_curve": "2d_frame",
                        "int8_pv": True,
                        "verbose": False,
                        "use_tma": False,
                        "dense_blocks": "0-2,-2,-1",
                    },
                    "_meta": {"title": "SolAttn — Conservative H3 Speed Patch"},
                }
                prompt["19"]["inputs"]["model"] = ["22", 0]
        else:
            set_inputs(prompt, 3, mode=profile["cache_mode"])
        if "12" in prompt:
            set_inputs(prompt, 12, steps=profile["steps"])
        ratio, resolution = str(payload.get("ratio", "16:9")), str(payload.get("resolution", "720p"))
        try:
            width, height = DIMENSIONS[ratio][resolution]
        except KeyError as error:
            raise GenerationError("Unsupported ratio or resolution") from error
        if preset == "minimax-h3-video-turbo":
            # Generate the expensive pass at aspect-correct 480p, then use the
            # learned MiniMax 3D latent model for the final HD video. A second H3
            # pass is deliberately avoided: on 16 GB Dynamic VRAM it has to
            # re-fault the entire model after the upscaler and is both slower and
            # less reliable than decoding the learned upscale directly.
            # The existing 480p table is already aspect-correct and aligned to
            # 32 for every supported ratio, avoiding geometry drift in pass 1.
            base_width, base_height = DIMENSIONS[ratio]["480p"]
            prompt["30"] = {
                "class_type": "LTXVSeparateAVLatent",
                "inputs": {"av_latent": ["14", 0]},
                "_meta": {"title": "Separate Video and Audio Latents"},
            }
            prompt["31"] = {
                "class_type": "MinimaxH3LatentUpscaler3D",
                "inputs": {
                    "latent": ["30", 0],
                    "model_name": "minimax_h3_latent_upscaler_3d_fp16.safetensors",
                    "mode": "target dimensions",
                    "mode.width": width,
                    "mode.height": height,
                    "align": 32,
                    "enable_temporal_chunking": True,
                    "force_unload": True,
                    "device": "cuda",
                    "precision": "fp16",
                },
                "_meta": {"title": "MiniMax H3 Learned 3D Latent Upscale"},
            }
            prompt["32"] = {
                "class_type": "LTXVConcatAVLatent",
                "inputs": {"video_latent": ["31", 0], "audio_latent": ["30", 1]},
                "_meta": {"title": "Recombine Upscaled Video with Original Audio"},
            }
            prompt["15"]["inputs"]["samples"] = ["32", 0]
            # The upscaler only changes the video latent. Decode audio from the
            # pristine first pass so speech and synchronization are untouched.
            prompt["16"]["inputs"]["samples"] = ["14", 0]
            width, height = base_width, base_height
        duration = max(1.0, min(float(payload.get("duration", 5)), 150.0))
        frame_count = int(round(duration * 24)) + 1
        text = str(payload.get("prompt", "")).strip()
        if not text:
            raise GenerationError("Prompt is required")
        image_ids = []
        ref_image_size = "match"
        if payload.get("start_frame"):
            image_ids.append(payload["start_frame"])
            text = "Use <Picture 1> as the exact opening frame. " + text
        if payload.get("end_frame"):
            image_ids.append(payload["end_frame"])
            end_number = len(image_ids)
            text = f"Use <Picture {end_number}> as the target ending frame. " + text
        image_ids.extend(payload.get("image_refs") or [])
        if image_ids:
            text = preserve_reference_text(text)
        if preset == "minimax-h3-video-turbo" and image_ids:
            # Identity work needs a less approximate attention path than generic
            # scenery. Keep references/audio exact, retain Sage's kernel speed,
            # and give multi-person faces more first-pass pixels before upscale.
            prompt["19"]["inputs"]["model"] = ["21", 0]
            prompt["21"]["inputs"]["sage_attention"] = "sageattn_qk_int8_pv_fp16_triton"
            # MiniMax H3's `max` reference mode keeps high-resolution identity
            # tokens active throughout sampling. It is slower than `match`, but
            # substantially reduces facial-feature drift in reference-led clips.
            ref_image_size = "max"
            if len(image_ids) >= 2 and resolution != "480p":
                target_width = int(prompt["31"]["inputs"]["mode.width"])
                target_height = int(prompt["31"]["inputs"]["mode.height"])
                identity_scale = min(1.0, math.sqrt(589_824 / max(1, target_width * target_height)))
                width = max(256, int(math.floor(target_width * identity_scale / 32 + 0.5)) * 32)
                height = max(256, int(math.floor(target_height * identity_scale / 32 + 0.5)) * 32)
        text = apply_scene_audio_policy(text)
        set_inputs(prompt, 8, prompt=text, width=width, height=height, length=frame_count, ref_image_size=ref_image_size)
        output_name = "MiniMax_H3_Video_Turbo_HD" if preset == "minimax-h3-video-turbo" else "MiniMax_H3"
        output_project_id = video_output_project_id(owner_id, project_id)
        set_inputs(prompt, 18, filename_prefix=f"Tanjir_Studio/{owner_id}/{output_project_id}/{output_name}", format="mp4", codec="h264")
        next_id = 1000
        for index, asset_id in enumerate(image_ids[:9]):
            filename = self._copy_asset(assets[asset_id])
            prompt[str(next_id)] = {"class_type": "LoadImage", "inputs": {"image": filename}, "_meta": {"title": f"Reference image {index + 1}"}}
            prompt["8"]["inputs"][f"ref_images.ref_image_{index}"] = [str(next_id), 0]
            next_id += 1

        video_slot = 0
        for index, asset_id in enumerate((payload.get("video_audio_refs") or [])[:2]):
            if video_slot >= 3:
                break
            filename = self._copy_asset(assets[asset_id])
            prompt[str(next_id)] = {"class_type": "VHS_LoadVideo", "inputs": {"video": filename, "force_rate": 24, "custom_width": 0, "custom_height": 0, "frame_load_cap": 0, "skip_first_frames": 0, "select_every_nth": 1}, "_meta": {"title": f"Video and audio reference {index + 1}"}}
            prompt["8"]["inputs"][f"ref_videos.ref_video_{video_slot}"] = [str(next_id), 0]
            prompt["8"]["inputs"][f"ref_video_audios.ref_video_audio_{video_slot}"] = [str(next_id), 2]
            video_slot += 1
            next_id += 1

        for index, asset_id in enumerate((payload.get("video_refs") or [])[:2]):
            if video_slot >= 3:
                break
            filename = self._copy_asset(assets[asset_id])
            prompt[str(next_id)] = {"class_type": "VHS_LoadVideo", "inputs": {"video": filename, "force_rate": 24, "custom_width": 0, "custom_height": 0, "frame_load_cap": 0, "skip_first_frames": 0, "select_every_nth": 1}, "_meta": {"title": f"Reference video {index + 1}"}}
            prompt["8"]["inputs"][f"ref_videos.ref_video_{video_slot}"] = [str(next_id), 0]
            video_slot += 1
            next_id += 1

        for index, asset_id in enumerate((payload.get("audio_refs") or [])[:2]):
            filename = self._copy_asset(assets[asset_id])
            prompt[str(next_id)] = {"class_type": "LoadAudio", "inputs": {"audio": filename}, "_meta": {"title": f"Reference audio {index + 1}"}}
            prompt["8"]["inputs"][f"ref_audios.ref_audio_{index}"] = [str(next_id), 0]
            next_id += 1
        return prompt

    def _cinema_lab(self, payload: dict, assets: dict[str, object], owner_id: str, project_id: str, profile_file: str = "cinema-lab-profile.json") -> dict:
        profile = load_workflow(self.workflow_root / profile_file)
        source_payload = dict(payload, resolution="720p")
        duration = max(1.0, min(float(payload.get("duration", 3)), 30.0))
        frame_count = int(round(duration * 24)) + 1
        final_profile = profile_file == "cinema-consistency-profile.json"
        if final_profile:
            source_payload["ratio"] = "16:9"
            # Resolve website image mentions against the actual reference order.
            refs = list(payload.get("image_refs") or [])
            order = list(payload.get("reference_order") or refs)
            offset = int(bool(payload.get("start_frame"))) + int(bool(payload.get("end_frame")))
            def picture_mention(match):
                index = int(match.group(1)) - 1
                if 0 <= index < len(order) and order[index] in refs:
                    return f"<Picture {offset + refs.index(order[index]) + 1}>"
                return match.group(0)
            source_payload["prompt"] = re.sub(r"@Image(\d+)\b", picture_mention, str(payload.get("prompt", "")))
        prompt = self._minimax(source_payload, assets, owner_id, project_id, "minimax-h3-pdd")
        prompt.update(profile.get("nodes", {}))
        for node_id, inputs in profile.get("inputs", {}).items():
            prompt[node_id]["inputs"].update(inputs)
        # The research profile originally kept only its 3-second test window.
        # Match that window to the requested duration so longer generations are
        # not rendered in full and then silently truncated to three seconds.
        if "113" in prompt:
            set_inputs(prompt, 113, length=frame_count)
        if "114" in prompt:
            set_inputs(prompt, 114, duration=duration)
        output_project_id = video_output_project_id(owner_id, project_id)
        set_inputs(prompt, 18, filename_prefix=f"Tanjir_Studio/{owner_id}/{output_project_id}/Cinema_Lab_{profile['name']}_1080p")
        if final_profile:
            base = f"Tanjir_Studio/{owner_id}/{output_project_id}/Cinema_Consistency_PDD8"
            if payload.get("resolution") == "1080p":
                # Save only the finished 1080p stream.  Keeping node 18 on the
                # native stream and node 112 on the finish stream created two
                # gallery assets for one Generate click.
                set_inputs(prompt, 18, video=["111", 0], filename_prefix=base + "_1080p")
                prompt.pop("112", None)
            else:
                for node_id in ("110", "111", "112"):
                    prompt.pop(node_id, None)
                set_inputs(prompt, 18, video=["17", 0], filename_prefix=base + "_720p")
        else:
            # Cinema Lab's profile routed the finished stream to node 18 and
            # also retained node 112 for the native stream.  One request should
            # create exactly one final asset, so keep the finish and drop the
            # duplicate native SaveVideo node.
            prompt.pop("112", None)
        return prompt

    def _minimax_edit(self, payload: dict, assets: dict[str, object], owner_id: str, project_id: str, preset: str) -> dict:
        source_id = str(payload.get("source_media") or "")
        if not source_id or source_id not in assets:
            raise GenerationError("Choose a source video to edit")
        source_row = assets[source_id]
        if not str(source_row["mime_type"] or "").startswith("video/"):
            raise GenerationError("Video Edit source must be a video")
        settings = payload.get("video_edit") or {}
        operation = str(settings.get("operation") or "remove").strip().lower()
        if operation not in {"remove", "replace", "change", "move"}:
            raise GenerationError("Unknown video edit operation")
        target = str(settings.get("target") or "").strip()
        if not target:
            raise GenerationError("Describe the object or subject to track")
        instruction = str(payload.get("prompt") or "").strip()
        if operation in {"replace", "change", "move"} and not instruction:
            raise GenerationError("Describe the replacement or requested change")
        actions = {
            "remove": f"Remove the tracked {target} completely and reconstruct the naturally hidden background behind it. Do not add another object in its place.",
            "replace": f"Replace only the tracked {target} with the requested replacement, matching its perspective, lighting, contact shadows and motion.",
            "change": f"Change only the tracked {target} as requested while preserving its motion, position and physical interaction with the scene.",
            "move": f"Move only the tracked {target} into the user-marked destination area, preserving its exact identity, scale, perspective, lighting, contact and natural motion. Reconstruct the background at its original position.",
        }
        edit_prompt = (
            "Use the supplied source video as the exact base video and temporal reference. "
            + actions[operation]
            + (f" Requested edit: {instruction}." if instruction else "")
            + " Preserve every untracked pixel as closely as possible: same people, faces, body motion, camera, framing, background, props, lighting, timing and continuity. "
            + "Do not modify anything outside the tracked target region. Produce stable edges with no flicker, morphing or duplicate objects."
        )
        base_preset = "minimax-h3-best" if preset.endswith("-best") else "minimax-h3"
        base_payload = dict(payload)
        base_payload["preset"] = base_preset
        base_payload["prompt"] = edit_prompt
        base_payload["video_refs"] = []
        base_payload["audio_refs"] = []
        base_payload["video_audio_refs"] = []
        prompt = self._minimax(base_payload, assets, owner_id, project_id, base_preset)
        ratio, resolution = str(payload.get("ratio", "16:9")), str(payload.get("resolution", "720p"))
        width, height = DIMENSIONS[ratio][resolution]
        duration = max(1.0, min(float(payload.get("duration", 5)), 150.0))
        frame_count = int(round(duration * 24)) + 1
        filename = self._copy_asset(source_row)
        prompt["2000"] = {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": "sam3.1_multiplex_fp16.safetensors"}, "_meta": {"title": "SAM3.1 object tracker"}}
        prompt["2001"] = {"class_type": "CLIPTextEncode", "inputs": {"clip": ["2000", 1], "text": target[:160]}, "_meta": {"title": "Tracked object description"}}
        prompt["2002"] = {"class_type": "VHS_LoadVideo", "inputs": {"video": filename, "force_rate": 24, "custom_width": width, "custom_height": height, "frame_load_cap": frame_count, "skip_first_frames": 0, "select_every_nth": 1}, "_meta": {"title": "Exact source video and original audio"}}
        mask_id = str(payload.get("edit_mask") or "")
        tracked_mask = ["2003", 0]
        if mask_id and mask_id in assets:
            mask_filename = self._copy_asset(assets[mask_id])
            prompt["2010"] = {"class_type": "LoadImage", "inputs": {"image": mask_filename}, "_meta": {"title": "User-painted target mask"}}
            prompt["2011"] = {"class_type": "ImageToMask", "inputs": {"image": ["2010", 0], "channel": "red"}, "_meta": {"title": "Painted first-frame mask"}}
            prompt["2012"] = {"class_type": "SAM3_VideoTrack", "inputs": {"images": ["2002", 0], "model": ["2000", 0], "initial_mask": ["2011", 0], "detection_threshold": float(settings.get("threshold") or 0.45), "max_objects": 1, "detect_interval": 1}, "_meta": {"title": "Track painted target through video"}}
            prompt["2013"] = {"class_type": "SAM3_TrackToMask", "inputs": {"track_data": ["2012", 0], "object_indices": "0"}, "_meta": {"title": "Tracked painted mask"}}
            tracked_mask = ["2013", 0]
        else:
            prompt["2003"] = {"class_type": "SAM3_Detect", "inputs": {"model": ["2000", 0], "image": ["2002", 0], "conditioning": ["2001", 0], "threshold": float(settings.get("threshold") or 0.45), "refine_iterations": 2, "individual_masks": False}, "_meta": {"title": "Track target through every frame"}}
        prompt["2004"] = {"class_type": "GrowMask", "inputs": {"mask": tracked_mask, "expand": int(settings.get("mask_expand") or 6), "tapered_corners": True}, "_meta": {"title": "Protect target edges"}}
        prompt["2005"] = {"class_type": "FeatherMask", "inputs": {"mask": ["2004", 0], "left": 8, "top": 8, "right": 8, "bottom": 8}, "_meta": {"title": "Blend edited edges"}}
        prompt["2006"] = {"class_type": "ImageCompositeMasked", "inputs": {"destination": ["2002", 0], "source": ["15", 0], "x": 0, "y": 0, "resize_source": True, "mask": ["2005", 0]}, "_meta": {"title": "Composite only the tracked edit over original frames"}}
        final_images = ["2006", 0]
        placement_id = str(payload.get("edit_placement_mask") or "")
        if placement_id and placement_id in assets:
            placement_filename = self._copy_asset(assets[placement_id])
            prompt["2020"] = {"class_type": "LoadImage", "inputs": {"image": placement_filename}, "_meta": {"title": "User-painted destination area"}}
            prompt["2021"] = {"class_type": "ImageToMask", "inputs": {"image": ["2020", 0], "channel": "red"}, "_meta": {"title": "Destination composite mask"}}
            prompt["2022"] = {"class_type": "GrowMask", "inputs": {"mask": ["2021", 0], "expand": 4, "tapered_corners": True}, "_meta": {"title": "Destination edge allowance"}}
            prompt["2023"] = {"class_type": "FeatherMask", "inputs": {"mask": ["2022", 0], "left": 10, "top": 10, "right": 10, "bottom": 10}, "_meta": {"title": "Blend destination edges"}}
            prompt["2024"] = {"class_type": "ImageCompositeMasked", "inputs": {"destination": ["2006", 0], "source": ["15", 0], "x": 0, "y": 0, "resize_source": True, "mask": ["2023", 0]}, "_meta": {"title": "Composite generated destination over original video"}}
            final_images = ["2024", 0]
        prompt["8"]["inputs"]["ref_videos.ref_video_0"] = ["2002", 0]
        prompt["17"]["inputs"]["images"] = final_images
        if bool(settings.get("preserve_audio", True)):
            prompt["17"]["inputs"]["audio"] = ["2002", 2]
        prompt["17"]["inputs"]["fps"] = 24
        output_project_id = video_output_project_id(owner_id, project_id)
        prompt["18"]["inputs"]["filename_prefix"] = f"Tanjir_Studio/{owner_id}/{output_project_id}/MiniMax_H3_Video_Edit"
        return prompt

    def _ltx(self, payload: dict, assets: dict[str, object], owner_id: str, project_id: str) -> dict:
        ref_ids = list(payload.get("image_refs") or [])
        image_id = payload.get("start_frame") or (ref_ids[0] if ref_ids else None)
        workflow_name = "video_ltx2_5_i2v.json" if image_id else "video_ltx2_5_t2v.json"
        workflow = load_workflow(self.workflow_root / workflow_name)
        subgraphs = (workflow.get("definitions") or {}).get("subgraphs") or []
        if not subgraphs:
            raise GenerationError("LTX 2.5 workflow subgraph is missing")
        subgraph = subgraphs[0]
        prompt = ui_to_api({"nodes": subgraph.get("nodes", []), "links": subgraph.get("links", [])}, self.object_info)

        ratio, resolution = str(payload.get("ratio", "16:9")), str(payload.get("resolution", "360p"))
        try:
            raw_width, raw_height = DIMENSIONS[ratio][resolution]
        except KeyError as error:
            raise GenerationError("Unsupported ratio or resolution") from error
        width = max(64, int(math.floor(raw_width / 32 + 0.5)) * 32)
        height = max(64, int(math.floor(raw_height / 32 + 0.5)) * 32)
        duration = max(1, min(int(round(float(payload.get("duration", 5)))), 30))
        text = str(payload.get("prompt", "")).strip()
        if not text:
            raise GenerationError("Prompt is required")
        if image_id:
            text = preserve_reference_text(text)

        text = apply_scene_audio_policy(text)
        set_inputs(prompt, 360, value=height)
        set_inputs(prompt, 361, value=24)
        set_inputs(prompt, 362, value=duration)
        set_inputs(prompt, 372, value=width)
        set_inputs(prompt, 376, value=text)
        set_inputs(prompt, 383, value=False)
        # The official UI workflow serializes the dynamic prompt-enhancer
        # controls positionally. Force the API form to the disabled branch;
        # otherwise Comfy validates missing sampling_mode.* inputs even though
        # the visible prompt-enhance switch is off.
        set_inputs(
            prompt,
            380,
            max_length=600,
            sampling_mode="off",
            thinking=False,
            use_default_template=True,
        )
        set_inputs(prompt, 384, unet_name="ltx-2.5-22b-distilled-transformer-bf16.safetensors", weight_dtype="default")
        set_inputs(prompt, 385, vae_name="ltx-2.5-video-vae-bf16.safetensors")
        set_inputs(prompt, 386, vae_name="ltx-2.5-audio-vae-bf16.safetensors")
        set_inputs(prompt, 387, clip_name="gemma4-12b-with-proj-ltx-2.5-bf16.safetensors", type="ltxv", device="default")
        set_inputs(prompt, 393, clip_name="gemma4-12b-with-proj-ltx-2.5-bf16.safetensors", type="ltxv", device="default")
        set_inputs(prompt, 371, model_name="ltx-2.5-latent-spatial-upscaler-x2-bf16-1.0.safetensors")

        if image_id:
            filename = self._copy_asset(assets[image_id])
            prompt["900"] = {"class_type": "LoadImage", "inputs": {"image": filename}, "_meta": {"title": "LTX start image"}}
            set_inputs(prompt, 351, input=["900", 0])

        prompt["999"] = {
            "class_type": "SaveVideo",
            "inputs": {
                "video": ["370", 0],
                "filename_prefix": f"Tanjir_Studio/{owner_id}/{video_output_project_id(owner_id, project_id)}/LTX_2_5",
                "format": "auto",
                "codec": "auto",
            },
            "_meta": {"title": "Save LTX 2.5 video"},
        }
        return prompt

    def _lustify_remix(self, payload: dict, owner_id: str, project_id: str) -> dict:
        ratio, resolution = str(payload.get("ratio", "1:1")), str(payload.get("resolution", "1080p"))
        try:
            raw_width, raw_height = DIMENSIONS[ratio][resolution]
        except KeyError as error:
            raise GenerationError("Unsupported ratio or resolution") from error
        scale = min(1.0, math.sqrt(1_500_000 / max(1, raw_width * raw_height)))
        width = max(512, round(raw_width * scale / 16) * 16)
        height = max(512, round(raw_height * scale / 16) * 16)
        text = str(payload.get("prompt", "")).strip()
        if not text:
            raise GenerationError("Prompt is required")
        seed = uuid.uuid4().int % (2**63 - 1)
        return {
            "1": {"class_type": "UNETLoader", "inputs": {"unet_name": "lustifyNSFWCheckpoint_v10Krea2.safetensors", "weight_dtype": "default"}, "_meta": {"title": "Lustify Krea 2 diffusion model"}},
            "2": {"class_type": "VAELoader", "inputs": {"vae_name": "qwen_image_vae.safetensors"}, "_meta": {"title": "Qwen Image VAE"}},
            "3": {"class_type": "CLIPLoader", "inputs": {"clip_name": "qwen3vl_4b_fp8_scaled.safetensors", "type": "krea2", "device": "default"}, "_meta": {"title": "Krea 2 Qwen3-VL encoder"}},
            "4": {"class_type": "CLIPTextEncode", "inputs": {"text": text, "clip": ["3", 0]}, "_meta": {"title": "Prompt"}},
            "5": {"class_type": "EmptySD3LatentImage", "inputs": {"width": width, "height": height, "batch_size": 1}, "_meta": {"title": "Krea 2 latent size"}},
            "6": {"class_type": "KSampler", "inputs": {"model": ["1", 0], "positive": ["4", 0], "negative": ["4", 0], "latent_image": ["5", 0], "seed": seed, "steps": 28, "cfg": 1.0, "sampler_name": "euler", "scheduler": "simple", "denoise": 1.0}, "_meta": {"title": "Krea 2 sampler"}},
            "7": {"class_type": "VAEDecode", "inputs": {"samples": ["6", 0], "vae": ["2", 0]}, "_meta": {"title": "Decode image"}},
            "8": {"class_type": "SaveImage", "inputs": {"images": ["7", 0], "filename_prefix": f"Tanjir_Studio/{owner_id}/{project_id}/Lustify_Remix"}, "_meta": {"title": "Save Remix image"}},
        }

    def _wan22_remix(self, payload: dict, assets: dict[str, object], owner_id: str, project_id: str) -> dict:
        image_id = payload.get("start_frame") or next(iter(payload.get("image_refs") or []), None)
        if not image_id or image_id not in assets:
            raise GenerationError("Wan 2.2 Remix requires one start/reference image")
        workflow = load_workflow(self.workflow_root / "wan22-remix-i2v.json")
        subgraphs = (workflow.get("definitions") or {}).get("subgraphs") or []
        if not subgraphs:
            raise GenerationError("Wan 2.2 Remix workflow subgraph is missing")
        subgraph = subgraphs[0]
        prompt = ui_to_api({"nodes": subgraph.get("nodes", []), "links": subgraph.get("links", [])}, self.object_info)
        ratio, resolution = str(payload.get("ratio", "16:9")), str(payload.get("resolution", "360p"))
        try:
            raw_width, raw_height = DIMENSIONS[ratio][resolution]
        except KeyError as error:
            raise GenerationError("Unsupported ratio or resolution") from error
        # Keep dimensions divisible by 16 and cap the first release for a 16 GB laptop GPU.
        scale = min(1.0, math.sqrt(640 * 640 / max(1, raw_width * raw_height)))
        width = max(256, round(raw_width * scale / 16) * 16)
        height = max(256, round(raw_height * scale / 16) * 16)
        duration = max(1.0, min(float(payload.get("duration", 5)), 10.0))
        length = max(17, int(round(duration * 16 / 4)) * 4 + 1)
        text = str(payload.get("prompt", "")).strip()
        if not text:
            raise GenerationError("Prompt is required")
        text = preserve_reference_text(text)
        set_inputs(prompt, 84, clip_name="umt5_xxl_fp8_e4m3fn_scaled.safetensors", type="wan", device="default")
        set_inputs(prompt, 90, vae_name="wan_2.1_vae.safetensors")
        set_inputs(prompt, 95, unet_name="Wan2.2_Remix_NSFW_i2v_14b_high_lighting_fp8_e4m3fn_v3.0.safetensors", weight_dtype="default")
        set_inputs(prompt, 96, unet_name="Wan2.2_Remix_NSFW_i2v_14b_low_lighting_fp8_e4m3fn_v3.0.safetensors", weight_dtype="default")
        set_inputs(prompt, 101, lora_name="wan2.2_i2v_lightx2v_4steps_lora_v1_high_noise.safetensors", strength_model=1.0)
        set_inputs(prompt, 102, lora_name="wan2.2_i2v_lightx2v_4steps_lora_v1_low_noise.safetensors", strength_model=1.0)
        set_inputs(prompt, 93, text=text)
        # The downloaded UI workflow stores control-widget metadata alongside sampler
        # values. ui_to_api cannot reliably map those widgets, so set the two-stage
        # LightX2V sampler inputs explicitly after conversion.
        prompt["86"]["inputs"].update({
            "add_noise": "enable", "noise_seed": uuid.uuid4().int % (2**63 - 1),
            "steps": 4, "cfg": 1.0, "sampler_name": "euler", "scheduler": "simple",
            "start_at_step": 0, "end_at_step": 2, "return_with_leftover_noise": "enable",
        })
        prompt["85"]["inputs"].update({
            "add_noise": "disable", "noise_seed": 0,
            "steps": 4, "cfg": 1.0, "sampler_name": "euler", "scheduler": "simple",
            "start_at_step": 2, "end_at_step": 4, "return_with_leftover_noise": "disable",
        })
        filename = self._copy_asset(assets[image_id])
        prompt["900"] = {"class_type": "LoadImage", "inputs": {"image": filename}, "_meta": {"title": "Wan Remix start image"}}
        set_inputs(prompt, 98, start_image=["900", 0], width=width, height=height, length=length, batch_size=1)
        set_inputs(prompt, 117, fps=16)
        prompt["999"] = {"class_type": "SaveVideo", "inputs": {"video": ["117", 0], "filename_prefix": f"Tanjir_Studio/{owner_id}/{video_output_project_id(owner_id, project_id)}/Wan22_Remix", "format": "mp4", "codec": "h264"}, "_meta": {"title": "Save Wan 2.2 Remix video"}}
        return prompt
    def _realesrgan(self, payload: dict, assets: dict[str, object], owner_id: str, project_id: str, preset: str) -> dict:
        source_id = payload.get("source_media")
        if not source_id or source_id not in assets:
            raise GenerationError("Choose an image to upscale")
        source = assets[source_id]
        if source["kind"] != "image":
            raise GenerationError("Real-ESRGAN supports images only. Use SeedVR2 for video.")
        source_path = (self.data_root / source["relative_path"]).resolve()
        if self.data_root not in source_path.parents or not source_path.is_file():
            raise GenerationError("Source image is missing")
        with Image.open(source_path) as image:
            source_width, source_height = image.size
        target = {"1080p": 1080, "1440p": 1440, "2160p": 2160}.get(str(payload.get("resolution")), 1080)
        if source_width >= source_height:
            height = target
            width = max(8, round((target * source_width / source_height) / 8) * 8)
        else:
            width = target
            height = max(8, round((target * source_height / source_width) / 8) * 8)
        filename = self._copy_asset(source)
        model_name = "RealESRGAN_x4plus_anime_6B.pth" if preset == "realesrgan-anime" else "RealESRGAN_x4plus.pth"
        return {
            "1": {"class_type": "LoadImage", "inputs": {"image": filename}, "_meta": {"title": "Source image"}},
            "2": {"class_type": "UpscaleModelLoader", "inputs": {"model_name": model_name}, "_meta": {"title": "Real-ESRGAN model"}},
            "3": {"class_type": "ImageUpscaleWithModel", "inputs": {"upscale_model": ["2", 0], "image": ["1", 0]}, "_meta": {"title": "Real-ESRGAN 4x"}},
            "4": {"class_type": "ImageScale", "inputs": {"image": ["3", 0], "upscale_method": "lanczos", "width": width, "height": height, "crop": "disabled"}, "_meta": {"title": "Target resolution"}},
            "5": {"class_type": "SaveImage", "inputs": {"images": ["4", 0], "filename_prefix": f"Tanjir_Studio/{owner_id}/{project_id}/RealESRGAN"}, "_meta": {"title": "Save upscaled image"}},
        }
    def _realesrgan_video(self, payload: dict, assets: dict[str, object], owner_id: str, project_id: str) -> dict:
        source_id = payload.get("source_media")
        if not source_id or source_id not in assets:
            raise GenerationError("Choose a video to upscale")
        source = assets[source_id]
        if source["kind"] != "video":
            raise GenerationError("This Real-ESRGAN preset supports video only. Choose an [Image] preset for images.")
        source_path = (self.data_root / source["relative_path"]).resolve()
        if self.data_root not in source_path.parents or not source_path.is_file():
            raise GenerationError("Source video is missing")
        with av.open(str(source_path)) as container:
            stream = container.streams.video[0]
            source_width, source_height = stream.width, stream.height
        target = {"1080p": 1080, "1440p": 1440, "2160p": 2160}.get(str(payload.get("resolution")), 1080)
        if source_width >= source_height:
            height = target
            width = max(8, round((target * source_width / source_height) / 8) * 8)
        else:
            width = target
            height = max(8, round((target * source_height / source_width) / 8) * 8)
        filename = self._copy_asset(source)
        return {
            "1": {"class_type": "LoadVideo", "inputs": {"file": filename}, "_meta": {"title": "Source video"}},
            "2": {"class_type": "GetVideoComponents", "inputs": {"video": ["1", 0]}, "_meta": {"title": "Extract frames, audio and FPS"}},
            "3": {"class_type": "UpscaleModelLoader", "inputs": {"model_name": "RealESRGAN_x4plus.pth"}, "_meta": {"title": "Real-ESRGAN low-VRAM model"}},
            "4": {"class_type": "ImageUpscaleWithModel", "inputs": {"upscale_model": ["3", 0], "image": ["2", 0]}, "_meta": {"title": "Tiled Real-ESRGAN video frames"}},
            "5": {"class_type": "ImageScale", "inputs": {"image": ["4", 0], "upscale_method": "lanczos", "width": width, "height": height, "crop": "disabled"}, "_meta": {"title": "Exact target resolution"}},
            "6": {"class_type": "CreateVideo", "inputs": {"images": ["5", 0], "audio": ["2", 1], "fps": ["2", 2], "bit_depth": 8, "color_space": "sRGB"}, "_meta": {"title": "Restore audio and FPS"}},
            "7": {"class_type": "SaveVideo", "inputs": {"video": ["6", 0], "filename_prefix": f"Tanjir_Studio/{owner_id}/{video_output_project_id(owner_id, project_id)}/RealESRGAN_Video", "format": "mp4", "codec": "h264"}, "_meta": {"title": "Save fast upscaled video"}},
        }
    def _seedvr(self, payload: dict, assets: dict[str, object], owner_id: str, project_id: str) -> dict:
        source_id = payload.get("source_media")
        if not source_id or source_id not in assets:
            raise GenerationError("Choose an image or video to upscale")
        source = assets[source_id]
        is_video = source["kind"] == "video"
        workflow = load_workflow(self.workflow_root / ("seedvr-video.json" if is_video else "seedvr-photo.json"))
        prompt = ui_to_api(workflow, self.object_info)
        filename = self._copy_asset(source)
        target = {"1080p": 1080, "1440p": 1440, "2160p": 2160}.get(str(payload.get("resolution")), 1080)
        set_inputs(prompt, 10, seed=42, resolution=target, max_resolution=0, batch_size=5 if is_video else 1, uniform_batch_size=True, color_correction="lab", temporal_overlap=0, prepend_frames=0, input_noise_scale=0.0, latent_noise_scale=0.0, offload_device="cpu", enable_debug=False)
        if is_video:
            set_inputs(prompt, 21, file=filename)
            set_inputs(prompt, 23, filename_prefix=f"Tanjir_Studio/{owner_id}/{video_output_project_id(owner_id, project_id)}/SeedVR_Video")
        else:
            set_inputs(prompt, 16, image=filename)
            set_inputs(prompt, 15, filename_prefix=f"Tanjir_Studio/{owner_id}/{project_id}/SeedVR_Image")
        return prompt








