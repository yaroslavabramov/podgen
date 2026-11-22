"""Small byte-preserving Markdown composition: includes, named sections and conditions."""

import re

from podgen.domain.errors import ContractError

TOKENS = re.compile(r"\{\{((?:[^{}]|\{(?:format|sources)\})+)\}\}")
SECTIONS = re.compile(r"\{\{section ([a-z-]+)\}\}(.*?)\{\{endsection\}\}", re.DOTALL)


def render(path, context, read, *, stack=(), memo=None):
    if path in stack or len(stack) >= 32:
        raise ContractError("cyclic_writing_prompt_include")
    filename, _, section = path.partition("#")
    memo = {} if memo is None else memo
    if filename not in memo:
        memo[filename] = read(filename)
    raw = memo[filename]
    definitions = SECTIONS.findall(raw)
    sections = dict(definitions)
    if len(definitions) != len(sections):
        raise ContractError("duplicate_writing_prompt_section")
    if section and section not in sections:
        raise ContractError("unknown_writing_prompt_section")
    text = sections[section] if section else SECTIONS.sub("", raw)
    # Validate every local section, including branches not selected in this format.
    # A misspelled or unclosed directive must never disappear with an unused block.
    for fragment in (SECTIONS.sub("", raw), *sections.values()):
        _render_text(fragment, context, lambda _: "")
    return _render_text(
        text, context, lambda child: render(child, context, read, stack=(*stack, path), memo=memo)
    )


def _render_text(text, context, include):
    remainder = TOKENS.sub("", text)
    if "{{" in remainder:
        raise ContractError("malformed_writing_prompt_directive")
    result, frames, cursor = [], [], 0
    active = True

    def condition(expression):
        key, equal, value = expression.partition("=")
        if key not in context:
            raise ContractError("unknown_writing_prompt_condition")
        if equal and value not in {
            "sources": ("cards", "research", "sources"),
            "format": ("solo", "dialogue"),
        }.get(key, ()):
            raise ContractError("unknown_writing_prompt_condition_value")
        if not equal and not isinstance(context[key], bool):
            raise ContractError("unknown_writing_prompt_condition_value")
        return context[key] == value if equal else context[key]

    for token in TOKENS.finditer(text):
        if active:
            result.append(text[cursor : token.start()])
        directive = token[1]
        if directive.startswith("if "):
            selected = condition(directive[3:])
            frames.append([active, selected, False])
            active = active and selected
        elif directive.startswith("elif ") or directive == "else":
            if not frames:
                raise ContractError("unbalanced_writing_prompt_condition")
            parent, selected, otherwise = frames[-1]
            if otherwise:
                raise ContractError("unbalanced_writing_prompt_condition")
            branch = directive == "else" or condition(directive[5:])
            active = parent and not selected and branch
            frames[-1][1] = selected or branch
            frames[-1][2] = directive == "else"
        elif directive == "endif":
            if not frames:
                raise ContractError("unbalanced_writing_prompt_condition")
            active = frames.pop()[0]
        elif directive.startswith("include:"):
            try:
                child, *filters = directive[8:].format_map(context).split("|")
            except (KeyError, ValueError):
                raise ContractError("unknown_writing_prompt_include_context") from None
            if filters not in ([], ["rstrip"]):
                raise ContractError("unsupported_writing_prompt_filter")
            if active:
                value = include(child)
                result.append(value.rstrip() if filters else value)
        else:
            raise ContractError("unsupported_writing_prompt_directive")
        cursor = token.end()
    if frames:
        raise ContractError("unbalanced_writing_prompt_condition")
    if active:
        result.append(text[cursor:])
    return "".join(result)
