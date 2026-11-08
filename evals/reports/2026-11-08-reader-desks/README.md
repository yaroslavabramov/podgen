# One cold reader, two editorial desks

Diagnostic only. No production prompt, runtime, role registry or persisted historical run is changed.

`language_review` is a compact, separately generated list of addressed form/wording repairs. `reader_review` retains the existing `listener-experience/4` shape and field order. The generic front instruction assigns copyediting and connected spoken-prose editing before the listener assessment. It contains no topic, known error, expected result or example from the cases. Both sections receive the same complete cold payload; neither is an independent second reader.

Before generation, `frozen.json` records exact requests, scripts, criteria and instructions. Cases 01/04/05 are the initial three independent Terra medium calls. The inherited `criteria.json` remains outside all requests. Only if the initial calls catch both known defects in 01 without requiring the corresponding repairs in corrected 04 or damaging the working 05 control may 02/03 be called. `initial-assessment.json` must record that decision and evidence first. Required findings, exact anchors and proposed operations are assessed, not just recommendation labels or counts. No editorial repair or repeat generation is authorized by this experiment.

```sh
PYTHONPATH=backend/src backend/.venv/bin/python evals/reports/2026-11-08-reader-desks/run.py prepare
PYTHONPATH=backend/src backend/.venv/bin/python -m pytest -q evals/reports/2026-11-08-reader-desks/test_desks.py
# Paid: exactly the three initial cases, each protected by an exclusive durable claim.
PYTHONPATH=backend/src backend/.venv/bin/python evals/reports/2026-11-08-reader-desks/run.py initial
# Offline: validates full saved outputs, addresses, request/response hashes and costs.
PYTHONPATH=backend/src backend/.venv/bin/python evals/reports/2026-11-08-reader-desks/run.py replay
```

The original whole cold payload is unchanged, including duration context. Existing OpenAI transport and full-text output validators are reused. Structured schema/reference nodes and full script identity are checked offline. Native visible-byte and input-token limits apply; maximum output is 8192 tokens. All raw responses and usage are archived. A missing response after submission requires reconciliation and cannot cause automatic resubmission. Cost estimates use the project’s separately archived published Terra rates; no new paid model probe is made.
