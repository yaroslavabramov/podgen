"""Pure role-output validation; no inference, persistence or semantic approval."""

import json

from pydantic import ValidationError

from podgen.writing.roles import EditorialFeedback, WriteOutput


class RoleOutputError(ValueError):
    def __init__(self, details):
        self.details = details
        super().__init__("role_output_validation_failed")


def load_unique_json(raw):
    """Reject duplicate keys before schema validation can silently replace meaning."""

    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate_output_json_key")
            result[key] = value
        return result

    return json.loads(raw, object_pairs_hook=unique)


def validate_output(stage, raw, *, speaker_labels, draft=None):
    try:
        load_unique_json(raw)
        parsed = (EditorialFeedback if stage == "review" else WriteOutput).model_validate_json(raw)
        if stage == "review":
            parsed.validate_against(WriteOutput.model_validate(draft))
            if parsed.reading_mode != "informed":
                raise ValueError("review_access_mode_mismatch")
        elif {utterance.speaker for utterance in parsed.utterances} != set(speaker_labels):
            raise ValueError("speaker_labels_mismatch")
        return parsed.model_dump(mode="json")
    except ValidationError as error:
        raise RoleOutputError(
            [
                dict(path=list(e["loc"]), type=e["type"], message=e["msg"])
                for e in error.errors(include_input=False)
            ]
        ) from None
    except ValueError as error:
        known = {
            "duplicate_output_json_key",
            "feedback_utterance_index_out_of_range",
            "feedback_quote_mismatch",
            "review_access_mode_mismatch",
            "speaker_labels_mismatch",
        }
        raise RoleOutputError(
            [dict(code=str(error) if str(error) in known else "output_validation_failed")]
        ) from None
