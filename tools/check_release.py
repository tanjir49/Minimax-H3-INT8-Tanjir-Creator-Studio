"""Check tracked/stageable release files, without reading ignored private data."""
from pathlib import Path
import ast
import json
import re
import subprocess

ROOT = Path(__file__).resolve().parents[1]
files = subprocess.check_output(
    ['git', 'ls-files', '--cached', '--others', '--exclude-standard', '-z'],
    cwd=ROOT,
).decode().split('\0')
errors = []
blocked = {'.png', '.jpg', '.jpeg', '.webp', '.gif', '.avif', '.mp4', '.webm',
           '.mov', '.mkv', '.avi', '.wav', '.mp3', '.flac', '.ogg', '.m4a',
           '.db', '.sqlite', '.log', '.safetensors', '.gguf', '.pt', '.pth',
           '.ckpt', '.bin', '.pem', '.key', '.zip', '.pyc'}
screenshots = {'docs/screenshots/login.png', 'docs/screenshots/workspace.png',
               'docs/screenshots/gallery.png', 'docs/screenshots/video.png',
               'docs/screenshots/voice.png', 'docs/screenshots/music.png'}
secret = re.compile(r'gh[pousr]_[A-Za-z0-9]{25,}|github_pat_[A-Za-z0-9_]{30,}'
                    r'|sk-[A-Za-z0-9]{25,}|-----BEGIN [A-Z ]*PRIVATE KEY-----')
for name in sorted(set(filter(None, files))):
    path = ROOT / name
    if any(part in {'data', 'backups', 'logs', '.venv', '__pycache__', 'models', 'runtime'} for part in path.relative_to(ROOT).parts):
        errors.append(f'Private runtime path: {name}')
    if path.suffix.lower() in blocked and name not in screenshots:
        errors.append(f'Excluded file type: {name}')
        continue
    if name in screenshots:
        continue
    if path.name.startswith('.env') and path.name != '.env.example':
        errors.append(f'Environment secrets: {name}')
        continue
    text = path.read_text(encoding='utf-8-sig')
    if secret.search(text):
        errors.append(f'Potential credential: {name}')
    if re.search(r'data:(?:image|video|audio)/[^;]+;base64,', text):
        errors.append(f'Embedded media: {name}')
    if path.suffix == '.py':
        ast.parse(text, filename=name)
    if path.suffix == '.json':
        json.loads(text)
if errors:
    raise SystemExit('\n'.join(errors))
print(f'PASS: {len(set(filter(None, files)))} release files; syntax, JSON, media and common-secret checks passed.')
