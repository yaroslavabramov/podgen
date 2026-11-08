"""Freeze identical author content, full historical inputs and a reproducible excerpt."""

import copy
import json
import re
from pathlib import Path

from artifacts import save, save_bytes, sha

from podgen.runtime.evaluation.validation import load_unique_json, validate_output

TASK = {
    "scope": "opening_excerpt_only",
    "instruction": (
        "For this call, write only the opening approximately two minutes of the supplied episode, "
        "aiming for 220–270 spoken Russian words, then stop. The full-episode duration and plan "
        "remain context for the larger work, not the requested output length. "
        "Cover the inspection and the transition to asking people questions. "
        "Choose the opening words yourself. Return the complete requested excerpt in the ordinary "
        "WriteOutput utterances structure, with the supplied speaker labels and output_language."
    ),
    "target_seconds": 120,
    "target_words": {"min": 220, "max": 270},
}


def payload(body):
    assert len(body["input"]) == 1
    assert len(body["input"][0]["content"]) == 1
    return json.loads(body["input"][0]["content"][0]["text"])


def response_text(response, provider):
    if provider == "anthropic":
        return "".join(row["text"] for row in response.get("content", []) if row["type"] == "text")
    return "".join(
        block["text"]
        for row in response.get("output", [])
        if row["type"] == "message"
        for block in row["content"]
        if block["type"] == "output_text"
    )


def script(response, provider, speakers):
    if provider == "anthropic":
        assert response.get("stop_reason") == "end_turn", "Incomplete/refused message"
    else:
        assert response.get("status") == "completed", "Incomplete/refused response"
    text = response_text(response, provider)
    load_unique_json(text)
    return validate_output("draft", text, speaker_labels=speakers)


def spoken(output):
    return "\n\n".join(row["text"] for row in output["utterances"]) + "\n"


def words(text):
    return len(re.findall(r"[^\W_]+(?:[’'−–-][^\W_]+)*", text, flags=re.UNICODE))


def historical_excerpt(output):
    """An exact prefix ending at a sentence boundary nearest 245 words; never rewrite it."""
    text = spoken(output)
    ends = [match.end() for match in re.finditer(r"[.!?…](?:[»”\"])?(?=\s|$)", text)]
    options = [(abs(words(text[:end]) - 245), end) for end in ends if 220 <= words(text[:end]) <= 270]
    if not options:
        raise ValueError("No historical sentence boundary in the predeclared word window")
    _, end = min(options)
    return text[:end] + "\n", dict(start=0, end=end, word_count=words(text[:end]), exact_prefix=True)


def prepare(source, response, directory, accepted_plan=None):
    directory = Path(directory)
    original = json.loads(Path(source).read_bytes())
    full = payload(original)
    assert full["output_language"] == "ru"
    assert "opening_task" not in full
    common = copy.deepcopy(full)
    common["opening_task"] = TASK
    speakers = [row["label"] for row in common["assignment"]["speakers"]]
    historical = script(json.loads(Path(response).read_bytes()), "openai", speakers)
    excerpt, selection = historical_excerpt(historical)
    save_bytes(directory / "source/request.json", Path(source).read_bytes())
    save_bytes(directory / "source/response.json", Path(response).read_bytes())
    if accepted_plan:
        save_bytes(directory / "source/accepted-plan.json", Path(accepted_plan).read_bytes())
    save_bytes(directory / "common/instructions.txt", original["instructions"].encode())
    save(directory / "common/payload.json", common)
    save(directory / "common/schema.json", original["text"]["format"]["schema"])
    save(directory / "historical/full-script.json", historical)
    save_bytes(directory / "historical/opening.md", excerpt.encode())
    save(directory / "historical/selection.json", selection)
    save(
        directory / "conditions.json",
        dict(
            contract="blind-author-openings/1",
            source_request_sha256=sha(Path(source).read_bytes()),
            source_response_sha256=sha(Path(response).read_bytes()),
            instructions_sha256=sha(original["instructions"].encode()),
            story_sha256=sha(json.dumps(full["story"], ensure_ascii=False, sort_keys=True).encode()),
            task=TASK,
            change="Only opening_task is added to the full original user payload; author instructions and story are unchanged.",
            assessment="One sample per author; no repair, editorial feedback, reranking or length-driven regeneration.",
            word_count="Unicode word groups including numbers, with internal hyphens/apostrophes counted as one word.",
        ),
    )
    return original, common


def author_request(original, common, provider, model):
    text = json.dumps(common, ensure_ascii=False, separators=(",", ":"))
    if provider == "openai":
        request = copy.deepcopy(original)
        request["model"] = model
        request["input"][0]["content"][0]["text"] = text
        request.pop("metadata", None)
        request.update(max_output_tokens=8192, reasoning={"effort": "medium"}, service_tier="default")
        return request
    # Only unsupported wire constraints move into descriptions; validate the original WriteOutput locally.
    schema = copy.deepcopy(original["text"]["format"]["schema"])

    def adapt(node):
        if isinstance(node, dict):
            minimum = node.pop("minLength", None)
            if minimum is not None:
                node["description"] = node.get("description", "") + f" Minimum length: {minimum}."
            for value in node.values():
                adapt(value)
        elif isinstance(node, list):
            for value in node:
                adapt(value)

    adapt(schema)
    return dict(
        model=model,
        system=original["instructions"],
        messages=[dict(role="user", content=[dict(type="text", text=text)])],
        max_tokens=8192,
        thinking={"type": "adaptive"},
        output_config={"effort": "medium", "format": {"type": "json_schema", "schema": schema}},
        stream=False,
    )
