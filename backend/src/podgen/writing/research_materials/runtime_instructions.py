"""Role-selected operational Markdown, pinned into the complete saved request."""

from podgen.writing.prompts.composition import render


def attach(body, *, anchored=False, planning_selection=True):
    return body + render("durable-tools.md", anchored=anchored, planning_selection=planning_selection)
