# Evaluating Podgen

Use these materials to test a particular decision in the production workflow. A first reader sees the spoken text without the dossier. A fact-checker traces a spoken claim to its source. A listener compares recordings of the same passage without seeing the voice or provider name. Each exercise keeps the operator's key separate from the input being evaluated.

| To investigate | Start here |
| --- | --- |
| Can readers follow a dialogue about GPS or a solo narrative about John Snow? | [Frozen development corpus and export instructions](cases/development/README.md) |
| Can a planner select a route through a dossier? | [GPS shared and fixed inputs](editorial-inputs/current/gps-clock/README.md) and [Snow own-plan input](editorial-inputs/current/snow-pump/README.md) |
| Can an editor distinguish a material error from a harmless storytelling choice? | [Paired plan controls](operator/editorial-judgment/README.md) and [blind script readings](operator/script-listening/README.md) |
| Does a narrator preserve words, emphasis and cadence? | [Audio fixtures](audio/README.md) and [listening protocol](audio/listening/protocol.md) |
| Does an improvement transfer to a different episode? | [Apollo 13](editorial-inputs/experiments/apollo13-air/README.md), [rain and soil](editorial-inputs/experiments/rain-soil/README.md), [Wright brothers](editorial-inputs/experiments/wright-first-flight/input-corrected/shared.json) |
| Can a change handle a longer script on the same evidence? | [Thirty-minute Snow comparison](editorial-inputs/experiments/snow-30min/README.md) |

Run reports retain selected conclusions. Write raw provider responses, recordings and full exports to a private run directory under `tmp/`. Reproducible comparisons record the exact script, source versions, model profile, request, stage verdict and usage. A provider-free preview verifies the input a model would see. A verdict or a synthetic duration estimate cannot establish what a human listener heard.

The English guides describe how to use the material. Russian scripts, role inputs and evaluation keys retain the language and bytes used in the experiment. Third-party documents have their own provenance and notices. See the [corpus source guide](cases/development/README.md#source-provenance-and-rights) and the [rain study attribution](editorial-inputs/experiments/rain-soil/README.md).
