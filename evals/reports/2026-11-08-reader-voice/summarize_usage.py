"""Aggregate recorded usage only; never construct a provider or make a new call."""

from decimal import Decimal

from run_readings import HERE, read, save


def main():
    files = (
        "results-v1.json",
        "results-wording-first.json",
        "results-speech-01-04-05.json",
        "results-speech-v2-01-04-05.json",
    )
    rows = []
    for name in files:
        result = read(HERE / name)
        assert result["failures"] == []
        rows.extend(dict(row, source=name) for row in result["results"])
    new = [row for row in rows if not row.get("reused", False)]
    usage = {
        "new_calls": len(new),
        "archived_reused": len(rows) - len(new),
        "new_estimated_usd": str(sum(Decimal(row["estimated_usd"]) for row in new)),
        "input_tokens": sum(row["usage"]["input_tokens"] for row in new),
        "output_tokens": sum(row["usage"]["output_tokens"] for row in new),
        "cached_tokens": sum(
            row["usage"].get("input_tokens_details", {}).get("cached_tokens", 0)
            for row in new
        ),
        "cache_write_tokens": sum(
            row["usage"].get("input_tokens_details", {}).get("cache_write_tokens", 0)
            for row in new
        ),
        "pricing_basis": read(HERE / "results-v1.json")["pricing_basis"],
        "stages": [
            {
                "source": name,
                "new_estimated_usd": read(HERE / name)["new_estimated_usd"],
            }
            for name in files
        ],
    }
    save(HERE / "usage.json", usage)
    print(usage)


if __name__ == "__main__":
    main()
