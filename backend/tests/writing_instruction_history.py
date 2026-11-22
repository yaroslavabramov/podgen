"""Exact prior prompt bodies used by historical request comparisons."""

import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).parent / "fixtures/writing-instructions"


def historical_instructions(identity):
    catalog = json.loads((ROOT / "catalog.json").read_text())
    text = (ROOT / catalog[identity]).read_bytes().decode("utf-8")
    assert hashlib.sha256(text.encode("utf-8")).hexdigest() == identity
    return text
