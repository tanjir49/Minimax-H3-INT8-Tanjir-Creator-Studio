# Release validation — 2026-09-07

- Python syntax and JSON parsing passed.
- All frontend JavaScript files passed Node syntax checking.
- Source-only release scan passed; only the specifically allowed UI screenshots are included as raster files.
- An isolated Studio instance on port 8876 used a fresh database outside the release, with ComfyUI deliberately disconnected.
- First-run setup code creation, administrator creation, session authentication, duplicate setup rejection, projects, 23 presets, empty gallery, and SVG preview response passed.
- The clean account opened Image, Video, Voice, and Music controls in Chrome; the owner approved the screenshots for publication.
- Fixed two first-run issues in this distribution: missing bootstrap-code creation and using event.currentTarget after an await in the login handler.

Testing used the installed Python runtime and pinned package versions. A fresh pip installation and full GPU generation across all presets have not been verified. Third-party nodes/models/runtimes are external prerequisites.
