"""Review-specific regression probes; this is not a general semantic classifier."""

import json
import re
from pathlib import Path

BASELINE = json.loads((Path(__file__).parent / "fixtures/own_plan_contamination.json").read_text())


def strings(value, path=""):
    if isinstance(value, dict):
        for key, child in value.items():
            yield from strings(child, path + "/" + key)
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from strings(child, path + "/" + str(index))
    elif isinstance(value, str):
        yield path, value


def normalized(text):
    return " ".join(text.casefold().split())


def editorial_leaks(case, visible):
    leaks = []
    # Detect this complete reviewed ordered guide, not its scientific statements wherever quoted.
    payload = json.loads(visible["input"][0]["content"][0]["text"])
    questions = [row["question"] for row in payload["dossier"].get("threads", [])]
    if questions == BASELINE["thread_questions"][case]:
        leaks.append(("prewritten_thread_route", "/input/dossier/threads"))
    semantic_view = dict(
        instructions=visible["instructions"], input=payload, schema=visible["text"]["format"]["schema"]
    )
    for path, text in strings(semantic_view):
        for probe in BASELINE["cases"][case]:
            if normalized(probe["text"]) in normalized(text):
                leaks.append((probe["kind"], path))
        # Independent facets of the old GPS experiment: arithmetic survives a renamed illustration.
        if case == "gps-clock" and re.search(r"\b450\b", text) and re.search(r"\b650\b", text):
            leaks.append(("prepared_measurement_pair", path))
        if case == "gps-clock" and all(
            re.search(r"\b" + number + r"\b", text) for number in ("1100", "1000", "50")
        ):
            leaks.append(("prepared_arithmetic_result", path))
    return leaks


def inject_reviewed_leaks(data, case):
    """Negative control: real corpus and bindings remain; old editorial decisions enter shared again."""
    probes = BASELINE["cases"][case]
    jobs = [p["text"] for p in probes if p["kind"] == "preassigned_host_scene"]
    for speaker, text in zip(data["assignment"]["speakers"], jobs, strict=True):
        speaker["function"] = text
    data["assignment"]["style_guidance"] = [
        p["text"] for p in probes if p["kind"] != "preassigned_host_scene"
    ]

    label = data["dossier"]["knowledge_cards"][0]["label"]
    data["dossier"]["threads"] = [
        dict(
            question=question,
            explanation="Исторически предписанный порядок рассказа.",
            support_labels=[label],
            limitations=[],
        )
        for question in BASELINE["thread_questions"][case]
    ]
