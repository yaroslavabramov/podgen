"""Pin all instructional runtime messages; old manifests retain their published text."""

from podgen.domain.canonical import digest
from podgen.domain.errors import ContractError

from . import composition, markdown
from .snapshot import instruction_snapshot

KEY = "research_runtime_instructions"
VERSION = "research-runtime-instructions/1"
BLOCKS = ("checkpoint", "deferred", "structured")
LEGACY_SHA256 = "7369a55444831df09454a1e82ead1781bf3ac0f3bdd03ecc56bdcb043b07f1b8"


@instruction_snapshot()
def pin():
    blocks = {name: composition.render("research-continuation.md#" + name) for name in BLOCKS}
    value = dict(contract=VERSION, blocks=blocks, sha256=digest(blocks))
    from_manifest({KEY: value})
    return value


def legacy():
    raw = composition.read("runtime/research-continuation-v1.md", historical=True)
    blocks = {
        name: markdown.render("runtime/research-continuation-v1.md#" + name, {}, lambda _: raw)
        for name in BLOCKS
    }
    if digest(blocks) != LEGACY_SHA256:
        raise ContractError("legacy_research_runtime_instructions_changed")
    return blocks


def from_manifest(manifest):
    if KEY not in manifest:
        return legacy()
    value = manifest[KEY]
    if (
        not isinstance(value, dict)
        or set(value) != {"contract", "blocks", "sha256"}
        or value["contract"] != VERSION
        or not isinstance(value["blocks"], dict)
        or set(value["blocks"]) != set(BLOCKS)
        or any(not isinstance(text, str) or not text.strip() for text in value["blocks"].values())
        or digest(value["blocks"]) != value["sha256"]
    ):
        raise ContractError("invalid_pinned_research_runtime_instructions")
    return value["blocks"]
