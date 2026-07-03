from __future__ import annotations

import argparse
import asyncio
import json
import uuid

from app.database import async_session_factory
from app.generation_v2.artifacts import summarize_tool_trace_costs


async def _main(run_id: uuid.UUID) -> None:
    async with async_session_factory() as db:
        report = await summarize_tool_trace_costs(db, run_id)
    print(json.dumps({"run_id": str(run_id), **report}, ensure_ascii=False, indent=2))


def main() -> None:
    parser = argparse.ArgumentParser(description="Print V2 run token-cost summary from tool traces.")
    parser.add_argument("run_id", type=uuid.UUID)
    args = parser.parse_args()
    asyncio.run(_main(args.run_id))


if __name__ == "__main__":
    main()
