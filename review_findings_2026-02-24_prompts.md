## Prompt Audit Findings (2026-02-24)

Аудит всех 15 системных промптов против `PROMPT_RULES.md`.

### Inventory

| Фаза | Модуль | Переменная | LLM | Human-readable output |
|------|--------|-----------|-----|----------------------|
| 0 | show_configurator | `SHOW_BIBLE_SYSTEM` | Sonnet | да (host names, policies) |
| 2 | decomposer | `DECOMPOSER_SYSTEM` | Sonnet | да (questions, hints) |
| 3.1 | hunter | `EXTRACT_EVIDENCE_SYSTEM` | Haiku | нет (quotes from source) |
| 3.2A | librarian_fast | `EXTRACT_WEB_EVIDENCE_SYSTEM` | Sonnet | нет (quotes from source) |
| 4.1 | claim_builder | `BUILD_CLAIMS_SYSTEM` | Sonnet | да (statements) |
| 4.2 | verifier | `VERIFIER_SYSTEM` | Sonnet | да (reasoning) |
| 4.3 | conflict_resolver | `RESOLVE_SYSTEM` | Sonnet | да (what/why/how) |
| 5.1 | showrunner | `SHOWRUNNER_SYSTEM` | Sonnet | да (act titles) |
| 5.2 | act_planner | `ACT_PLANNER_SYSTEM` | Sonnet | да (descriptions, notes) |
| 5.2 | act_planner | `EVIDENCE_EXTRACT_SYSTEM` | Haiku | нет (quotes from source) |
| 5.3 | integrator | `INTEGRATOR_SYSTEM` | Sonnet | да (transition notes, suggestions) |
| 6.1 | screenwriter | `SCREENWRITER_SYSTEM` | Sonnet | да (dialogue) |
| 6.2 | writer_revolt | `EVAL_SYSTEM` | Haiku | да (suggestions) |
| 6.3 | citation_auditor | `AUDITOR_SYSTEM` | Sonnet | да (descriptions, suggestions) |
| 7.2 | shownotes | `SHOWNOTES_SYSTEM` | Haiku | да (section titles, positions) |
| ingestion | canonicalizer | inline | Haiku | нет |
| ingestion | scout | inline | Haiku | да (topic names, summary) |

---

### [P1] Language field отсутствует в промптах с human-readable output

**Rule 4:** "Pass output language explicitly as an input field."

Промпты, генерирующие human-readable текст, но **не упоминающие язык**:
- `SHOWRUNNER_SYSTEM` — act titles
- `ACT_PLANNER_SYSTEM` — scene descriptions, notes
- `INTEGRATOR_SYSTEM` — transition notes, suggestions
- `RESOLVE_SYSTEM` — what_conflicts, how_to_present
- `VERIFIER_SYSTEM` — reasoning
- `EVAL_SYSTEM` — weak_points suggestions
- `AUDITOR_SYSTEM` — issue descriptions, suggestions
- `SHOWNOTES_SYSTEM` — section titles, positions, reasons
- scout (inline) — topic names, summary

`BUILD_CLAIMS_SYSTEM` — единственный, который упоминает "target language" для statements.
`SCREENWRITER_SYSTEM` — получает `language` в state, но промпт не инструктирует "write in {language}".
`SHOW_BIBLE_SYSTEM` — говорит "All human-readable values must be in the requested output language" (правильно).

**Impact:** Для RU-подкастов промежуточные артефакты (act titles, scene descriptions, conflict briefs, shownotes) могут быть на английском, т.к. LLM по умолчанию отвечает на языке промпта (EN).

---

### [P1] Failure/empty behavior не определён

**Rule 3:** "Define null/empty behavior explicitly."

Промпты **без** определения fallback при невозможности выполнить задачу:
- `RESOLVE_SYSTEM`
- `SHOWRUNNER_SYSTEM`
- `ACT_PLANNER_SYSTEM`
- `INTEGRATOR_SYSTEM`
- `SCREENWRITER_SYSTEM`
- `EVAL_SYSTEM`
- `AUDITOR_SYSTEM`
- `SHOWNOTES_SYSTEM`

Промпты **с** корректным fallback:
- `EXTRACT_EVIDENCE_SYSTEM` (hunter): `Return [] if nothing relevant found`
- `EVIDENCE_EXTRACT_SYSTEM` (act_planner): defines null return `{"quote": null, ...}`

---

### [P2] Enum регистр несогласован между промптами

**Rule 8:** "Keep one naming style for keys."

- `EXTRACT_EVIDENCE_SYSTEM` (hunter): `asset_type` UPPERCASE (`STATISTIC|ANECDOTE|...`)
- `EXTRACT_WEB_EVIDENCE_SYSTEM` (librarian): UPPERCASE
- `EVIDENCE_EXTRACT_SYSTEM` (act_planner): lowercase (`anecdote|case|...`)
- `BUILD_CLAIMS_SYSTEM`: `claim_type` UPPERCASE (`FACT|OPINION|REPORT`)
- `VERIFIER_SYSTEM`: `verification_level` UPPERCASE (`VERIFIED|OBSERVED|...`)
- DB модели (`AssetType`, `ClaimType`, `VerificationLevel`): все lowercase

Код нормализует (`.lower()`, `.upper()`), но промпты должны быть единообразны. Рекомендация: lowercase, совпадает с DB.

---

### [P2] Unbounded context inputs

**Rule 6:** "Bound all long inputs."

- `SHOWRUNNER_SYSTEM` — получает **все** claims проекта (может быть 30-50+), усечение только `.statement[:300]`
- `BUILD_CLAIMS_SYSTEM` — получает все evidence_ids проекта
- `SCREENWRITER_SYSTEM` — beat_sheet + claims + evidence per scene без явного лимита в промпте
- `SHOWNOTES_SYSTEM` — все claims + все evidence + все sources

Код частично ограничивает (`.[:300]`, `[:20]`), но промпты не документируют ожидаемый размер входа.

---

### [P3] Evidence extractor получает обрезанный контент

`EVIDENCE_EXTRACT_SYSTEM` (act_planner) просит "exact verbatim text from the chunk", но получает `preview[:300]`. Модель может галлюцинировать цитату длиннее 300 символов.

---

### [P3] Скрытый fallback в citation_auditor

`citation_auditor.py:101-104` — если аудит сегмента бросает исключение, сегмент помечается `PASSED`. Это скрытый grace-degrade, не описанный ни в промпте, ни в контракте. Rule 7: "Use deterministic fallback when safe" — но fallback должен быть документирован.

---

### [P3] Model asked to return IDs from input (anti-pattern risk)

**Rule 9:** "Asking model to invent runtime-owned identifiers."

- `BUILD_CLAIMS_SYSTEM`: `supporting_evidence_ids`, `illustrative_evidence_ids` — модель копирует UUIDs из входа. Если галлюцинирует UUID → несуществующий ID в DB.
- `ACT_PLANNER_SYSTEM`: `claim_ids`, `evidence_ids` — аналогично.

Код фильтрует через `allowed_claim_ids`/`allowed_evidence_ids`, так что на практике это safe. Но промпт мог бы явно сказать "Only use IDs from the provided input".
