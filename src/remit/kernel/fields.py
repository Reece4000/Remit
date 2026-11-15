"""Field types that many modules share, so that the same thing is validated the same way
everywhere it appears."""

from importlib.resources import files
from typing import Annotated

import pycountry
from pydantic import AfterValidator, StringConstraints

Code = Annotated[str, StringConstraints(pattern=r"^[A-Z0-9](?:[A-Z0-9._/-]{0,30}[A-Z0-9])?$")]
"""A short identifier that people read and type, such as an account, customer or item code.

Upper-case letters and digits, with `.`, `_`, `/` or `-` between them; at most 32
characters. A code identifies a record to people; its id identifies it to the system.
"""

Name = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
"""What a record is called. Never blank; surrounding whitespace is removed."""


def _assigned(code: str) -> str:
    if pycountry.countries.get(alpha_2=code) is None:
        raise ValueError(f"{code!r} is not an assigned ISO 3166-1 country code")
    return code


Country = Annotated[str, StringConstraints(pattern=r"^[A-Z]{2}$"), AfterValidator(_assigned)]
"""An assigned ISO 3166-1 alpha-2 country code, such as GB (not UK) or KE."""


def _load_units() -> dict[str, str]:
    lines = files(__package__).joinpath("data", "units.tsv").read_text(encoding="utf-8")
    return dict(
        line.split("\t", 1) for line in lines.splitlines() if line and not line.startswith("#")
    )


_UNITS = _load_units()


def unit_name(code: str) -> str:
    """What a unit code means, such as 'kilogram' for KGM."""
    return _UNITS[code]


def _known_unit(code: str) -> str:
    if code not in _UNITS:
        raise ValueError(
            f"{code!r} is not a UN/ECE Recommendation 20 or 21 unit code "
            "(for example EA each, KGM kilogram, HUR hour, XBX box)"
        )
    return code


UnitCode = Annotated[str, AfterValidator(_known_unit)]
"""A unit of measure, as a UN/ECE Recommendation 20 code, or a Recommendation 21 package
code prefixed with X: the codes e-invoicing (Peppol, EN 16931) accepts. The list is
vendored in `data/units.tsv`.
"""
