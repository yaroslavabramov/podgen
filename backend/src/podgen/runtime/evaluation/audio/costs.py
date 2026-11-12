from decimal import Decimal


def estimate(profile, usage, basis):
    rates = {r.unit: r for r in profile.rates}
    if not rates or any(unit not in usage for unit in rates):
        return dict(
            status="unknown", usage=usage, basis=basis, usd=None, reservation_usd=profile.reservation_usd
        )
    usd = sum(
        (
            Decimal(rate.usd_per_million) * Decimal(usage[unit]) / Decimal(1000000)
            for unit, rate in rates.items()
        ),
        Decimal(0),
    )
    return dict(
        status="measured_usage_estimate"
        if basis.startswith("provider_reported")
        else "submitted_text_estimate",
        usage=usage,
        basis=basis,
        usd=str(usd),
        reservation_usd=profile.reservation_usd,
        invoice_verified=False,
    )


def charge(cost):
    return Decimal(cost["usd"]) if cost.get("usd") is not None else Decimal(cost["reservation_usd"])
