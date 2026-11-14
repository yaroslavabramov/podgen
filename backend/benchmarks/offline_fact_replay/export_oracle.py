"""Compare complete exports, allowing only explicitly paired technical identities."""

import hashlib
import json
import tarfile
from pathlib import Path
from uuid import UUID, uuid5

from podgen.domain.canonical import digest


class ExportOracle:
    def __init__(self, archive):
        with tarfile.open(archive) as trace:
            self.saved = {
                member.name.removeprefix("export/"): trace.extractfile(member).read()
                for member in trace.getmembers()
                if member.isfile()
            }

    def verify(self, directory):
        current = {
            str(path.relative_to(directory)): path.read_bytes()
            for path in Path(directory).rglob("*")
            if path.is_file()
        }
        assert current.keys() == self.saved.keys(), (
            "export_inventory_changed",
            current.keys() - self.saved.keys(),
            self.saved.keys() - current.keys(),
        )
        old_manifest, new_manifest = (json.loads(files["manifest.json"]) for files in (self.saved, current))
        old_run, new_run = (UUID(value["command"]["run_id"]) for value in (old_manifest, new_manifest))
        substitutions = {}

        def pair(old, new):
            if old != new:
                if new in substitutions:
                    assert substitutions[new] == old, ("inconsistent_identity", new)
                substitutions[new] = old

        pair(str(old_run), str(new_run))
        pair(digest(old_manifest), digest(new_manifest))
        for name in ("manifest", "invocation:fact-review"):
            pair(str(uuid5(old_run, name)), str(uuid5(new_run, name)))
        old_root, new_root = (uuid5(value, "invocation:fact-review") for value in (old_run, new_run))
        pair(str(uuid5(old_root, "request")), str(uuid5(new_root, "request")))
        pair(str(uuid5(old_root, "output")), str(uuid5(new_root, "output")))
        for index in range(43):
            old_turn, new_turn = (uuid5(value, f"source-turn:{index}") for value in (old_root, new_root))
            pair(str(old_turn), str(new_turn))
            pair(str(uuid5(old_turn, "request")), str(uuid5(new_turn, "request")))
        for field in ("artifact_id", "revision_id"):
            pair(
                old_manifest["prompt_refs"]["fact-review"][field],
                new_manifest["prompt_refs"]["fact-review"][field],
            )
        events_name = "archive/fact-review/events.json"
        old_events, new_events = (json.loads(files[events_name]) for files in (self.saved, current))
        assert len(old_events) == len(new_events) == 399
        for old, new in zip(old_events, new_events, strict=True):
            assert old["event_kind"] == new["event_kind"]
            pair(old["record_ref"]["record_id"], new["record_ref"]["record_id"])
        # Conversation hashes are derived from already verified physical requests.
        for name in current:
            if name.endswith("request.json") and name.startswith("archive/fact-review/"):
                old, new = (json.loads(files[name]) for files in (self.saved, current))
                pair(digest(old), digest(new))

        blob_substitutions = {}

        def normalize(value, *, name, path=()):
            if isinstance(value, str):
                return substitutions.get(value, value)
            if isinstance(value, list):
                return [normalize(item, name=name, path=(*path, index)) for index, item in enumerate(value)]
            if not isinstance(value, dict):
                return value
            if digest(value) in blob_substitutions:
                value = blob_substitutions[digest(value)]
            if value.get("journal_version") == "execution-journal/2":
                assert value.get("contract") in (
                    "model-request-v1",
                    "model-invocation-event-v1",
                    "writing-evaluation-output-v1",
                    "independent-reading-output/1",
                )
                value = {key: item for key, item in value.items() if key != "journal_version"}
                if value.get("event_kind") in ("source_tool_call", "source_tool_result"):
                    data = value["data"]
                    assert (
                        data["arguments_hash"] == hashlib.sha256(data["raw_arguments"].encode()).hexdigest()
                    )
                    parent = next(
                        row for row in new_events if row["record_ref"] == data["provider_response_ref"]
                    )
                    assert parent["event_kind"] == "provider_response"
                    assert parent["data"]["source_turn_index"] == data["turn_index"]
                    value = dict(
                        value,
                        data={
                            key: item
                            for key, item in data.items()
                            if key not in ("provider_response_ref", "arguments_hash")
                        },
                    )
            result = {}
            for key, item in value.items():
                # Wall-clock creation time and HTTP request IDs belong to this
                # offline execution. No model output, usage or arbitrary field is dropped.
                if name == "manifest.json" and not path and key == "created_at":
                    result[key] = old_manifest["created_at"]
                elif key == "provider_request_id" and value.get("contract") == "model-invocation-event-v1":
                    result[key] = None
                else:
                    result[key] = normalize(item, name=name, path=(*path, key))
            return result

        for index, (old, new) in enumerate(zip(old_events, new_events, strict=True)):
            for field in ("response_blob", "wire_request_blob", "wire_response_blob"):
                if field not in old:
                    assert field not in new
                    continue
                name = f"archive/fact-review/{index:03d}-{old['event_kind']}-{field}.json"
                left = normalize(json.loads(self.saved[name]), name=name)
                right = normalize(json.loads(current[name]), name=name)
                assert left == right, ("export_blob_content_changed", name)
                blob_substitutions[digest(new[field])] = old[field]

        different, byte_exact, json_equal = [], 0, 0
        for name, value in current.items():
            expected = self.saved[name]
            if value == expected:
                byte_exact += 1
                continue
            if name.endswith(".json"):
                before, after = (json.loads(blob) for blob in (expected, value))
                left, right = (normalize(document, name=name) for document in (before, after))
                # Files without an actual allowed value substitution must retain
                # bytes too; incidental JSON formatting changes are not excused.
                if left == right and (left != before or right != after):
                    json_equal += 1
                    continue
            different.append(name)
        assert not different, ("export_changed", different[:20], len(different))
        return dict(
            archive_contract="execution-journal/2",
            verified_technical_delta=[
                "journal_version",
                "exact tool provider_response_ref and arguments_hash",
            ],
            files_verified=len(current),
            byte_exact_files=byte_exact,
            json_equal_after_technical_substitutions=json_equal,
            substitutions=substitutions,
            verified_blob_ref_substitutions=len(blob_substitutions),
            creation_time_substitution="manifest.json:created_at",
            transport_substitution="provider_request_id:offline HTTP transport",
            all_other_json_values_exact=True,
        )
