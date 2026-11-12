from .observations import charged_or_reserved


def export(store, config):
    manifest = store.get("manifest.json")
    if not manifest:
        return dict(status="not_prepared", production_ready=False)
    cases = []
    for case in manifest["cases"]:
        rows = store.attempts(case)
        last = rows[-1] if rows else None
        state = "prepared"
        value = None
        if last:
            if last["result"]:
                state, value = "response_saved", last["result"]["provider_response"]
            elif last["failure"]:
                state = "response_invalid"
            elif not last["receipt"] or not last["receipt"]["complete"] or last["receipt"]["error"]:
                state = "outcome_unknown"
            else:
                state = "provider_rejected"
        cases.append(
            dict(
                case_id=case["case_id"],
                status=state,
                attempts=len(rows),
                idempotency_key=case["idempotency_key"],
                credits_or_reservation=charged_or_reserved(case, rows),
                policy_violations=last["result"]["policy_violations"] if last and last["result"] else [],
                model_version=value.get("model_version") if value else None,
                engine_route=value.get("engine_route") if value else None,
                billing=value.get("billing") if value else None,
                model_changed=any(store.get(row["key"] + "/model-change.json") is not None for row in rows),
                provider_response=value,
            )
        )
    all_saved = all(c["status"] == "response_saved" for c in cases)
    blocked = any(c["policy_violations"] or c["model_changed"] for c in cases)
    report = dict(
        contract="toneperfect-qualification-report/1",
        run_id=str(config.run_id),
        status="responses_saved" if all_saved and not blocked else "needs_attention",
        cases=cases,
        requests_submitted=sum(c["attempts"] for c in cases),
        credits_or_reservation=sum(c["credits_or_reservation"] for c in cases),
        credit_cap=config.max_credits,
        qualification_status="not_evaluated",
        production_ready=False,
    )
    # An immutable report per distinct state; no prior observation is replaced.
    from podgen.domain.canonical import digest

    path = "exports/" + digest(report) + ".json"
    store.put(path, report)
    return dict(report, report_path=str(store.root / path))
