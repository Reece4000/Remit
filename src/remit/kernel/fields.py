"""Field types that many modules share, so that the same thing is validated the same way
everywhere it appears."""

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
