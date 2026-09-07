# Workflow setup

The ten JSON files in `workflows/` are the deployed Studio graph templates and cinema overrides, sanitized for distribution. Load image/video inputs are blank; select your own files when using graphs directly. In Studio these inputs are populated from your own uploads.

For the main MiniMax H3 INT8 graph, Windows users can run `Download MiniMax H3 Files.cmd`. The downloader presents the core and optional packs separately and puts each selected model in its expected ComfyUI model directory. It does not install executable custom nodes; use ComfyUI Manager to resolve missing nodes after opening the workflow.

Open each graph in ComfyUI and resolve missing nodes/models before using its Studio preset. `cinema-lab-profile.json` and `cinema-consistency-profile.json` are overrides applied by Studio, not standalone ComfyUI UI graphs. Model filenames matter: install the matching files or update both graph templates and the corresponding hardcoded defaults in `generation.py`.

[workflow-inventory.json](workflow-inventory.json) lists node types, model filenames, and upstream URLs found in the graphs. It also includes dynamic API node names from generation.py. Notes embedded in the graphs retain upstream download pointers.

The source installation has these custom-node packages: acestep-cpp-comfyui, Comfyui_Minimax_h3_latent_Upscaler, ComfyUI-AspectResolutionPreset, ComfyUI-GGUF, ComfyUI-KJNodes, ComfyUI-MiniMax-H3-PDD-Acc, ComfyUI-MiniMaxH3-TeaCache, ComfyUI-SeedVR2_VideoUpscaler, ComfyUI-SolAttn_triton, ComfyUI-VideoHelperSuite, and minimax-h3-firstblockcache. This is an observed installation inventory, not a verified minimal installer or a guarantee that every package revision is interchangeable. Some acceleration nodes may need compatible local patches.

Model weights, CUDA/PyTorch, custom nodes, llama.cpp, and IndicF5 are installed separately under their own licenses. Download only the models for features you intend to use. The full generation matrix has not been tested on a clean machine; unsupported/missing nodes must be resolved in ComfyUI before a preset can render.
