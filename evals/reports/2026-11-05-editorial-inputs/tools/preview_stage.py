"""Reuse a locally admitted preview state; forwards arguments only to the offline preview command."""
import json
from pathlib import Path
import subprocess
import sys

if len(sys.argv) < 3:
    raise SystemExit('Usage: preview_stage.py STATE.json --stage STAGE ... --out FILE')
state = json.loads(Path(sys.argv[1]).read_text())
raise SystemExit(subprocess.run([*state['cli'], *state['preview'], *sys.argv[2:]], check=False).returncode)
