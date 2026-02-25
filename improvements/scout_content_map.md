# Проблема: Scout Content Map — бедный вход и отсутствие структуры

## Текущее поведение

`backend/app/ingestion/scout.py`, функция `_build_chunk_summaries()`:

```python
summary = {
    "source": source_title or "Untitled",
    "type": source_type,
    "headings": chunk.heading_path,       # часто []
    "preview": chunk.content[:200],       # обрезка, не аннотация
}
```

Плоский список чанков → один Haiku вызов → ContentMap JSON.

## Что должно быть по архитектуре (PIPELINE.md 1.4)

> H1/H2/аннотации + (опционально) частотные сущности/термины → компактная карта корпуса (3–5k токенов)

### Правильный flow scouting:

1. **Скелет каждого источника** — H1/H2 + lead/abstract под каждой секцией (1-2 строки), путь секции
2. **Сигналы покрытия** — длина источника (токены), число секций, глубина структуры
3. **Сущности/термины** — дешёвый TF mining или NER по заголовкам/лидам
4. **Дедуп/кластеризация** — убрать дубли заголовков между источниками, грубая группировка
5. **Выход** — компактный JSON 3–5k токенов для контекста reasoning-модели Phase 2

## Расхождения с текущей реализацией

| Шаг | Ожидание | Реальность | Проблема |
|-----|----------|------------|---------|
| Скелет | H1/H2 + lead | `heading_path` (часто `[]`) + `content[:200]` | Нет структуры, обрезка вместо lead |
| Покрытие | Размер, число секций, глубина | Не передаётся | Decomposer не знает масштаб |
| Сущности | TF/NER по заголовкам | Делегировано Haiku на плохом входе | Работает, но неточно |
| Дедуп | Убрать дубли заголовков | Нет | Засоряет карту |
| Агрегация | Per-source: "источник X → секции A,B,C" | Плоский список чанков | Потеряна структура источников |

## Предлагаемый план исправления

### Шаг 1: Агрегация по источникам (без LLM)

Вместо плоского списка чанков — сгруппировать по `source_id`:

```python
{
    "source": "HKMA - History of Note-issuing Banks",
    "type": "web",
    "url": "https://www.hkma.gov.hk/...",
    "published_at": "2019-08-26",
    "total_tokens": 350,
    "chunk_count": 1,
    "sections": [
        {"headings": [], "lead": "Hong Kong's currency notes are issued by commercial banks..."}
    ]
}
```

Данные уже есть в БД: `Source.title`, `Source.source_type`, `Source.original_url`, `Source.published_at`, `Chunk.token_count`, `Chunk.heading_path`.

### Шаг 2: Lead вместо обрезки (без LLM)

Для `lead` — первое предложение чанка (split по `. ` с минимальной длиной), не `content[:200]`.

### Шаг 3: Обновить Haiku промпт

Промпт должен получать source-level структуру, а не chunk-level плоский список.

### Шаг 4 (опционально): Частотные термины (без LLM)

Простой TF по heading_path + leads — top-N терминов. Добавить в промпт как подсказку.

## Файлы для изменения

- `backend/app/ingestion/scout.py` — `_build_chunk_summaries()` → `_build_source_summaries()`, обновить промпт
- `backend/app/models/chunk.py` — проверить что `token_count` доступен
- Промпт в `_extract_content_map()` — адаптировать под source-level вход

## Оценка сложности

Шаги 1-3: ~1-2 часа, только scout.py, без миграций, без новых зависимостей.
Шаг 4: опциональный, +30 минут.
