"""Compare full fake-provider polls against the archived implementation, offline."""

import argparse
import json
import subprocess
import time
import tracemalloc
from collections import Counter
from pathlib import Path

from pytest import MonkeyPatch

from podgen.research.hosted.service import advance
from podgen.research.hosted.state import status
from tests.hosted_research_support import Provider, ready, response


def legacy(ref):
    modules = {}
    for name in ("state", "service"):
        source = subprocess.check_output(
            ["git", "show", f"{ref}:backend/src/podgen/research/hosted/{name}.py"],
            text=True,
        )
        namespace = dict(__package__="podgen.research.hosted", __name__=f"baseline_{name}")
        exec(compile(source, f"{ref}/{name}.py", "exec"), namespace)
        modules[name] = namespace
    for name in ("exchanges", "latest", "status"):
        modules["service"][name] = modules["state"][name]
    return modules["service"]["advance"], modules["state"]["status"]


def measure(poll, check, history):
    with MonkeyPatch.context() as patch:
        journal, _ = ready(patch)
        provider = Provider(
            *(response(steps=[dict(content="retained provider text " * 200)]) for _ in range(history + 2))
        )
        poll(journal, provider)
        for _ in range(history):
            poll(journal, provider)
        work = Counter()
        for name in ("get", "bytes", "head", "blob", "put", "exchange"):
            original = getattr(journal, name)

            def observed(*args, _name=name, _original=original, **kwargs):
                work[_name] += 1
                value = _original(*args, **kwargs)
                if _name == "bytes":
                    work["blob_bytes_read"] += len(value)
                elif _name in ("get", "head") and value is not None:
                    work["record_bytes_read"] += len(json.dumps(value).encode())
                elif _name == "blob":
                    work["blob_bytes_written"] += len(args[0])
                return value

            patch.setattr(journal, name, observed)

        def sample(operation):
            work.clear()
            tracemalloc.start()
            started = time.perf_counter()
            result = operation()
            seconds = time.perf_counter() - started
            _, peak = tracemalloc.get_traced_memory()
            tracemalloc.stop()
            assert result["state"] == "in_progress"
            return dict(work=dict(work), seconds=seconds, python_peak_bytes=peak)

        return dict(
            polls=history,
            next_poll=sample(lambda: poll(journal, provider)),
            status=sample(lambda: check(journal)),
        )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline-ref", default="05b7d855")
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    before, previous_status = legacy(args.baseline_ref)
    output = dict(
        qualification="complete_service_operations_with_fake_provider_and_memory_journal",
        baseline_ref=args.baseline_ref,
        real_provider_calls=0,
        baseline=[measure(before, previous_status, n) for n in (1, 20, 100)],
        current=[measure(advance, status, n) for n in (1, 20, 100)],
        limitations="SQL/real object-store measurements belong to test_hosted_research_head_persistence.py; Python peak allocation is not process RSS.",
    )
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(output, indent=2) + "\n")
    print(json.dumps(output))


if __name__ == "__main__":
    main()
