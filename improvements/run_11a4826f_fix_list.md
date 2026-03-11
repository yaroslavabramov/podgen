# Run 11a4826f Fix List

Основано на прогоне `run-11a4826f-9688-4731-84cd-01bcadf92447`.

Назначение этого документа:

- зафиксировать, что именно показал конкретный run
- отделить это от универсальных исправлений пайплайна

Ниже в каждом пункте есть две части:

- `Evidence from this run`
- `Systemic fix`

## P0

### 1. Hunter extraction failure должен иметь retry path

**Evidence from this run**

- В Hunter ветки `b3` и `b5` загрузили по `8` selected chunks, но сохранили `0` evidence.
- После этого pipeline был вынужден переходить к `fast-web`, который дороже и менее предсказуем как fallback.

**Systemic fix**

- Если Hunter selected нормальный пакет чанков, но extraction вернул `0` items, нужен retry вместо немедленного `continue`.
- Retry должен зависеть от типа failure:
  - `extraction_failed` / exception path: меньший payload, меньше чанков
  - `invalid_extractor_payload`: более жёсткая инструкция на формат + узкий payload

### 2. Fast-web retention должен учитывать trust не только в reranking, но и в post-fetch filter

**Evidence from this run**

- В verifier fast-web по `b1.2` были отброшены сильные на вид `official/legal/academic` кандидаты.
- При этом был принят слабый unknown page из dealer/collector family.

**Systemic fix**

- High-trust страницы не должны так же легко падать в `_is_low_value_page`, как unknown pages.
- Нужен authority-aware bypass или softer threshold для `primary` / `secondary`.
- В debug dump для page drops нужно писать explicit reason и overlap stats.
- Для weak source families нужен явный penalty.

### 3. Verifier gaps не должны дублировать один и тот же corroboration target

**Evidence from this run**

- Во втором verifier-pass были gap groups, которые пересекались по одному и тому же `claim_id`.
- Из-за этого один corroboration target мог занимать больше одного fast-web slot’а.

**Systemic fix**

- При построении `research_gaps` нужно схлопывать overlapping gap groups по union всех `claim_ids`, а не по одному lead claim.
- Aggregated gap должен мержить:
  - `claim_ids`
  - `claim_contexts`
  - `found_assets`
  - `missing_assets`
  - coverage summary по question contexts

### 4. Fast-web budget должен быть adaptive и intent-aware

**Evidence from this run**

- Во втором fast-web loop было больше verifier gaps, чем реально обработанных gap slots.
- Interpretive branches остались без попытки corroboration, потому что budget кончился раньше.

**Systemic fix**

- Budget не должен быть только fixed constant.
- Он должен учитывать:
  - `remaining_gap_count`
  - `intent`
- `debate` и `compare` должны получать повышенный приоритет в verifier-loop.
- Budget должен оставаться upper-bounded, чтобы не раздувать loop без контроля.

## P1

### 5. Claim-corroboration должен сильнее предпочитать качественные источники

**Evidence from this run**

- Во втором fast-web pass рядом с сильными источниками принимались и слабые secondary/unknown sources.
- Это дало полезный delta, но quality accepted evidence получилась очень неровной.

**Systemic fix**

- Для `claim_corroboration` нужен stronger source-quality ceiling.
- Unknown/weak pages не должны переживать более сильные direct candidates без явной причины.
- Приоритет по quality должен жить:
  - в reranking
  - в extractor prompt/payload

### 6. Expected-evidence coverage нужно реально track’ать

**Evidence from this run**

- По hunter dumps `expected_evidence` практически не отражается как найденный coverage signal.
- При этом сами `expected_evidence` в research tree присутствуют.

**Systemic fix**

- Это не logging issue, а отсутствие explicit tracking.
- Нужно добавить expected-evidence coverage в extraction loop и в hunter debug/summary.
- После этого уже улучшать prompt/retrieval под non-core assets:
  - `ANECDOTE`
  - `CONTRAST`
  - `DEFINITION`
  - и т.д.

### 7. Showrunner должен сильнее сжимать claim pool

**Evidence from this run**

- Showrunner построил хорошую act structure.
- Но отдельные акты получили слишком большой пул claims и стали ближе к grouping layer, чем к editorial selection.

**Systemic fix**

- Нужен explicit density cap на act claim pool.
- Quotas должны ограничивать не только типы контента, но и максимальную плотность claims.
- Curated layer должен работать как editor, а не как складирование почти всего подходящего.

### 8. Excerpt selection должен уметь отбрасывать bibliography/reference windows

**Evidence from this run**

- В fast-web был принят excerpt, который по сути оказался bibliography/reference fragment, а не содержательным evidence.

**Systemic fix**

- Для excerpt selection нужен penalty / filter для reference-like windows.
- Типичные сигналы:
  - citation-heavy lists
  - `doi`
  - `vol.`
  - `pp.`
  - много фамилий и годов
  - citation regex patterns

### 9. Numeric conflict detection нужен до showrunner

**Evidence from this run**

- В claim pool жили несовместимые числовые формулировки, но conflict layer их не отметил.
- Из-за этого verifier-loop мог строить `confirm` objective там, где по смыслу нужен `resolve`.

**Systemic fix**

- Нужен lightweight numeric contradiction detection pass.
- Минимальный охват:
  - exchange rates
  - dates
  - counts
  - ranges
- Лучшее место: отдельный pass перед showrunner.
- Текущий conflict layer такие случаи не видит.

### 10. Claim-to-branch assignment должен быть semantic, а не только lineage-based

**Evidence from this run**

- В verifier-loop вопрос одной ветки мог получить gap по claim’у, который по смыслу относится к другой ветке.
- Это означает неверный corroboration target и потерю budget на не тот вопрос.

**Systemic fix**

- Нужно пересмотреть, как claim builder и provenance summary выбирают `builder_branch_id` / question linkage.
- Branch/question assignment должен больше зависеть от semantic fit claim’а, а не только от исходного evidence scope.

### 11. Verifier fingerprint skip должен реально экономить LLM calls

**Evidence from this run**

- Во втором verifier-pass `skipped_unchanged_claim_count = 0`.
- При этом rebuild scope был большой, а verifier всё равно заново гнал почти всё внутри него.

**Systemic fix**

- Проблема не только observational, а structural:
  - targeted rebuild удаляет старые claims
  - создаёт новые UUID
  - новые claims не имеют прежнего `verification_fingerprint`
- Предпочтительный fix:
  - по возможности обновлять rebuilt claims, а не удалять и создавать новые
- Fallback:
  - переносить previous fingerprint
  - или матчить rebuilt claims с pre-rebuild equivalent по statement+evidence signature

## P2

### 12. Multi-source diversity на уровне claims нужно улучшать явно

**Evidence from this run**

- В финальном planning input почти все claims имели `source_diversity = 1`.

**Systemic fix**

- Это не просто метрика для наблюдения, а признак текущего ограничения builder layer.
- Если нужен более robust editorial backbone, понадобится либо:
  - post-build merge/corroboration pass
  - либо изменение prompt/payload claim builder’а под multi-source claims

## Короткий итог

Это уже в основном не список “подкрутить именно HKD run”, а список системных улучшений, которые просто были хорошо подсвечены этим run.

Главные приоритеты:

1. Hunter retry на extraction failure
2. Trust-aware fast-web retention
3. Gap dedupe по overlapping claim targets
4. Adaptive/intention-aware fast-web budget
5. Stronger claim-corroboration source quality policy
6. Expected-evidence tracking
7. Bibliography window filter
8. Numeric conflict detection
9. Better showrunner compression
10. Real fingerprint-based verifier savings
