"""One bounded diagnostic: only the form changes against the v1 readings."""

import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from copy import deepcopy
from decimal import Decimal
from uuid import NAMESPACE_URL, uuid5

from podgen.runtime.models.contracts import ProviderFault
from pydantic import ValidationError
from run_readings import HERE, ROOT, one, read, save
from wording_form import describe_wording_first


def main():
    rows = []
    for case in ("01", "04", "05"):
        source = HERE / "runs/new-v1" / case
        request = deepcopy(read(source / "request.json"))
        describe_wording_first(request["text"]["format"]["schema"])
        run_id = uuid5(NAMESPACE_URL, "podgen-reader-wording-first:" + case)
        request["metadata"]["podgen_run_id"] = str(run_id)
        request["metadata"]["podgen_invocation_id"] = str(
            uuid5(run_id, "invocation:reader-review-2")
        )
        directory = HERE / "runs/wording-first" / case
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / "request.json"
        if path.exists():
            assert read(path) == request
        else:
            save(path, request)
        call = deepcopy(read(source / "call.json"))
        call.update(role="wording-first", reuse_archival=False)
        if (directory / "call.json").exists():
            assert read(directory / "call.json") == call
        else:
            save(directory / "call.json", call)
        rows.append(
            {"role": "wording-first", "case": case, "path": str(path.relative_to(ROOT))}
        )
        prior = read(source / "request.json")
        assert (
            request["instructions"] == prior["instructions"]
            and request["input"] == prior["input"]
        )
        for key in (
            "model",
            "reasoning",
            "max_output_tokens",
            "tools",
            "store",
            "background",
        ):
            assert request[key] == prior[key]
    save(
        HERE / "requests-wording-first.json",
        {"diagnostic_only": True, "production_wiring": False, "requests": rows},
    )
    results = []
    failures = []
    with ThreadPoolExecutor(max_workers=3) as executor:
        futures = {executor.submit(one, row): row for row in rows}
        for f in as_completed(futures):
            try:
                results.append(f.result())
            except (
                ProviderFault,
                ValidationError,
                ValueError,
                AssertionError,
                OSError,
                KeyError,
            ) as error:
                failures.append(
                    {
                        "case": futures[f]["case"],
                        "code": getattr(error, "code", type(error).__name__),
                    }
                )
    result = {
        "diagnostic_only": True,
        "results": sorted(results, key=lambda x: x["case"]),
        "failures": failures,
        "new_calls": len(results),
        "new_estimated_usd": str(sum(Decimal(x["estimated_usd"]) for x in results)),
    }
    save(HERE / "results-wording-first.json", result)
    print(
        json.dumps(
            {
                "completed": len(results),
                "failures": failures,
                "cost": result["new_estimated_usd"],
            }
        ),
        flush=True,
    )
    return bool(failures)


if __name__ == "__main__":
    raise SystemExit(main())
