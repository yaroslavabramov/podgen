"""Only documented knobs; ignored or misspelled controls are rejected before billing."""

from podgen.domain.errors import ContractError


def validate(values):
    numbers = dict(
        seed=(0, 4294967295),
        max_output_tokens=(1, None),
        temperature=(0, 1),
        top_p=(0, 1),
        chunk_length=(100, 300),
        speakingRate=(0.5, 1.5),
        speed=(0.5, 2),
        vol=(0.000001, 10),
        pitch=(-12, 12),
    )
    integers = {"seed", "max_output_tokens", "chunk_length", "pitch"}
    enums = dict(
        apply_text_normalization={"auto", "on", "off"},
        deliveryMode={"STABLE", "BALANCED", "CREATIVE"},
        timestampType={"WORD", "CHARACTER", "TIMESTAMP_TYPE_UNSPECIFIED"},
        latency={"normal", "balanced", "low"},
        emotion={"happy", "sad", "angry", "fearful", "disgusted", "surprised", "calm"},
    )
    for key, value in values.items():
        if key in numbers:
            low, high = numbers[key]
            if (
                type(value) not in (int, float)
                or (key in integers and type(value) is not int)
                or value < low
                or (high is not None and value > high)
            ):
                raise ContractError("invalid_synthesis_option_value", key)
        elif key in enums:
            if not isinstance(value, str) or value not in enums[key]:
                raise ContractError("invalid_synthesis_option_value", key)
        elif key == "normalize":
            if type(value) is not bool:
                raise ContractError("invalid_synthesis_option_value", key)
        elif key == "pronunciation_dict":
            if (
                not isinstance(value, dict)
                or set(value) != {"tone"}
                or not isinstance(value["tone"], list)
                or not all(isinstance(x, str) and x for x in value["tone"])
            ):
                raise ContractError("invalid_synthesis_option_value", key)
