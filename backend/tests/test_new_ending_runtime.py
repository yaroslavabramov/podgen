"""A full native rewrite can write a missing ending, including new turns in the declared last range."""

import json
from copy import deepcopy

from podgen import writing_cli
from podgen.domain.canonical import digest
from podgen.writing.behavior import resolve_behavior
from tests.revision_cases import composition, scope
from tests.revision_providers import reply
from tests.test_editorial_independence import invoke, payload, prepare
from tests.test_independent_readings import ReadingProvider
from tests.test_own_plan import own_dataset as own_dataset


def spoken(text):
    return dict(speaker="РАССКАЗЧИК", text=text)


# A synthetic miniature for checking the edit contract. No historical episode is edited.
SOURCE = [
    spoken("В Хэмпстеде жила женщина, которой привозили воду с Брод-стрит."),
    spoken("Сноу интересовался, откуда люди брали воду."),
    spoken("На этом сведения заканчиваются."),
]
NEW_ENDING = [
    spoken("По адресу её дома Сноу мог бы пройти мимо."),
    spoken("За водой всё равно пришлось бы вернуться на Брод-стрит."),
]


class EndingProvider(ReadingProvider):
    def submit(self, body, *, client_request_id):
        stage = body["metadata"]["podgen_stage"]
        if stage == "revise-1":
            self.requests.append(deepcopy(body))
            operation = payload(body)["revision_operation"]
            assert operation["mode"] == "sequence"
            (selected,) = operation["sequences"]
            assert (selected["first_utterance_index"], selected["last_utterance_index"]) == (2, 2)
            schema = body["text"]["format"]["schema"]
            assert schema["$defs"]["SequenceEdit"]["properties"]["range_id"]["enum"] == [selected["range_id"]]
            return reply(
                body,
                dict(
                    sequences=[dict(range_id=selected["range_id"], utterances=NEW_ENDING)],
                    edits=[],
                    composition=composition(),
                ),
            )
        response = super().submit(body, client_request_id=client_request_id)
        if stage != "reader-review":
            return response
        review = json.loads(json.loads(response.body)["output"][0]["content"][0]["text"])
        anchor = dict(utterance_index=2, quote=SOURCE[2]["text"])
        reason = "The evidence for a finish is already in the delivery story; the final note simply stops."
        review.update(
            recommendation="revise",
            revision_scope=scope(
                "sequence",
                anchors=[anchor],
                sequence_ranges=[dict(anchors=[anchor, anchor], reason=reason)],
            ),
            findings=[
                dict(
                    dimension="transition",
                    priority="important",
                    anchors=[anchor],
                    observation=reason,
                    requested_change="Write the final beat using what the delivery reveals.",
                    preserve=[],
                ),
            ],
        )
        return reply(body, review)


def test_compiled_final_range_materializes_new_words_and_both_readers_receive_the_result(
    own_dataset, tmp_path, monkeypatch
):
    prefix, options, _, _, _ = prepare(own_dataset, "snow-pump", tmp_path, monkeypatch)
    options.pop("review_mode")
    source, ref = tmp_path / "miniature.json", tmp_path / "imported.json"
    source.write_text(json.dumps(dict(utterances=SOURCE), ensure_ascii=False))
    assert invoke(prefix, "script-import", file=source, out=ref) == 0
    options["initial_script_ref"] = "@" + str(ref)
    provider = EndingProvider()
    monkeypatch.setattr(writing_cli, "OpenAIProvider", lambda *_, **kwargs: provider)
    assert invoke(prefix, "run", **options) == 0
    stages = {body["metadata"]["podgen_stage"]: body for body in provider.requests}
    selected = resolve_behavior(options["behavior_profile"])
    assert stages["revise-1"]["instructions"] == selected.instructions("dual-revise", "solo").body
    record = json.loads((tmp_path / "export/revise-1.json").read_text())
    expected = dict(utterances=SOURCE[:2] + NEW_ENDING)
    assert record["output"] == expected and digest(dict(utterances=SOURCE)) == record["script_hash"]
    assert record["revision"]["application"]["unchanged_outside_targets"]
    assert all(row["text"] not in {old["text"] for old in SOURCE} for row in NEW_ENDING)
    for stage in ("reader-review-1", "fact-review-1"):
        received = payload(stages[stage])["script"]["utterances"]
        assert [dict(speaker=row["speaker"], text=row["text"]) for row in received] == expected["utterances"]
    assert json.loads((tmp_path / "export/final.json").read_text())["output"] == expected
    assert len(stages) == 5
