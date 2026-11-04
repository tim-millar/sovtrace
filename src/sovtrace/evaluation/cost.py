"""Optional exact estimates from an explicitly configured verified schedule.

No provider prices are supplied. The caller is responsible for verifying the
source, date, model identities and service tier before injecting a schedule.
"""

from dataclasses import dataclass
from datetime import date
from decimal import Decimal, localcontext


@dataclass(frozen=True)
class PricingSchedule:
    """USD per million tokens; cache writes are distinct from ordinary input."""

    model: str
    service_tier: str
    source: str
    verified_on: date
    ordinary_input: Decimal
    cached_input: Decimal
    cache_write: Decimal
    output: Decimal
    observed_models: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not all(value.strip() for value in (self.model, self.service_tier, self.source)):
            raise ValueError("Pricing requires explicit model, tier and verification source")
        if not isinstance(self.verified_on, date):
            raise ValueError("Pricing requires a verification date")
        for rate in (self.ordinary_input, self.cached_input, self.cache_write, self.output):
            if not isinstance(rate, Decimal) or not rate.is_finite() or rate < 0:
                raise ValueError("Rates must be finite nonnegative Decimals")


def estimated_cost(operation: dict, *, pricing: PricingSchedule | None = None) -> Decimal | None:
    """Unavailable unless model/tier and every charging category are known."""
    if pricing is None:
        return None
    if (
        operation.get("requested_model") != pricing.model
        or operation.get("actual_model") not in (pricing.model, *pricing.observed_models)
        or operation.get("service_tier") != pricing.service_tier
        or operation.get("actual_call") is not True
        or operation.get("outcome") != "response"
    ):
        return None
    usage = operation.get("usage")
    keys = (
        "input_tokens",
        "cached_input_tokens",
        "cache_write_tokens",
        "output_tokens",
        "reasoning_tokens",
    )
    if not isinstance(usage, dict) or any(
        type(usage.get(k)) is not int or usage[k] < 0 for k in keys
    ):
        return None
    total, cached, written, output, reasoning = (usage[k] for k in keys)
    if cached + written > total or reasoning > output:
        return None
    rates = (pricing.ordinary_input, pricing.cached_input, pricing.cache_write, pricing.output)
    counts = (total - cached - written, cached, written, output)
    with localcontext() as context:
        context.prec = max(
            60,
            max(len(str(n)) for n in counts)
            + max(len(r.as_tuple().digits) + abs(int(r.as_tuple().exponent)) for r in rates)
            + 20,
        )
        return sum(
            (Decimal(n) * rate for n, rate in zip(counts, rates, strict=True)), Decimal(0)
        ) / Decimal(1_000_000)
