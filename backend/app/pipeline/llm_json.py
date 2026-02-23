"""Robust JSON extraction from LLM responses."""

import json


def _extract_first_json_object(text: str) -> str | None:
    object_start = text.find("{")
    array_start = text.find("[")
    starts = [idx for idx in (object_start, array_start) if idx != -1]
    if not starts:
        return None
    start = min(starts)

    open_char = text[start]
    close_char = "}" if open_char == "{" else "]"
    depth = 0
    in_string = False
    escaped = False

    for idx in range(start, len(text)):
        ch = text[idx]
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue

        if ch == '"':
            in_string = True
        elif ch == open_char:
            depth += 1
        elif ch == close_char:
            depth -= 1
            if depth == 0:
                return text[start:idx + 1]

    return None


def parse_llm_json(raw: str) -> dict | list:
    """Parse JSON from LLM output, handling markdown fences and surrounding text.

    Raises ValueError if no valid JSON found.
    """
    if not raw or not raw.strip():
        raise ValueError("LLM returned empty response")

    candidates: list[str] = []

    stripped = raw.strip()
    candidates.append(stripped)

    # Strip markdown code fences (closed or unclosed/truncated)
    if stripped.startswith("```"):
        lines = stripped.splitlines()
        # Remove first line (```json or ```)
        inner_lines = lines[1:]
        # Remove last line if it's a closing fence
        if inner_lines and inner_lines[-1].strip() == "```":
            inner_lines = inner_lines[:-1]
        inner = "\n".join(inner_lines).strip()
        if inner:
            candidates.append(inner)

    # Extract first JSON object/array from each candidate
    for candidate in list(candidates):
        extracted = _extract_first_json_object(candidate)
        if extracted and extracted != candidate:
            candidates.append(extracted)

    tried = set()
    for candidate in candidates:
        if candidate in tried:
            continue
        tried.add(candidate)
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            continue

    raise ValueError(f"No valid JSON found in LLM response: {raw[:200]}")
