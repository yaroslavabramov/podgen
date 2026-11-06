"""Bounded semantic rounds; retries remain physical attempts within one versioned reading."""

VERSION = "independent-readings/1"
BASES = ("reader-review", "fact-review")


def round_stage(base, index):
    return base if index == 0 else f"{base}-{index}"


def base_stage(stage):
    for base in (*BASES, "revise"):
        if stage == base or stage in (base + "-1", base + "-2"):
            return base
    return stage


def reading_stages(max_revisions):
    stages = ["draft"]
    for index in range(max_revisions + 1):
        stages.extend(round_stage(base, index) for base in BASES)
        if index < max_revisions:
            stages.append(round_stage("revise", index + 1))
    return tuple(stages)


ALL_READING_STAGES = reading_stages(2)[1:]


def independent(command):
    return command.review_mode == VERSION


def is_reading(stage):
    return stage == "review" or base_stage(stage) in BASES
