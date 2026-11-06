# Независимые чтения сценария

Новые `WritingRunCommand` и CLI `run` по умолчанию используют `review_mode=independent-readings/1`. Профиль `reviewer_profile_ref` выполняет оба чтения (Terra); `writer_profile_ref` пишет и правит (Sol). Это явно заданные ModelProfile, без скрытой смены модели. После каждого полного текста идут два отдельных запроса, без общей истории разговора:

- `reader-review`: только `script` с полными индексированными репликами, `audience`, `format`, `genre`, `output_language`. `audience` берётся исключительно из нового `assignment.listener_profile`; при отсутствии — нейтральный профиль без знания темы. `genre` берётся из нового одноимённого поля или равен `podcast`. Старые audience, style_guidance, план, вопрос задания, досье и прошлые отзывы скрыты. Инструкции — только отдельный `reader-review.md`, без common/format-компонентов.
- `fact-review`: тот же `script`, `output_language`, `factual_remit` (вопрос, обязательные темы, knowledge_cutoff, непустые исключения), карточки `support`, точные атрибутированные `evidence`, `necessary_limits`. Первое заключение и план не передаются. Инструкции — отдельный `fact-review.md`.

Оба вывода сохраняются полностью и проверяются по точным якорям текущего текста. Фактологические находки дополнительно содержат `support_labels` — точные метки опор для правки. `needs_material` требует конкретного anchored research_request. Объединение детерминировано: необходимые материалы, затем required finding/revise любой роли, затем keep. Третьего модельного арбитра нет.

Sol получает текущий script, assignment, существующий editorial_frame (если есть план), `revision_guidance` с двумя origin (`reader`, `fact`): actionable findings, исходные якоря, research_requests, duration_note, listener_understanding, якоря сохраняемых мест. В новой проекции /4 общие important_limits остаются только в полном отзыве; существенное адресное условие сохраняется в findings.preserve, requested_change и опорах. Опоры — авторская выборка плюс карточки, прямо названные фактологическими находками; точные `repair_evidence` содержат выдержки только из этих названных карточек. Полный фактологический архив не повторяется. Суждения сохраняются в исходных отзывах. `dual-revise.md` требует сохранять фактические ограничения при стилевой правке.

`max_script_revisions` равен 0, 1 или 2 (по умолчанию 2). Правки `revise-1`, `revise-2` всегда проходят свежие `reader-review-N`, `fact-review-N`. Положительный keep возможен только для версии, которую прочли обе роли. Исчерпание циклов и needs_material сохраняют terminal outcome с обоими отзывами и точной последней версией, без публикационного допуска.

Каждое чтение/правка закрепляет script_ref/hash, refs отзывов, profile, prompt, schema и manifest. Промпты сохраняются до первого вызова. Resume использует сохранённые requests, не читает текущие промпты и не повторяет завершённые чтения. `--review-anchor-policy bounded-selection-v1` либо `resume --repair-review-anchors` допускает одно исправление адресов на роль и версию; суждения, support_labels и режим доступа сохраняются. Неоднозначный исход сетевого вызова не порождает автоматического повтора. Credit recovery работает отдельно по UUID попытки.

## Сохранённый сценарий

`script-import --file script.json --out script-ref.json` принимает чистый полный `WriteOutput` (utterances), сохраняет исходные bytes и смысловой hash. Точные bytes идемпотентны; разные JSON-представления не конфликтуют.

В `run` передайте `--initial-script-ref @script-ref.json`, обычные shared author/ref и профили. В этом случае новый draft и новый план не генерируются; fixed-plan input необязателен. Можно также передать RuntimeRef существующего сохранённого draft/final. Модельные запросы возникают только при явном `run`/`resume`.

`preview --stage reader-review|fact-review --draft script.json` собирает такой же wire без провайдера. Для `revise-1|revise-2` нужны также `--reader-feedback` и `--fact-feedback`; approved-plan опционален. Прочие общие параметры preview прежние.

Export содержит отдельные архивы всех attempts и JSON/text каждой версии, `reading-round-N.json`, `review-outcome.json`, человеческий `review-result.md`. Только keep создаёт `final.json/txt`; отказ сохраняет `candidate-final.json/txt`. `run`/`resume` возвращает код 2 при terminal отказе, код 0 при keep; оба результата остаются `production_ready=false`.

## Совместимость

Manifest без review_mode остаётся informed-v1; отсутствие role_projection_version по-прежнему означает /1. Старые fingerprints, ролевые проекции /1–/3, pinned prompts/requests и завершённые результаты не меняются. Старый режим явно выбирается `--review-mode informed-v1`. Legacy-author inputs автоматически сохраняют informed-v1. Новые необязательные listener_profile/genre при отсутствии не добавляются в сериализацию исторического Assignment. Миграция 0015 расширяет только перечень стадий и допустимые адресные repairs.
