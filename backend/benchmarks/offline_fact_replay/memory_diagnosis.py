"""Isolate replay-oracle overhead without PostgreSQL, providers or model inference."""

import argparse
import json
import os
import resource
import subprocess
import sys
from pathlib import Path
from uuid import UUID

from export_oracle import ExportOracle
from traffic import Traffic


def memory():
    return dict(
        rss_bytes=int(subprocess.check_output(["ps", "-o", "rss=", "-p", str(os.getpid())])) * 1024,
        cumulative_peak_bytes=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        * (1 if sys.platform == "darwin" else 1024),
    )


def size(value):
    visited = set()

    def count(item):
        if id(item) in visited:
            return 0
        visited.add(id(item))
        result = sys.getsizeof(item)
        if isinstance(item, dict):
            result += sum(count(key) + count(child) for key, child in item.items())
        elif isinstance(item, (list, tuple)):
            result += sum(count(child) for child in item)
        return result

    return count(value)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trace", type=Path, required=True)
    parser.add_argument("--export", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    report = dict(before_helpers=memory())
    oracle = ExportOracle(args.trace)
    report["after_export_oracle"] = memory()
    traffic = Traffic(args.trace, UUID("a07cc6d1-c399-5979-a698-084ece565c47"))
    report["after_traffic"] = memory()
    verification = oracle.verify(args.export)
    report["after_verification"] = memory()
    report.update(
        export_oracle_retained_python_bytes=size(oracle.saved),
        traffic_retained_python_bytes=size(
            [traffic.initial, traffic.events, traffic.counts, traffic.responses]
        ),
        verified_files=verification["files_verified"],
        limitations="Isolated helper overhead only; these RSS increments cannot simply be subtracted from another process high-water to infer runtime peak. No model, database or provider calls.",
    )
    traffic.client.close()
    args.out.write_text(json.dumps(report, indent=2))
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
