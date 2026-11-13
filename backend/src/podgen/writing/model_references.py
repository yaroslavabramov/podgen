"""Versioned wire aliases, bound to the complete frozen packet; persisted references remain card IDs."""

from copy import deepcopy
from dataclasses import dataclass

from podgen.domain.canonical import digest
from podgen.domain.errors import ContractError

from .card_catalog import catalog
from .projection_versions import creative_materials, manifest_version

CATALOG_KEY = "model_material_references"
REFERENCE_LISTS = {"material_refs", "support_labels", "supports", "material_catalog"}


def material_aliases(card_ids):
    return {identity: f"M{index}" for index, identity in enumerate(card_ids, 1)}


@dataclass(frozen=True)
class ModelReferences:
    packet_digest: str
    card_ids: tuple[str, ...]

    @classmethod
    def create(cls, packet):
        return cls(digest(packet.model_dump(mode="json")), tuple(catalog(packet)))

    @classmethod
    def restore(cls, data):
        if (
            not isinstance(data, dict)
            or set(data) != {"contract", "packet_digest", "entries"}
            or data["contract"] != "model-material-references/1"
            or not isinstance(data.get("packet_digest"), str)
            or len(data["packet_digest"]) != 64
            or any(char not in "0123456789abcdef" for char in data["packet_digest"])
        ):
            raise ContractError("invalid_model_material_catalog")
        entries = data["entries"]
        if not isinstance(entries, list) or not entries:
            raise ContractError("invalid_model_material_catalog")
        ids = []
        for index, entry in enumerate(entries, 1):
            if (
                not isinstance(entry, dict)
                or set(entry) != {"ref", "card_id"}
                or entry["ref"] != f"M{index}"
                or not isinstance(entry["card_id"], str)
                or not entry["card_id"].strip()
            ):
                raise ContractError("invalid_model_material_catalog")
            ids.append(entry["card_id"])
        if len(set(ids)) != len(ids):
            raise ContractError("invalid_model_material_catalog")
        return cls(data["packet_digest"], tuple(ids))

    @property
    def aliases(self):
        return tuple(material_aliases(self.card_ids).values())

    def as_payload(self):
        return dict(
            contract="model-material-references/1",
            packet_digest=self.packet_digest,
            entries=[
                dict(ref=ref, card_id=identity)
                for ref, identity in zip(self.aliases, self.card_ids, strict=True)
            ],
        )

    def translate(self, value, *, inbound=False):
        source, target = (self.aliases, self.card_ids) if inbound else (self.card_ids, self.aliases)
        mapping = dict(zip(source, target, strict=True))

        def reference(item):
            if not isinstance(item, str) or item not in mapping:
                raise ContractError(
                    "unknown_wire_material_reference" if inbound else "unknown_persisted_material_reference"
                )
            return mapping[item]

        def walk(item):
            if isinstance(item, list):
                return [walk(child) for child in item]
            if not isinstance(item, dict):
                return deepcopy(item)
            result = {}
            for key, child in item.items():
                if key in REFERENCE_LISTS:
                    if not isinstance(child, list):
                        raise ContractError("invalid_material_reference_list")
                    result[key] = [reference(ref) for ref in child]
                elif key in ("card_id", "material_id"):
                    result[key] = reference(child)
                elif key in ("previous_material", "next_material"):
                    result[key] = reference(child) if child is not None else None
                else:
                    result[key] = walk(child)
            return result

        return walk(value)


def preview_references(materials, projection_version):
    if (
        creative_materials(projection_version)
        and getattr(materials, "packet", None) is not None
        and materials.packet.contract in ("editorial-input/2", "research-editorial-input/1")
    ):
        return ModelReferences.create(materials.packet)
    return None


def run_references(materials, manifest):
    from .research_materials.pinning import validate

    validate(materials, manifest)
    expected = preview_references(materials, manifest_version(manifest))
    if expected is None:
        if CATALOG_KEY in manifest:
            raise ContractError("unexpected_model_material_catalog")
        return None
    actual = ModelReferences.restore(manifest.get(CATALOG_KEY))
    if actual != expected:
        raise ContractError("model_material_catalog_binding_mismatch")
    return actual


def context_references(context):
    return ModelReferences.restore(context[CATALOG_KEY]) if CATALOG_KEY in context else None


def bind_schema(schema, references):
    if references is None:
        return schema
    result = deepcopy(schema)

    def walk(node):
        if isinstance(node, dict):
            for key, field in node.get("properties", {}).items():
                if key in ("material_refs", "support_labels"):
                    field["items"]["enum"] = list(references.aliases)
            for child in node.values():
                walk(child)
        elif isinstance(node, list):
            for child in node:
                walk(child)

    walk(result)
    return result
