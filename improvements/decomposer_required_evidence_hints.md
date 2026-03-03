# Идея: семантические hint'ы в required_evidence

## Проблема

`required_evidence` в декомпозере задаёт тип evidence (`MECHANISM`, `DEFINITION`, etc.) без указания темы.
Gap detection и extraction validation работают только на уровне типа — если найден **любой** MECHANISM, требование считается выполненным, даже если он отвечает на периферийный вопрос, а не на целевой.

**Пример:** вопрос "что не позволяет банкам злоупотреблять правом печатать деньги?" требует MECHANISM.
Hunter находит MECHANISM про владение типографией (HKMA owns printing plant) — тип закрыт.
Но ключевой механизм (Certificates of Indebtedness + 100% резервирование) не извлечён и gap не объявляется.

## Текущий flow

```
Decomposer → required_evidence: [{type: "MECHANISM", min_count: 1}]
Hunter → нашёл любой MECHANISM → gap check: ✓
Phase 4 → claim про CIs может не сформироваться вообще
```

## Предлагаемое решение

Добавить опциональный `hint` в `required_evidence`:

```json
{
  "type": "MECHANISM",
  "min_count": 1,
  "hint": "Certificates of Indebtedness — как они ограничивают выпуск банкнот"
}
```

### Где использовать hint:

1. **Extraction prompt (Hunter):** передавать hint вместе с `required_evidence`, чтобы Haiku
   понимал не просто "нужен MECHANISM", а "нужен MECHANISM про CIs". Можно добавить поле
   `"satisfies_required": true/false` в LLM output для каждого item.

2. **Gap detection:** если ни один найденный MECHANISM не имеет `satisfies_required: true` для
   данного required target → объявить gap, даже если count >= min_count.

3. **Phase 3.2 (open-web):** при gap с hint'ом — использовать hint как дополнительный поисковый
   термин.

## Затрагиваемые компоненты

- `backend/app/pipeline/phase2_decomposition/decomposer.py` — схема вывода, промпт
- `backend/app/pipeline/phase3_research/hunter.py` — EXTRACT_EVIDENCE_SYSTEM, gap check
- `backend/app/pipeline/phase3_research/` — Phase 3.2 (open-web gap fill)

## Сложность

Средняя. Decomposer нужно научить генерировать hints (один дополнительный промпт-инструкция).
Hunter: небольшое расширение схемы + логика gap check.
Риск: hints могут быть слишком узкими и ложно объявлять gaps на хорошие доказательства.
