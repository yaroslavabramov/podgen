"""Published technical recovery policy; independent of gateway and journal imports."""

KIND = "technical_retry"
VERSION = "technical-recovery/1"
MAX_STAGE_REPLACEMENTS = 2
STATES = ("submitting", "waiting", "outcome_unknown", "failed")
