# Получение и извлечение источников

Runtime: `podgen.sources.acquisition.service.acquire_source`. Вход и результат — `AcquisitionRequest` / `AcquisitionResult` из `sources/acquisition/contracts.py`. Это отдельный от hosted research слой: URL из отчёта не становится EvidenceSpan автоматически.

## Профиль и права

Используется обычный production `Repository` с ACL проекта. Нужны существующие `Policy` refs для хранения и публикации. Минимальная подготовка:

```python
from uuid import uuid4
from podgen.domain.contracts.common import CodeProducer
from podgen.domain.contracts.evidence import SourceRights
from podgen.sources.acquisition.contracts import (
    VERSION, AcquisitionProfile, AcquisitionRequest,
)
from podgen.sources.acquisition.service import acquire_source

operation = uuid4()
producer = CodeProducer(
    kind="code", component="source-acquisition-setup",
    build_id=VERSION, operation_id=operation,
)
profile = repo.create(
    project_id, "ProcessingProfile",
    dict(
        profile_kind="extraction", implementation=VERSION, version="1",
        configuration=AcquisitionProfile().model_dump(mode="json"),
        input_profile_refs=[],
    ),
    producer=producer, operation_id=operation,
)
rights = SourceRights(
    access_class="project",
    storage_policy_ref=storage_policy_ref,
    publication_policy_ref=publication_policy_ref,
)
request = AcquisitionRequest(
    operation_id=uuid4(), source_id=uuid4(), url=reported_url,
    profile_ref=profile.ref, rights=rights, language="und",
)
result = await acquire_source(repo, project_id, request)
```

Повторять нужно тот же request, включая `operation_id`. Сохранённые original и extractor output используются повторно. Изменение request под прежним ID отклоняется. Параллельный запуск той же операции получает `source_acquisition_busy`. Повторная загрузка новой версии URL требует новой операции.

## Что сохраняется

Сначала сохраняются точные полученные bytes и acquisition record: URL, конечный URL, redirect chain, response headers, status, время. Размер ограничивается во время HTTP streaming; распаковка compressed body имеет отдельное такое же ограничение. DNS проверяется при соединении, подключение идёт к проверенному IP, proxies из окружения отключены. Cross-origin redirect не переносит credentials.

`SourceRevision` указывает на эти bytes. Извлечение читает их из ObjectStore, а не делает второй GET. Фактически выбранный extractor получает собственный frozen `ProcessingProfile`, связанный с исходным acquisition profile; representation сохраняет его имя, версию и configuration hash. Полный extractor output хранится без обрезания. Paragraph ranges относятся к этому тексту; точные DOM/PDF anchors не выдумываются.

Jina, MediaWiki, promoted PDF и rendered DOM сохраняются как отдельные originals/revisions. Их связь с первоначальным URL видна в acquisition records и source metadata. Jina не выдаётся за извлечение сохранённого HTML. Browser snapshot явно отличается от HTTP original.

Статусы `available`, `unavailable`, `unconfirmed`, `extraction_failed` описывают техническую доступность. Они не доказывают достоверность содержания или полноту документа. Captcha, consent-only, publisher shell, HTTP error, redirect к login сохраняются с диагностикой. Содержательный документ с HTTP 403 не отбрасывается только из-за статуса.

## Локальная переработка

После установки extractor или исправления extraction используйте новую операцию без нового fetch:

```python
from podgen.sources.acquisition.contracts import ReExtractionRequest
from podgen.sources.acquisition.reprocess import reprocess_source

result = await reprocess_source(
    repo, project_id,
    ReExtractionRequest(
        operation_id=uuid4(),
        source_revision_ref=saved_source_revision_ref,
        profile_ref=profile.ref,
        reason="Install the missing local extractor",
    ),
)
```

У этого API нет fetcher. Он читает original через существующий SourceRevision, сохраняет прежнюю ошибку и создаёт новые результаты. Повтор той же операции использует сохранённый результат. После отзыва ACL чтение запрещено.

## Зависимости и ограничения

Основной environment содержит Trafilatura, pypdfium2 и PyPDF2. PDF сначала обрабатывается Marker worker с жёстким timeout, затем text-layer fallback. Отсутствие OCR или ошибка Marker сохраняются в diagnostics; пустой результат не становится успешным чтением скана.

Проверенный legacy OCR stack закреплён: Marker 1.5.5, Surya 0.12.1, transformers 4.57.6. Он требует отдельного Python 3.12–3.13; legacy regex dependency не собирается в текущем Python 3.14. Для OCR есть отдельный проект и lockfile `backend/ocr`: `uv sync --project backend/ocr --python 3.13`. Затем задайте абсолютный путь `backend/ocr/.venv/bin/python` в `marker_python_executable` AcquisitionProfile. Worker проверяет версию Marker. Основной runtime не наследует ограничения OCR на tokenizers/huggingface-hub. OCR model weights должны быть доступны этому environment; live OCR на новых документах требует отдельной проверки.

Browser — optional extra `browser`, плюс установленный Chromium (`playwright install chromium`). Для включения задайте `enable_browser=True`. Cross-origin assets и service workers блокируются; DOM ограничен по размеру. Снимок может отличаться от полноценно отрисованной страницы. Guard защищает redirects, subrequests, WebSocket и прямые WebRTC transports.

Jina выключена по умолчанию; включается `enable_jina=True` с credential из `jina_credential_env` (по умолчанию `JINA_API_KEY`). Credentials не сохраняются в исходниках. PDF со страницы публикации — отдельный fetch и отдельная версия; это не повторный GET для HTML extraction.
