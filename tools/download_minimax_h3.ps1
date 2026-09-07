param([string]$ComfyUIRoot)

$ErrorActionPreference = 'Stop'

if (-not $ComfyUIRoot) {
    $ComfyUIRoot = Read-Host 'Enter your ComfyUI folder (example: C:\ComfyUI)'
}
$ComfyUIRoot = [IO.Path]::GetFullPath($ComfyUIRoot.Trim('"'))
if (-not (Test-Path -LiteralPath $ComfyUIRoot -PathType Container)) {
    throw "ComfyUI folder does not exist: $ComfyUIRoot"
}

$modelRoot = Join-Path $ComfyUIRoot 'models'
$packs = [ordered]@{
    '1' = @{
        Name = 'Core MiniMax H3 INT8 Ref2VA pack (required)'
        Files = @(
            @('diffusion_models/minimax_h3_ref2va_pruned_int8_convrot.safetensors', 'https://huggingface.co/Comfy-Org/MiniMax-H3/resolve/main/diffusion_models/minimax_h3_ref2va_pruned_int8_convrot.safetensors'),
            @('text_encoders/qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors', 'https://huggingface.co/Comfy-Org/MiniMax-H3/resolve/main/text_encoders/qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors'),
            @('vae/minimax_h3_video_vae_fp16.safetensors', 'https://huggingface.co/Comfy-Org/MiniMax-H3/resolve/main/vae/minimax_h3_video_vae_fp16.safetensors'),
            @('vae/minimax_h3_audio_vae_fp32.safetensors', 'https://huggingface.co/Comfy-Org/MiniMax-H3/resolve/main/vae/minimax_h3_audio_vae_fp32.safetensors')
        )
    }
    '2' = @{
        Name = 'Turbo 4-step add-on (optional)'
        Files = @(
            @('loras/minimax_h3_ref2v_turbo_4step_v0.1_comfyui_bf16.safetensors', 'https://huggingface.co/Comfy-Org/MiniMax-H3/resolve/main/loras/minimax_h3_ref2v_turbo_4step_v0.1_comfyui_bf16.safetensors')
        )
    }
    '3' = @{
        Name = 'PDD Acc 8-step add-on (optional)'
        Files = @(
            @('pdd_acc/minimax_h3_ref2va_pdd_acc_8step_comfyui.safetensors', 'https://huggingface.co/aptech0081/MiniMax-H3-Acc-LoRAs-ComfyUI/resolve/main/minimax_h3_ref2va_pdd_acc_8step_comfyui.safetensors')
        )
    }
    '4' = @{
        Name = '3D latent upscaler (optional)'
        Files = @(
            @('latent_upscale_models/minimax_h3_latent_upscaler_3d_fp16.safetensors', 'https://huggingface.co/LBH-123-AI/Minimax_h3_latent_Upscaler/resolve/main/minimax_h3_latent_upscaler_3d_fp16.safetensors')
        )
    }
}

Write-Host ''
Write-Host 'Nothing is bundled in this workflow repository.' -ForegroundColor Cyan
Write-Host 'Model files come from their upstream publishers and keep their own licenses.'
Write-Host 'MiniMax H3 weights use the MiniMax-H3 Community License Agreement:'
Write-Host 'https://huggingface.co/Comfy-Org/MiniMax-H3'
Write-Host ''
foreach ($entry in $packs.GetEnumerator()) {
    Write-Host ("[{0}] {1}" -f $entry.Key, $entry.Value.Name)
}
Write-Host '[A] Core pack plus every optional add-on'
$choice = (Read-Host 'Choose a pack').Trim().ToUpperInvariant()
$selected = if ($choice -eq 'A') { @('1', '2', '3', '4') } elseif ($packs.Contains($choice)) { @($choice) } else { throw 'Invalid selection.' }

if (-not (Get-Command curl.exe -ErrorAction SilentlyContinue)) {
    throw 'curl.exe was not found. Install current Windows curl and run again.'
}

foreach ($key in $selected) {
    Write-Host ("`n{0}" -f $packs[$key].Name) -ForegroundColor Yellow
    foreach ($file in $packs[$key].Files) {
        $relativePath, $url = $file
        $destination = Join-Path $modelRoot ($relativePath -replace '/', [IO.Path]::DirectorySeparatorChar)
        $folder = Split-Path -Parent $destination
        New-Item -ItemType Directory -Force -Path $folder | Out-Null
        if ((Test-Path -LiteralPath $destination) -and (Get-Item -LiteralPath $destination).Length -gt 1048576) {
            Write-Host "Already present: $relativePath"
            continue
        }
        Write-Host "Downloading: $relativePath"
        & curl.exe -L --fail --retry 3 --retry-delay 5 -C - --output $destination $url
        if ($LASTEXITCODE -ne 0) { throw "Download failed: $relativePath" }
    }
}

Write-Host "`nSelected files are ready under: $modelRoot" -ForegroundColor Green
Write-Host 'Restart ComfyUI, then load workflows/minimax-h3.json.'
