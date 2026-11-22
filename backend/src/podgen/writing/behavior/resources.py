"""Lazy live prompt metadata: resolving behavior never loads editable instruction files."""

import hashlib
from collections.abc import Mapping

from podgen.writing.prompts import composition, load_role
from podgen.writing.prompts.composition import ORDINARY


class CurrentResources(Mapping):
    def __iter__(self):
        return iter(("formats", "readings", "repairs", "craft_ru"))

    def __len__(self):
        return 4

    def __getitem__(self, key):
        def entry(role, format="solo"):
            prompt = load_role(role, format)
            return dict(version=prompt.version, sha256=hashlib.sha256(prompt.body.encode()).hexdigest())

        if key == "formats":
            return {
                fmt: {role: entry(role, fmt) for role in sorted(ORDINARY)} for fmt in ("solo", "dialogue")
            }
        if key == "readings":
            return {
                role: entry("reader-review" if role.startswith("reader-review-") else role)
                for role in (
                    "reader-review-1",
                    "reader-review-2",
                    "reader-review-3",
                    "fact-review",
                    "dual-revise",
                    "revision-verification",
                    "fact-verification",
                )
            }
        if key == "repairs":
            return {role: entry(role) for role in ("anchor-repair", "plan-target-repair")}
        if key == "craft_ru":
            return hashlib.sha256(composition.read("style-reference-ru.md").encode()).hexdigest()
        raise KeyError(key)


class ReleasedResources(Mapping):
    """Resolve immutable catalogs only when a fresh historical prompt is requested."""

    def __init__(self, catalog, overlays):
        self.catalog, self.overlays = catalog, overlays
        self._snapshot = None

    def _values(self):
        if self._snapshot is None:
            import json

            resources = json.loads(composition.read("profiles/" + self.catalog, historical=True))
            for catalog in self.overlays:
                change = json.loads(composition.read("profiles/" + catalog, historical=True))
                for format, roles in change["formats"].items():
                    resources["formats"][format].update(roles)
                resources["readings"].update(change["readings"])
            self._snapshot = resources
        return self._snapshot

    def __iter__(self):
        return iter(("formats", "readings", "repairs", "craft_ru"))

    def __len__(self):
        return 4

    def __getitem__(self, key):
        return self._values()[key]
