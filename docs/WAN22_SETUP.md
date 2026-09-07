# Original Wan 2.2 I2V setup

The removed `Wan 2.2 Remix I2V` preset used third-party Remix checkpoints. They are no longer part of Studio or this release. Use the original Comfy-Org Wan 2.2 I2V files below in a standard Wan 2.2 ComfyUI workflow.

## One-click download on Windows

Run `Download MiniMax H3 Files.cmd`, enter the ComfyUI folder, then choose:

- **5** — original Wan 2.2 I2V 14B core pack
- **6** — optional LightX2V 4-step LoRAs

The downloader places each file in its correct `ComfyUI/models/` subfolder and resumes interrupted downloads.

## Direct official links

Core diffusion models:

- [Wan 2.2 I2V high-noise 14B FP8](https://huggingface.co/Comfy-Org/Wan_2.2_ComfyUI_Repackaged/resolve/main/split_files/diffusion_models/wan2.2_i2v_high_noise_14B_fp8_scaled.safetensors) → `models/diffusion_models/`
- [Wan 2.2 I2V low-noise 14B FP8](https://huggingface.co/Comfy-Org/Wan_2.2_ComfyUI_Repackaged/resolve/main/split_files/diffusion_models/wan2.2_i2v_low_noise_14B_fp8_scaled.safetensors) → `models/diffusion_models/`

Additional required files:

- [UMT5 XXL FP8 text encoder](https://huggingface.co/Comfy-Org/Wan_2.1_ComfyUI_repackaged/resolve/main/split_files/text_encoders/umt5_xxl_fp8_e4m3fn_scaled.safetensors) → `models/text_encoders/`
- [Wan 2.1 VAE](https://huggingface.co/Comfy-Org/Wan_2.2_ComfyUI_Repackaged/resolve/main/split_files/vae/wan_2.1_vae.safetensors) → `models/vae/`

Optional 4-step acceleration:

- [LightX2V high-noise LoRA](https://huggingface.co/Comfy-Org/Wan_2.2_ComfyUI_Repackaged/resolve/main/split_files/loras/wan2.2_i2v_lightx2v_4steps_lora_v1_high_noise.safetensors) → `models/loras/`
- [LightX2V low-noise LoRA](https://huggingface.co/Comfy-Org/Wan_2.2_ComfyUI_Repackaged/resolve/main/split_files/loras/wan2.2_i2v_lightx2v_4steps_lora_v1_low_noise.safetensors) → `models/loras/`

Update ComfyUI before loading a current original Wan 2.2 workflow. These model files retain their upstream licenses and are not redistributed by this repository.
