from decimal import Decimal
from fractions import Fraction

import pytest
from hypothesis import given
from hypothesis import strategies as st
from pydantic import ValidationError

from remit.kernel import CurrencyMismatch, Money, RemitError, minor_units
from remit.kernel.money import DIGITS


def amounts(currency: str = "GBP", places: int = 2) -> st.SearchStrategy[Money]:
    bound = Decimal(10**9)
    return st.decimals(min_value=-bound, max_value=bound, places=places).map(
        lambda amount: Money.of(amount, currency)
    )


@given(amounts(), amounts(), amounts())
def test_addition_is_associative_and_commutative(a: Money, b: Money, c: Money) -> None:
    assert (a + b) + c == a + (b + c)
    assert a + b == b + a


@given(amounts())
def test_an_amount_and_its_negation_cancel(a: Money) -> None:
    assert (a + -a).is_zero
    assert a - a == Money.zero("GBP")


@given(amounts())
def test_json_round_trip_is_exact(a: Money) -> None:
    assert Money.model_validate_json(a.model_dump_json()) == a


@given(amounts("GBP"), amounts("JPY", places=0))
def test_currencies_never_mix(a: Money, b: Money) -> None:
    with pytest.raises(CurrencyMismatch):
        _ = a + b


def test_equal_amounts_serialise_identically() -> None:
    assert Money.of(1, "GBP").model_dump_json() == Money.of("1.00", "GBP").model_dump_json()
    assert (-Money.zero("GBP")).model_dump_json() == Money.zero("GBP").model_dump_json()


@pytest.mark.parametrize(
    ("amount", "currency"),
    [("1.005", "GBP"), ("1.5", "JPY"), ("1.0005", "KWD"), ("NaN", "GBP"), ("1.00", "gbp")],
)
def test_amounts_that_do_not_fit_the_currency_are_refused(amount: str, currency: str) -> None:
    with pytest.raises(ValidationError):
        Money.of(amount, currency)


def test_floats_are_refused() -> None:
    with pytest.raises(ValidationError):
        Money(amount=0.1, currency="GBP")  # pyright: ignore[reportArgumentType]


def test_conversion_names_its_rounding() -> None:
    assert Money.of("10.01", "USD").convert("GBP", rate=Decimal("0.75")) == Money.of("7.51", "GBP")
    assert Money.of("0.05", "GBP").times(Decimal("0.5")) == Money.of("0.02", "GBP")


@pytest.mark.parametrize("code", ["GPB", "XAU", "XDR", "XXX", "ABC"])
def test_only_currencies_iso_lists_with_a_minor_unit_can_hold_amounts(code: str) -> None:
    with pytest.raises(ValidationError, match="not an ISO 4217 currency"):
        Money.of(1, code)
    with pytest.raises(RemitError, match="not a currency amounts can be held in"):
        minor_units(code)


@pytest.mark.parametrize(
    ("code", "places"),
    [("GBP", 2), ("JPY", 0), ("ISK", 0), ("KWD", 3), ("BHD", 3), ("CLF", 4), ("UYW", 4)],
)
def test_minor_units_come_from_the_iso_table(code: str, places: int) -> None:
    assert minor_units(code) == places
    assert Money.of(1, code).amount.as_tuple().exponent == -places


def test_large_sums_are_exact_not_rounded() -> None:
    large = Money.of("1" + "0" * 30, "GBP")  # beyond the 28 digits of Python's default
    assert (large + Money.of("0.01", "GBP")).amount == Decimal("1" + "0" * 30 + ".01")
    assert (-large).amount == Decimal("-1" + "0" * 30 + ".00")
    assert (large - Money.of("0.01", "GBP")).amount == Decimal("9" * 30 + ".99")


def test_a_sum_too_large_for_an_amount_is_refused_not_rounded() -> None:
    largest = Money.of("9" * (DIGITS - 2), "GBP")
    with pytest.raises(RemitError, match=f"at most {DIGITS} digits"):
        _ = largest + largest + largest + largest + largest + largest + largest + largest
    for digits in (DIGITS + 1, 3 * DIGITS):
        with pytest.raises(ValidationError, match=f"at most {DIGITS} digits"):
            Money.of("1" + "0" * digits, "GBP")
    with pytest.raises(RemitError, match=f"more than {DIGITS} digits"):
        largest.times(Decimal("9" * DIGITS * 2))


def test_floats_are_refused_however_they_arrive() -> None:
    with pytest.raises(ValidationError, match="floats are inexact"):
        Money.of(0.5, "GBP")  # pyright: ignore[reportArgumentType]
    with pytest.raises(RemitError, match="floats are inexact"):
        Money.of(1, "GBP").times(0.5)  # pyright: ignore[reportArgumentType]
    with pytest.raises(RemitError, match="finite"):
        Money.of(1, "GBP").convert("EUR", rate=Decimal("Infinity"))


def test_rounding_is_named_where_it_happens() -> None:
    half = Money.of("0.05", "GBP")
    assert half.times(Decimal("0.5")) == Money.of("0.02", "GBP")
    assert half.times(Decimal("0.5"), rounding="ROUND_HALF_UP") == Money.of("0.03", "GBP")
    assert Money.of("100", "JPY").convert("KWD", rate=Decimal("0.0020")) == Money.of("0.200", "KWD")


def test_amounts_compare_only_within_one_currency() -> None:
    assert Money.of(1, "GBP") < Money.of(2, "GBP") <= Money.of(2, "GBP")
    assert Money.of(3, "GBP") > Money.of(2, "GBP") >= Money.of(2, "GBP")
    with pytest.raises(CurrencyMismatch):
        _ = Money.of(1, "GBP") < Money.of(1, "EUR")


def test_allocation_loses_nothing_to_rounding() -> None:
    parts = Money.of(100, "GBP").allocate([1, 1, 1])
    assert [str(part) for part in parts] == ["GBP 33.34", "GBP 33.33", "GBP 33.33"]
    assert Money.of("-0.05", "GBP").allocate([1, 2]) == (
        Money.of("-0.02", "GBP"),
        Money.of("-0.03", "GBP"),
    )
    assert Money.of(10, "JPY").allocate([Decimal("0.3"), Decimal("0.7")]) == (
        Money.of(3, "JPY"),
        Money.of(7, "JPY"),
    )


@given(
    amounts(),
    st.lists(st.decimals(min_value=0, max_value=1000, places=3), min_size=1, max_size=12).filter(
        lambda weights: sum(weights) > 0
    ),
)
def test_allocated_parts_sum_to_the_whole_and_each_is_within_a_penny_of_its_share(
    whole: Money, weights: list[Decimal]
) -> None:
    parts = whole.allocate(weights)

    assert Money.total(parts, "GBP") == whole
    total = Fraction(sum(weights))
    for part, weight in zip(parts, weights, strict=True):
        share = Fraction(whole.amount) * Fraction(weight) / total
        assert abs(Fraction(part.amount) - share) < Fraction(1, 100)


@pytest.mark.parametrize("weights", [[], [0, 0], [1, -1]])
def test_allocation_needs_weights_that_make_sense(weights: list[int]) -> None:
    with pytest.raises(RemitError, match="weights"):
        Money.of(1, "GBP").allocate(weights)
