# Fact-review startup: read-only code review

No live measurements, tests, edits, signals or provider calls were made. Reviewed the existing native sample `/tmp/podgen-fact-start-sample-20261113.txt` and current code at `94ba1332`. Root subsequently observed the role advancing through two physical turns.

## Evidence and limits

At the supplied observation the fact-review root invocation was already prepared but had no events. Therefore request compilation and journal.prepare had finished, while no first source_context_count receipt had yet been persisted. This does not identify one exact Python function: macOS native sample resolves Python interpreter/psycopg/libpq frames, not Python source frames. In that sample the main thread was doing local Python/PostgreSQL work, and the ONNX worker was waiting. It does not establish a multi-minute provider request or active embedding inference.

## Startup work before the first count receipt

1. `runtime/evaluation/reading_stage.py:152` freshly reads/authorizes the prepared request; prior_attempt_usage then checks attempts, and gateway.invoke loads/authorizes the run manifest. The request binds the existing script and manifest/provenance, so a new role still has nontrivial authorization ancestry.
2. `runtime/models/research_tool_gateway.py:33-34` calls journal.request twice for a normal first attempt: root and ancestors[0] are the same invocation. Each `EvaluationJournal.request` (`journal.py:221`) separately reads the request record and its blob through public authorized operations. This is a constant repeated graph cost, not a scan growing with this role's turns.
3. The new ResearchHistory restore has no prior fact-review events to scan. The patched growing count/plan/submitted-turn lookup therefore cannot explain this initial delay.
4. `research_tool_gateway.py:48` restores run-scoped material admissions, passing the full service guard. `ResearchMaterialResolver.restore` (`research/materials/resolver.py:41-45`) calls guard once per admission before loading it.
5. That guard (`runtime/evaluation/service.py:346-348`) reconstructs `_materials(command)`, checks the complete frozen material hash, and restores the run's admissions again using a no-op inner guard (`service.py:66-78`). `research/materials/loading.py:93-102` individually authorizes each bound representation; `loading.py:140-151` authorizes readable source representations again. These public operations have separate fresh snapshots.
6. The loop guard and the initial count's guard invoke the same complete material reconstruction again before the first count receipt is written.

## Conclusion

There is concrete repeated full material/authorization work on the first-role path even with zero history events. The admission restore also nests a whole-material/whole-admission guard inside its admission loop: for N frozen sources and A runtime admissions, this portion can grow approximately as A * (N + A) authorized loading work. For a fixed admitted corpus it is entry overhead, independent of the new role's turn count; it is not an intrinsic universal constant as the corpus changes.

This explains a credible local startup cost and is consistent with the supplied sample. Exact attribution of the observed three minutes between repeated request authorization, admission restore and guards remains unmeasured. Do not present the specific function or the A*(N+A) component as a measured cause. A later isolated count/profile can attribute it without changing ACL semantics. No patch proposed or applied during live.
