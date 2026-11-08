"""Reconcile position effects without consulting generation conditions."""

from decimal import Decimal

from run import HERE, LABELS, boxes, read, save


def decode(raw, order):
    return {"first": order[0], "second": order[1]}.get(raw, raw)


def main():
    result = []
    for left, right in boxes()[::2]:
        observations = []
        for order in (left + right, right + left):
            raw = read(HERE / "results/pairs" / order / "output.json")
            observations.append(
                {
                    "order": order,
                    "selected": decode(raw["judgment"], order),
                    "recordability": {
                        order[0]: raw["first"]["recording_status"],
                        order[1]: raw["second"]["recording_status"],
                    },
                    "reason": raw["editorial_reason"],
                }
            )
        choices = [row["selected"] for row in observations]
        result.append(
            {
                "pair": left + right,
                "orderings": observations,
                "joint": choices[0]
                if choices[0] == choices[1]
                else "order_disagreement",
            }
        )
    cold = {}
    for label in LABELS:
        r = read(HERE / "results/cold" / label / "output.json")
        cold[label] = {
            "recommendation": r["recommendation"],
            "revision_scale": r["revision_scope"]["scale"],
            "required_changes": [
                f["requested_change"]
                for f in r["findings"]
                if f["priority"] in ("important", "blocking")
            ],
        }
    save(HERE / "results/blind-comparison.json", {"cold": cold, "pairs": result})
    groups = {k: read(HERE / "cost" / f"{k}.json") for k in ("cold", "pairs")}
    amount = sum(Decimal(v["estimated_total_usd"]) for v in groups.values())
    save(
        HERE / "cost/total.json",
        {
            "calls": 12,
            "cold_estimated_usd": groups["cold"]["estimated_total_usd"],
            "pairs_estimated_usd": groups["pairs"]["estimated_total_usd"],
            "estimated_total_usd": str(amount),
            "pricing": "../../../2026-11-08-author-openings/pricing.json",
        },
    )


if __name__ == "__main__":
    main()
