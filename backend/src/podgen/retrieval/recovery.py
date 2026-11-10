"""Operational retrieval messages without changing the saved model task or enabling fallback."""

HINTS = {
    "source_search_models_not_installed": "Поисковые модели ещё не установлены. Выполните uv run --directory backend python -m podgen.retrieval prepare, затем resume прежнего run. Оплаченные ответы сохранятся.",
    "source_search_model_checksum_mismatch": "Контрольная сумма поисковой модели не совпала. Повторите python -m podgen.retrieval prepare, затем resume; поиск не заменяется буквальным.",
    "source_search_dependencies_missing": "Установите зависимости: uv sync --directory backend --locked, затем подготовьте модели python -m podgen.retrieval prepare. После этого доступен обычный resume.",
    "source_search_model_load_failed": "Проверьте зависимости uv sync --directory backend --locked и модели python -m podgen.retrieval check. После устранения ошибки доступен обычный resume без повторной оплаты сохранённых ответов.",
    "source_search_index_unavailable": "Поисковый индекс PostgreSQL недоступен. Проверьте pgvector и миграции Alembic, затем повторите resume. Уже оплаченные ответы и результаты инструментов сохранятся.",
    "source_search_corpus_limit_exceeded": "Корпус превышает закреплённый профиль поиска: до 64 первоисточников, 2 млн символов и 12 тыс. фрагментов. Источники не усекались; нужен профиль для большего корпуса.",
}


def hint(code):
    return HINTS.get(code)
