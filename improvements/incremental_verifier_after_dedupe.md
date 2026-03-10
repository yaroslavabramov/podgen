# Incremental Verifier After Dedupe

## Проблема

После `research_fast_web -> build_claims -> dedupe_claims` текущий пайплайн всё ещё делает
полный `verify_claims` по всему `state["claim_ids"]`.

Это означает:

- лишние LLM-вызовы на каждом verify-loop
- повторную верификацию claims, у которых не изменилось вообще ничего
- лишние деньги на прогон

Сейчас это видно и по коду:

- targeted rebuild уже есть в [claim_builder.py](/Users/yaroslavabramov/side/podgen/backend/app/pipeline/phase4_claims/claim_builder.py)
- но verifier всё ещё итерируется по всему `claim_ids` в [verifier.py](/Users/yaroslavabramov/side/podgen/backend/app/pipeline/phase4_claims/verifier.py)

## Почему нельзя просто верифицировать только новые claims

Простой вариант:

- `build_claims` создал новые claims
- verifier проверяет только их

не является корректным.

Причина: между `build_claims` и `verify_claims` стоит глобальный [dedupe_claims](/Users/yaroslavabramov/side/podgen/backend/app/pipeline/phase4_claims/dedupe.py).

`dedupe_claims` может:

- удалить duplicate claim
- оставить старый keep-claim
- перелить в keep-claim новые `ClaimEvidence`
- тем самым реально изменить support старого claim

То есть claim может быть "старым" по `claim_id`, но "новым" по смыслу verifier payload.

Поэтому граница incremental verification должна проходить **после dedupe**, а не после build.

## Правильная модель

Нужно верифицировать только те claims, у которых после `dedupe_claims` изменилось что-то, влияющее на verifier payload.

### Untouched claims

Claim можно не переверифицировать, если не изменилось ничего из этого:

- `statement`
- `claim_type`
- набор `ClaimEvidence`
- canonical source set
- provenance summary, которая попадает в verifier payload

Если это всё не изменилось, старый `verification_level` можно сохранить.

### Touched claims

Claim надо переверифицировать, если:

- это новый claim
- это keep-claim, в который влились новые evidence при dedupe
- это claim, чьи provenance/source summary изменились
- это claim, который уже был в `research_gaps` и попал в rebuild scope

## Что нужно поменять

### 1. Build Claims должен возвращать rebuild metadata

После targeted rebuild `build_claims` должен возвращать не только общий `claim_ids`, но и:

- `rebuilt_claim_ids`
- `preserved_claim_ids`
- `targeted_branch_ids`

Это нужно не для самого verifier, а чтобы downstream знал, какой кусок claim-space реально пересобирался.

Важно:

- это coordination metadata, а не долгоживущая часть pipeline state
- эти поля должны использоваться один раз в `dedupe -> verify`
- после завершения verifier их нужно очищать из state, чтобы стухшие `rebuilt_claim_ids` / `targeted_branch_ids` не протекали в следующий loop-pass

### 2. Dedupe Claims должен возвращать touched scope

`dedupe_claims` должен дополнительно возвращать:

- `dedupe_deleted_claim_ids`
- `dedupe_merged_into_claim_ids`
- `dedupe_untouched_claim_ids`

Ключевая вещь: `dedupe_merged_into_claim_ids` — это keep-claims, в которые влились новые links, и именно их нельзя считать preserved.

Как и build-метаданные выше, эти поля должны быть ephemeral:

- использоваться только ближайшим `verify_claims`
- очищаться после consumption

### 3. Verifier должен работать по explicit verify scope

В loop-режиме verifier не должен брать весь `state["claim_ids"]`.

Он должен брать:

- `rebuilt_claim_ids`
- `dedupe_merged_into_claim_ids`
- при необходимости claims, которые уже были в `research_gaps`, если они остались в итоговом `claim_ids`

Все остальные claims должны:

- сохранять прежний `verification_level`
- не попадать в LLM batches повторно

### 4. Нужен fingerprint verifier payload

Для надёжности нужен `verification_fingerprint`.

Его можно считать как hash от:

- `statement`
- `claim_type`
- sorted `(evidence_id, supports_fact, contradicts_fact, illustrative)`
- sorted canonical source ids
- sorted provenance pair summary

Если fingerprint у claim не изменился, verifier можно безопасно пропустить даже без сложной ручной логики.

### 5. Future extension: persistent fingerprint for retry/resume

Это не обязательно для первой реализации incremental verifier.

Но если позже pipeline будет поддерживать `retry/resume` прогона, полезно хранить `verification_fingerprint` на самом `Claim` в БД.

Тогда:

- первый full verify-pass не придётся заново прогонять после resume
- unchanged claims можно будет пропускать не только внутри loop-итерации, но и между запусками

Это `P2`, но его стоит держать в виду, чтобы не зацементировать purely in-memory design.

## Предпочтительный runtime flow

### Первый проход

- `build_claims` -> полный набор claims
- `dedupe_claims` -> итоговый набор claims
- `verify_claims` -> полный verify

### Verify-loop

- `research_fast_web`
- `build_claims` -> только targeted branches
- `dedupe_claims` -> merge new + old claims
- `verify_claims` -> только changed claims after dedupe

## Почему это важно

Это даёт сразу три эффекта:

- меньше LLM cost
- меньше latency verify-loop
- меньше шансов, что verifier на повторных проходах создаёт шум там, где ничего не изменилось

Именно это и нужно для дорогих iterative runs: если меняется 10-15% claim-space, нельзя платить за 100% verifier ещё раз.

## Приоритет

Это `P0/P1` improvement:

- не меняет продуктовую логику
- напрямую уменьшает стоимость loop-итераций
- хорошо сочетается с уже сделанным targeted rebuild в claim builder

Persistent fingerprint on `Claim` для retry/resume — это отдельный `P2`.
