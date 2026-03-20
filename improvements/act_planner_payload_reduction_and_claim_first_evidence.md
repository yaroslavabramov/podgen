# Act Planner Payload Reduction And Claim-First Evidence

Основано на planning run `run-fccc9c33-fd78-4da7-90ac-bf40c0177936`, особенно на:

- [showrunner](/Users/yaroslavabramov/side/podgen/tmp/debug/planning_showrunner/20260320T094209154380Z_run-fccc9c33-fd78-4da7-90ac-bf40c0177936_ok_689f2d6979.json)
- [act 4](/Users/yaroslavabramov/side/podgen/tmp/debug/planning_acts/20260320T095322626271Z_run-fccc9c33-fd78-4da7-90ac-bf40c0177936_act-04_ok_c14be9b788.json)

## Problem

Текущий prompt payload для `act_planner` одновременно:

- содержит лишний noise
- и не гарантирует, что support для выбранного claim попадёт в prompt

Это плохая комбинация.

На последнем прогоне это проявилось так:

- сцена про США до 1865 года строилась на реальном claim `ae1b4061...`
- но в `input.evidence` для этого акта не было ни одного evidence, связанного с этим claim
- rescue path (`vector_search` + `evidence extractor`) попытался поднять support post-hoc, но не нашёл релевантный chunk

То есть проблема не в том, что scene beat был полностью выдуман. Проблема в том, что `act_planner` работает с плохо упакованным support context.

## Что сейчас уходит в prompt

Top-level payload:

- `act_number`
- `output_language`
- `story_role`
- `act_title`
- `purpose`
- `internal_arc`
- `opening_question`
- `ending_turn`
- `focus_branch_ids`
- `branch_focus_summary`
- `estimated_duration_seconds`
- `scene_count_guidance`
- `content_quotas`
- `conflict_brief_ids`
- `conflict_count`
- `claims`
- `evidence`

Claim-level fields:

- `id`
- `type`
- `verification`
- `statement`
- `supporting_evidence_count`
- `illustrative_evidence_count`
- `has_illustrative`
- `branch_ids`
- `question_ids`
- `intents`
- `direct_support_count`
- `contextual_support_count`
- `quality_hints`
- `source_diversity`

Evidence-level fields:

- `id`
- `asset_type`
- `quote`
- `narrative_score`
- `illustrative`
- `supports_fact`
- `contradicts_fact`
- `source_title`
- `source_reliability`
- `authority_type`
- `is_temporary`
- `branch_ids`
- `question_ids`
- `intents`
- `direct_or_contextual`
- `question_contexts`
- `supporting_claim_ids`
- `illustrative_claim_ids`

## Что здесь лишнее

### 1. `evidence.question_contexts`

Это audit/debug payload, а не planning payload.

Для beat planning это слишком тяжёлый шум:

- `branch_id`
- `question_id`
- `question`
- `intent`
- `asset_type`
- `direct_or_contextual`

Planner не должен в это вчитываться, чтобы построить сцену.

### 2. `evidence.branch_ids`, `evidence.question_ids`, `evidence.intents`

Это largely duplicated metadata.

Act planner уже получает:

- `focus_branch_ids`
- `branch_focus_summary`
- claim-level `branch_ids`
- claim-level `intents`

Та же информация на evidence-level для scene planning почти не нужна.

### 3. `evidence.direct_or_contextual`

Это полезно для verifier/adjudication, но не для beat planning.

Если evidence уже попало в act prompt, planner важнее понимать:

- что это за quote
- насколько он narrative / illustrative
- к каким claims он относится

А не provenance-mode.

### 4. `conflict_brief_ids`

Голые UUID почти бесполезны.

Если нужен conflict pressure, planner должен видеть либо:

- `conflict_count`
- либо compact conflict summaries

Но не просто массив id.

### 5. Часть claim/evidence lineage metadata как есть

В act prompt есть слишком много “технических следов” research pipeline.

Для beat planning это не только token cost, но и cognitive noise.

## Что сейчас организовано неправильно

### 1. `evidence[:20]`

Это главный structural bug.

Evidence budget режется как flat global list, а не как support bundle per claim.

Из-за этого возможно:

- claim попал в акт
- claim support есть в БД
- но support не попал в prompt

Для current planner это уже неприемлемо.

### 2. `claims` и `evidence` живут как два независимых списка

Planner сам должен:

- выбрать scene claim set
- найти в flat evidence pool подходящие `evidence_ids`

Это лишняя работа для LLM и источник ошибок.

Planner должен получать не “мешок claims + мешок evidence”, а более явный `claim -> support bundle`.

## Что нужно оставить

### Top-level

Оставить:

- `act_number`
- `output_language`
- `story_role`
- `act_title`
- `purpose`
- `internal_arc`
- `opening_question`
- `ending_turn`
- `focus_branch_ids`
- `branch_focus_summary`
- `estimated_duration_seconds`
- `scene_count_guidance`
- `content_quotas`
- `conflict_count`

Убрать:

- `conflict_brief_ids`

### Claim-level

Оставить:

- `id`
- `type`
- `statement`
- `verification`
- `has_illustrative`
- `illustrative_evidence_count`
- `direct_support_count`
- `source_diversity`
- `branch_ids`
- maybe `intents`

Убрать или сильно сжать:

- `question_ids`
- `contextual_support_count`
- `quality_hints` как вложенный dict целиком

### Evidence-level

Если evidence вообще остаётся как отдельный flat list, оставить только:

- `id`
- `quote`
- `asset_type`
- `illustrative`
- `source_title`
- maybe `authority_type`
- `supporting_claim_ids`
- `illustrative_claim_ids`

Убрать:

- `source_reliability`
- `is_temporary`
- `branch_ids`
- `question_ids`
- `intents`
- `direct_or_contextual`
- `question_contexts`
- `supports_fact`
- `contradicts_fact`

## Предпочтительный redesign

### Claim-first evidence attachment

Вместо:

- `claims`
- `evidence[:20]`

нужно перейти к чему-то вроде:

```json
{
  "claims": [
    {
      "id": "...",
      "statement": "...",
      "verification": "verified",
      "has_illustrative": true,
      "source_diversity": 2,
      "support_bundle": {
        "direct_evidence": [
          {
            "id": "...",
            "quote": "...",
            "asset_type": "mechanism",
            "source_title": "...",
            "illustrative": false
          }
        ],
        "illustrative_evidence": [
          {
            "id": "...",
            "quote": "...",
            "asset_type": "case",
            "source_title": "...",
            "illustrative": true
          }
        ]
      }
    }
  ]
}
```

### Packaging rule

Для каждого claim акта:

- гарантированно положить в prompt хотя бы `1` direct support quote
- если есть, положить `1` illustrative quote

И только потом, если остаётся budget, добавлять:

- few global act-level extras
- shared comparative/context evidence

## Fallback logic after that

Только после deterministic claim-first packaging нужен rescue path:

1. scene references claim
2. у claim нет usable support bundle in prompt
3. тогда `vector_search`
4. потом `evidence extractor`

Сейчас rescue path пытается исправить проблему, которую prompt packaging сам создал.

## Alternative: claim-evidence tool instead of a large flat evidence payload

Есть ещё один естественный вариант:

- не грузить в prompt большой flat `evidence` pool
- а дать `act_planner` tool, который по выбранным `claim_ids` возвращает их support bundle

### Почему это хорошо

`Act planner` не обязан видеть все evidence сразу.

Ему обычно нужно:

1. увидеть список candidate claims
2. выбрать claims для конкретной сцены
3. быстро получить support именно для этих claims

Это лучше, чем current model:

- положить в prompt мешок `claims`
- положить в prompt мешок `evidence[:20]`
- надеяться, что support для нужного claim случайно окажется внутри

### Какой tool нужен

Нужен не open-ended search tool, а узкий deterministic tool:

`get_claim_support(claim_ids: list[str], mode: direct|illustrative|all)`

Возвращает по каждому claim:

- `direct_evidence`
- `illustrative_evidence`
- short source metadata
- maybe short authority hint

### Чего не надо делать

Не стоит делать основным способом:

- свободный `vector_search` по scene text
- open-ended retrieval по тому, что модель “примерно хочет сказать”

Это опять превращает `act_planner` в research agent.

### Лучший pragmatic вариант

Не чистый tool-only path, а hybrid:

1. В prompt положить минимальный preloaded support bundle per claim
2. Дополнительно дать tool `get_claim_support(...)` для on-demand retrieval

Тогда:

- planner не слепой с самого начала
- но может дозапросить support, если сцена реально использует claim глубже, чем initial bundle

### Когда это особенно полезно

Именно для случаев типа последнего Act 4:

- claim в акте есть
- support в БД есть
- но initial flat evidence prompt его не включил

В такой архитектуре planner не должен идти в vector search. Он должен просто сказать:

- `я использую claim X`
- `покажи support для claim X`

## Preferred direction

Если выбирать между:

1. улучшать current flat evidence payload
2. перейти к claim-linked support retrieval

то второй путь архитектурно чище.

Даже если не делать его полностью tool-driven сразу, это хороший ориентир:

- planner должен получать evidence **по claim**
- а не из общего act-wide мешка

## Evidence budget per claim

`Claim-first bundle` не должен превратиться в новый token blow-up.

Нужен явный packaging budget:

- обязательно: `1` лучший direct evidence per claim
- опционально: `1` illustrative evidence per claim, если он есть и реально помогает сцене
- сверху: global act cap порядка `30-40` evidence, чтобы длинный act с `10-11` claims не раздувал prompt бесконтрольно

Если budget не хватает:

- direct evidence имеет приоритет над illustrative
- claims, которые реально попали в beat sheet, должны получать support первыми

## Shared evidence policy

Один evidence часто поддерживает несколько claims.

Это нужно решить явно:

### Вариант A: дублировать quote в нескольких claim bundles

Плюсы:

- проще для LLM
- меньше implicit matching работы

Минусы:

- больше токенов

### Вариант B: shared evidence pool + ссылки по `id`

Плюсы:

- меньше токенов

Минусы:

- LLM снова должен матчить claim к внешнему pool
- часть выигрыша claim-first bundling теряется

Для первого pragmatic iteration лучше выглядит:

- небольшое controlled duplication
- особенно если один и тот же quote действительно является лучшей опорой для нескольких claim’ов

## Почему это связано с однообразием актов

Payload quality здесь влияет не только на factual grounding, но и на форму сцен.

Если planner получает claim-first bundles с живыми цитатами, у него появляется материал для разных scene functions:

- vivid quote / anecdote -> grounding scene
- mechanism quote -> explanation scene
- case quote -> case_study beat
- contrast quote -> transition or comparison beat

Когда таких привязанных цитат нет, planner чаще уходит в generic explanation mode.

То есть:

- scene monotony — это не только prompt wording problem
- это ещё и symptom слабой evidence packaging

## Tool-based path: future consideration, not immediate plan

Идея `get_claim_support(claim_ids, mode=...)` остаётся интересной, но как **следующий шаг** её брать не стоит.

Причины:

- текущий `act_planner` — это одиночный structured LLM call, а не tool loop
- claim-first bundling должно закрыть большую часть проблемы без новой orchestration complexity
- hybrid `preloaded bundle + tool` имеет смысл только после того, как baseline payload packaging уже будет правильным

То есть:

- как future direction — да
- как immediate implementation plan — нет

## Что делать следующим

### P0

Переделать `_build_act_prompt_payload(...)`:

- убрать noisy evidence metadata
- убрать `conflict_brief_ids`
- перестать резать evidence как flat `[:20]`

### P1

Сделать `claim-first support bundle` для act planner:

- `1-2` лучших quote на claim
- explicit attachment в payload

### P2

После этого уже смотреть, нужен ли вообще act-level `vector_search` rescue так часто.

Вполне возможно, что после правильной packaging он станет редким fallback, а не регулярным компенсатором prompt design.

## Короткий вывод

Проблема `act_planner` сейчас не только в prompt wording.

Проблема в том, что payload:

- шумный
- flat
- и не гарантирует claim support completeness

То есть нужно не только “лучше просить planner”, но и **лучше кормить planner**.
