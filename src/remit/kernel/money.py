"""Money: an exact amount in one currency.

Amounts never round silently and never mix currencies. Arithmetic is exact: a sum that
would need more digits than an amount may hold is refused, not rounded. Anything that
must lose precision (`times`, `convert`, `allocate`) does so in one named, documented
way.

Currencies and their minor units come from the ISO 4217 table, published by its
maintenance agency and shipped by the `iso4217` package. Only currencies in current use
with a defined minor unit can hold amounts: not withdrawn codes, and not units such as
gold (XAU) or the SDR (XDR) for which ISO defines no minor unit.
"""

from collections.abc import Iterable, Sequence
from decimal import (
    Context,
    Decimal,
    DecimalException,
    DivisionByZero,
    Inexact,
    InvalidOperation,
    Overflow,
)
from fractions import Fraction
from typing import Annotated, Literal, Self

import iso4217
from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    ValidationError,
    ValidationInfo,
    field_validator,
)

from .errors import CurrencyMismatch, RemitError

DIGITS = 38
"""The most significant digits an amount may have, as in a SQL `numeric(38)` column."""

_EXACT = Context(prec=2 * DIGITS, traps=[InvalidOperation, DivisionByZero, Overflow, Inexact])
"""Arithmetic that raises rather than round. Wide enough that the product of any two
amounts is exact."""

_MINOR_UNITS: dict[str, int] = {
    currency.code: currency.exponent
    for currency in iso4217.Currency
    if currency.exponent is not None
}

type Rounding = Literal[
    "ROUND_CEILING",
    "ROUND_DOWN",
    "ROUND_FLOOR",
    "ROUND_HALF_DOWN",
    "ROUND_HALF_EVEN",
    "ROUND_HALF_UP",
    "ROUND_UP",
    "ROUND_05UP",
]
"""A `decimal` rounding mode. Banker's rounding, ROUND_HALF_EVEN, is the default."""


def minor_units(currency: str) -> int:
    """Decimal places the currency is denominated in: 2 for GBP, 0 for JPY, 3 for KWD."""
    try:
        return _MINOR_UNITS[currency]
    except KeyError:
        raise RemitError(f"{currency!r} is not a currency amounts can be held in") from None


def _held(code: str) -> str:
    if code not in _MINOR_UNITS:
        raise ValueError(
            f"{code!r} is not an ISO 4217 currency in current use with a minor unit "
            "(for example GBP, EUR, USD, JPY)"
        )
    return code


Currency = Annotated[str, AfterValidator(_held)]
"""An ISO 4217 code for a currency in current use, such as GBP. Not lower case."""


class Money(BaseModel):
    model_config = ConfigDict(frozen=True, use_attribute_docstrings=True)

    currency: Currency
    amount: Decimal
    """Exactly as many decimal places as the currency has minor units: GBP 1 is "1.00"."""

    @field_validator("amount", mode="before")
    @classmethod
    def _never_a_float(cls, value: object) -> object:
        if isinstance(value, float):
            raise ValueError("give amounts as strings, integers or Decimals; floats are inexact")
        return value

    @field_validator("amount")
    @classmethod
    def _fits_currency(cls, amount: Decimal, info: ValidationInfo) -> Decimal:
        currency = info.data.get("currency")
        if currency is None:  # the currency was refused; that error is the one to report
            return amount
        places = minor_units(currency)
        try:
            exact = amount.quantize(_minor_unit(currency), context=_EXACT)
        except Inexact:
            raise ValueError(f"{currency} amounts have at most {places} decimal places") from None
        except InvalidOperation:
            exact = None
        if exact is None or len(exact.as_tuple().digits) > DIGITS:
            raise ValueError(f"an amount has at most {DIGITS} digits")
        # One canonical form, so that equal amounts serialise identically: "1" becomes
        # "1.00", and a negative zero is zero.
        return exact.copy_abs() if exact.is_zero() else exact

    @classmethod
    def of(cls, amount: Decimal | int | str, currency: str) -> Self:
        return cls.model_validate({"amount": amount, "currency": currency})

    @classmethod
    def zero(cls, currency: str) -> Self:
        return cls.of(0, currency)

    @classmethod
    def total(cls, amounts: Iterable[Money], currency: str) -> Money:
        """Sum amounts that must all be in `currency`. Zero when there are none."""
        result: Money = cls.zero(currency)
        for amount in amounts:
            result += amount
        return result

    @property
    def sign(self) -> int:
        return (self.amount > 0) - (self.amount < 0)

    @property
    def is_zero(self) -> bool:
        return self.amount.is_zero()

    def times(self, factor: Decimal | int, *, rounding: Rounding = "ROUND_HALF_EVEN") -> Money:
        """This amount multiplied by `factor`, rounded to the currency's minor unit."""
        return _rounded(self.amount, _factor(factor), self.currency, rounding)

    def convert(self, to: str, *, rate: Decimal, rounding: Rounding = "ROUND_HALF_EVEN") -> Money:
        """Convert at `rate`, expressed as units of `to` per unit of this currency."""
        return _rounded(self.amount, _factor(rate), to, rounding)

    def allocate(self, weights: Sequence[Decimal | int]) -> tuple[Money, ...]:
        """Split this amount in proportion to `weights`, losing nothing to rounding.

        The parts always sum to exactly this amount. Each part is rounded down to the
        minor unit, then the minor units left over go one each to the parts that lost
        most in rounding (the largest-remainder method), earlier parts first on a tie.
        Splitting GBP 100.00 three ways gives 33.34, 33.33 and 33.33.
        """
        exact = [Fraction(_factor(weight)) for weight in weights]
        if not exact or any(weight < 0 for weight in exact) or sum(exact) == 0:
            raise RemitError("allocate needs weights that are not negative and not all zero")
        unit = _minor_unit(self.currency)
        units = int(self.amount.copy_abs().scaleb(minor_units(self.currency)))
        shares = [units * weight / sum(exact) for weight in exact]
        parts = [int(share) for share in shares]  # Fractions floor exactly
        by_remainder = sorted(range(len(parts)), key=lambda i: (parts[i] - shares[i], i))
        for index in by_remainder[: units - sum(parts)]:
            parts[index] += 1
        sign = -1 if self.sign < 0 else 1
        return tuple(_held_money(sign * part * unit, self.currency) for part in parts)

    def __add__(self, other: Money) -> Money:
        return _held_money(_EXACT.add(self.amount, self._comparable(other)), self.currency)

    def __sub__(self, other: Money) -> Money:
        return _held_money(_EXACT.subtract(self.amount, self._comparable(other)), self.currency)

    def __neg__(self) -> Money:
        return _held_money(self.amount.copy_negate(), self.currency)

    def __abs__(self) -> Money:
        return _held_money(self.amount.copy_abs(), self.currency)

    def __lt__(self, other: Money) -> bool:
        return self.amount < self._comparable(other)

    def __le__(self, other: Money) -> bool:
        return self.amount <= self._comparable(other)

    def __gt__(self, other: Money) -> bool:
        return self.amount > self._comparable(other)

    def __ge__(self, other: Money) -> bool:
        return self.amount >= self._comparable(other)

    def __str__(self) -> str:
        return f"{self.currency} {self.amount}"

    def _comparable(self, other: Money) -> Decimal:
        if other.currency != self.currency:
            raise CurrencyMismatch(f"cannot combine {self.currency} with {other.currency}")
        return other.amount


def _minor_unit(currency: str) -> Decimal:
    """The smallest amount of the currency: 0.01 for GBP, 1 for JPY."""
    return Decimal(1).scaleb(-minor_units(currency))


def _held_money(amount: Decimal, currency: str) -> Money:
    """The result of arithmetic, refused with a `RemitError` if no amount can hold it."""
    try:
        return Money(amount=amount, currency=currency)
    except ValidationError as error:
        problem = error.errors()[0]["msg"].removeprefix("Value error, ")
        raise RemitError(f"the result cannot be held as {currency}: {problem}") from None


def _rounded(amount: Decimal, factor: Decimal, currency: str, rounding: Rounding) -> Money:
    # Rounding is inexact by design, so this context traps everything but that.
    rounder = Context(prec=_EXACT.prec, rounding=rounding, traps=[InvalidOperation, Overflow])
    try:
        product = _EXACT.multiply(amount, factor)
        rounded = product.quantize(_minor_unit(currency), context=rounder)
    except DecimalException:
        raise RemitError(f"the result has more than {DIGITS} digits") from None
    return _held_money(rounded, currency)


def _factor(value: Decimal | int) -> Decimal:
    if isinstance(value, float):
        raise RemitError("give factors and rates as Decimals or integers; floats are inexact")
    factor = Decimal(value)
    if not factor.is_finite():
        raise RemitError("a factor or rate must be a finite number")
    return factor
