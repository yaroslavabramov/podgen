"""One explicit retry of a proven no-generation credit refusal; never a transport retry policy."""

from copy import deepcopy
from uuid import uuid5

from podgen.domain.contracts.common import BlobRef, RuntimeRef

from .contracts import ALL_STAGES, RunFailure
from .validation import load_unique_json


def refusal_evidence(journal, invocation):
    from .source_tool_recovery import evidence_target

    target = evidence_target(journal, invocation)
    if target is not None:
        try:
            return refusal_evidence(*target)
        except RunFailure as error:
            raise RunFailure(
                error.code, run_id=invocation["run_id"], invocation_id=invocation["invocation_id"]
            ) from None

    def reject(code="credit_refusal_retry_not_eligible"):
        raise RunFailure(
            code,
            run_id=invocation["run_id"],
            invocation_id=invocation["invocation_id"],
        )

    if invocation["status"] in ("submitting", "waiting", "outcome_unknown"):
        reject("credit_refusal_retry_outcome_unknown")
    if (
        invocation["status"] != "failed"
        or invocation["provider_response_id"] is not None
        or invocation["output_record_id"] is not None
    ):
        reject()
    submits = []
    errors = []
    for event in journal.events(invocation):
        ref = RuntimeRef(record_kind="tool_execution", record_id=event["record_id"])
        record = journal.read(ref)
        if record["invocation_id"] != str(invocation["invocation_id"]) or record["request_ref"][
            "record_id"
        ] != str(invocation["request_record_id"]):
            reject()
        if event["event_kind"] == "submit_intent":
            submits.append(record)
        if event["event_kind"] == "provider_error":
            errors.append((ref, record))
        if event["event_kind"] in (
            "provider_response",
            "retrieve_error",
            "failure",
            "output_validation_error",
        ):
            reject()
    if (
        len(submits) != 1
        or submits[0]["data"].get("client_request_id") != str(invocation["invocation_id"])
        or len(errors) != 1
    ):
        reject()
    ref, error = errors[0]
    data = error["data"]
    if (
        data.get("operation") != "submit"
        or data.get("ambiguous") is not False
        or data.get("code") != "provider_http_429"
        or error.get("provider_http_status") != 429
        or error.get("provider_response_id") is not None
        or data.get("provider_response_id") is not None
        or not error.get("response_blob")
    ):
        reject()
    try:
        body = load_unique_json(journal.read_blob(ref, BlobRef.model_validate(error["response_blob"])))
        from .provider_rejections import credit

        if not credit(body):
            reject()
    except (ValueError, KeyError, TypeError):
        reject()
    return ref


def authorize_credit_retry(journal, run_id, target_id, *, guard, allow_create=True):
    target = None
    rows = None
    from .reading_workflow import ALL_READING_STAGES

    for stage in (*ALL_STAGES, *ALL_READING_STAGES):
        found = journal.attempts(run_id, stage)
        match = next((row for row in found if row["invocation_id"] == target_id), None)
        if match:
            target, rows = match, found
            break
    if target is None:
        raise RunFailure("credit_refusal_retry_not_eligible", run_id=run_id)
    receipt = refusal_evidence(journal, target)
    child = next(
        (
            row
            for row in rows
            if row["attempt_kind"] == "credit_retry" and row["parent_invocation_id"] == target_id
        ),
        None,
    )
    if child is not None:
        return child
    if not allow_create or rows[-1]["invocation_id"] != target_id:
        raise RunFailure("credit_refusal_retry_not_current", run_id=run_id, invocation_id=target_id)
    guard()
    from .source_tool_recovery import resume_authorization, retry_request

    body = deepcopy(retry_request(journal, target))
    new_id = uuid5(target_id, "credit-refusal-retry:1")
    index = target["attempt_index"] + 1
    body["metadata"].update(
        podgen_invocation_id=str(new_id),
        podgen_attempt_index=str(index),
        podgen_parent_invocation_id=str(target_id),
    )
    original = journal.read(RuntimeRef(record_kind="tool_execution", record_id=target["request_record_id"]))
    return journal.prepare(
        run_id,
        target["stage"],
        body,
        deepcopy(original["context"]),
        attempt_index=index,
        parent_invocation_id=target_id,
        attempt_kind="credit_retry",
        operation_kind=target["operation_kind"],
        authorization=dict(
            kind="credit-refusal-retry-v1",
            actor_id=str(journal.repo.actor_id),
            target_invocation_id=str(target_id),
            refusal_event_ref=receipt.model_dump(mode="json"),
            maximum_new_attempts=1,
            **resume_authorization(journal, target),
        ),
    )


def recovery_hint(code):
    from podgen.retrieval.recovery import hint

    message = hint(code)
    if message is not None:
        return message
    return {
        "technical_recovery_limit_exhausted": "Две технические замены этой стадии уже использованы. Все ответы, обращения к источникам и неизвестный расход сохранены; новая генерация не отправлена.",
        "run_token_budget_exceeded": "Общий токенный лимит исчерпан до отправки. Для доказанного превышения общего input доступен resume --retry-total-input-budget UUID --max-total-input-tokens N. Остальные лимиты сохраняются.",
        "legacy_outbound_disabled_start_compact_run": "Старый полный запрос сохранён для просмотра. Сетевое продолжение отключено: создайте новый запуск с editorial-role-packets/1 и новым run-id.",
        "role_visible_byte_budget_exceeded": "Запрос сохранён и генерация не отправлена. Для этого UUID доступен resume --retry-input-budget UUID --max-visible-bytes 262144 --max-input-tokens 65536; реальные токены, контекст модели и общий бюджет проверяются заново.",
        "role_input_token_budget_exceeded": "Подсчёт превысил местный токенный порог; генерация не отправлена. Адресное продолжение: resume --retry-input-budget UUID --max-input-tokens 65536. Контекст модели и общий бюджет остаются обязательными.",
        "editorial_needs_material": "Отзыв сохранён; для правки требуются дополнительные источники. Генерация правки не отправлена.",
        "contradictory_editorial_decision": "Отзыв сохранён, но решение не согласуется с правками или вопросами. Требуется явное редакционное решение.",
        "plan_target_repair_not_eligible": "Исправление адресов допустимо только для сохранённого plan-review с невалидными указателями и неизменённым планом; суждения и решение не переписываются.",
        "plan_target_repair_action_used": "Отдельная попытка исправления адресов уже использована; нового исправления не создано. Подтверждённый отказ по балансу возобновляется отдельно через --retry-credit-refusal.",
        "plan_target_repair_already_used": "Для этой версии plan-review попытка исправления адресов уже использована.",
        "plan_target_repair_not_current": "Указанная попытка plan-review уже не текущая.",
        "credit_refusal_retry_not_eligible": "Повтор разрешён только для архивного отказа submit: HTTP 429, credit_balance_exhausted, без response ID и неоднозначности. Успешные ответы и другие ошибки не повторяются.",
        "credit_refusal_retry_outcome_unknown": "Исход запроса неизвестен либо уже имеется незавершённый ответ провайдера. Повторная генерация запрещена; требуется сверка сохранённого ответа/состояния у провайдера.",
        "credit_refusal_retry_not_current": "Указанная попытка уже не текущая. Используйте UUID конкретной последней отказавшей попытки.",
        "credit_refusal_retry_action_used": "Это действие уже использовано. Нового повтора не создано. Для нового подтверждённого отказа по балансу явно укажите UUID новой отказавшей попытки.",
    }.get(code)
