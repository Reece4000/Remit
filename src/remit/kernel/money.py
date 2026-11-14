"""Money: an exact amount in one currency.

Amounts never round silently and never mix currencies. Anything that can lose precision
(`times`, `convert`) names its rounding mode.
"""

from collections.abc import Iterable
from decimal import ROUND_HALF_EVEN, Decimal, InvalidOperation
from typing import Annotated, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    StringConstraints,
    field_validator,
    model_validator,
)

from .errors import CurrencyMismatch

Currency = Annotated[str, StringConstraints(pattern=r"^[A-Z]{3}$")]
"""An ISO 4217 code."""

_MINOR_UNITS = {
    **dict.fromkeys(
        ("BIF", "CLP", "DJF", "GNF", "ISK", "JPY", "KMF", "KRW", "PYG",
         "RWF", "UGX", "UYI", "VND", "VUV", "XAF", "XOF", "XPF"),
        0,
    ),
    **dict.fromkeys(("BHD", "IQD", "JOD", "KWD", "LYD", "OMR", "TND"), 3),
}  # fmt: skip


def minor_units(currency: str) -> int:
    """Decimal places the currency is denominated in."""
    return _MINOR_UNITS.get(currency, 2)


class Money(BaseModel):
    model_config = ConfigDict(frozen=True)

    amount: Decimal
    currency: Currency

    @field_validator("amount", mode="before")
    @classmethod
    def _never_a_float(cls, value: object) -> object:
        if isinstance(value, float):
            raise ValueError("give amounts as strings or Decimals; floats are inexact")
        return value

    @model_validator(mode="after")
    def _fits_currency(self) -> Self:
        places = minor_units(self.currency)
        try:
            exact = self.amount.quantize(Decimal(1).scaleb(-places))
        except InvalidOperation:
            raise ValueError("amount is not a representable number") from None
        if exact != self.amount:
            raise ValueError(f"{self.currency} amounts have at most {places} decimal places")
        # Store one canonical form so equal amounts serialise identically ("1" -> "1.00").
        object.__setattr__(self, "amount", abs(exact) if exact.is_zero() else exact)
        return self

    @classmethod
    def of(cls, amount: Decimal | int | str, currency: str) -> Self:
        return cls(amount=Decimal(amount), currency=currency)

    @classmethod
    def zero(cls, currency: str) -> Self:
        return cls.of(0, currency)

    @classmethod
    def total(cls, amounts: Iterable[Money], currency: str) -> Money:
        """Sum amounts that must all be in `currency`."""
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

    def times(self, factor: Decimal | int, *, rounding: str = ROUND_HALF_EVEN) -> Money:
        return self._rounded(self.amount * factor, self.currency, rounding)

    def convert(self, to: str, *, rate: Decimal, rounding: str = ROUND_HALF_EVEN) -> Money:
        """Convert at `rate`, expressed as units of `to` per unit of this currency."""
        return self._rounded(self.amount * rate, to, rounding)

    def __add__(self, other: Money) -> Money:
        return Money(amount=self.amount + self._comparable(other), currency=self.currency)

    def __sub__(self, other: Money) -> Money:
        return Money(amount=self.amount - self._comparable(other), currency=self.currency)

    def __neg__(self) -> Money:
        return Money(amount=-self.amount, currency=self.currency)

    def __abs__(self) -> Money:
        return Money(amount=abs(self.amount), currency=self.currency)

    def __str__(self) -> str:
        return f"{self.currency} {self.amount}"

    def _comparable(self, other: Money) -> Decimal:
        if other.currency != self.currency:
            raise CurrencyMismatch(f"cannot combine {self.currency} with {other.currency}")
        return other.amount

    @staticmethod
    def _rounded(amount: Decimal, currency: str, rounding: str) -> Money:
        quantum = Decimal(1).scaleb(-minor_units(currency))
        return Money(amount=amount.quantize(quantum, rounding=rounding), currency=currency)
