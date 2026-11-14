from decimal import Decimal

import pytest
from hypothesis import given
from hypothesis import strategies as st
from pydantic import ValidationError

from remit.kernel import CurrencyMismatch, Money


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
