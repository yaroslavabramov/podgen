"""One localized writing reference, frozen as full text rather than reloaded on resume."""

import hashlib
from importlib.resources import files

from podgen.domain.errors import ContractError

AUTHOR_STAGES = ("draft", "revise", "dual-revise", "revise-1", "revise-2")


def load_reference(output_language):
    language = output_language.lower().replace("_", "-").split("-")[0]
    if language != "ru":
        return None
    resource = files("podgen.writing.prompts").joinpath("examples", "ru.md")
    if not resource.is_file():
        raise ContractError("craft_reference_unavailable")
    raw = resource.read_bytes()
    text = raw.decode("utf-8")
    if not text.strip():
        raise ContractError("empty_craft_reference")
    return dict(language=language, text=text, sha256=hashlib.sha256(raw).hexdigest())


def pinned_reference(manifest):
    reference = manifest.get("craft_reference")
    if reference is None:
        return None
    language = manifest["output_language"].lower().replace("_", "-").split("-")[0]
    if (
        not isinstance(reference, dict)
        or set(reference) != {"language", "text", "sha256"}
        or reference["language"] != language
        or language != "ru"
        or not isinstance(reference["text"], str)
        or not reference["text"].strip()
        or hashlib.sha256(reference["text"].encode()).hexdigest() != reference["sha256"]
    ):
        raise ContractError("invalid_pinned_craft_reference")
    return reference["text"]


def attach_reference(payload, stage, reference):
    if stage in AUTHOR_STAGES and reference is not None:
        payload["craft_reference"] = reference
