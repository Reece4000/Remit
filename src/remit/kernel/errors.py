"""Errors the kernel raises.

`Rejected` is the one callers handle. Its message is written to be acted on by whoever
issued the command, whether that is a person or an agent deciding what to try next.
When the kernel can tell which fields were at fault, it lists them in `problems` too, so
that a form can show each one beside its field.
"""

from collections.abc import Iterable
from dataclasses import dataclass

from pydantic import ValidationError
from pydantic_core import ErrorDetails


class RemitError(Exception):
    """Base class for every error the framework raises."""


class RegistrationError(RemitError):
    """A module is declared incorrectly. Raised at startup, never while serving."""


class Unauthorised(RemitError):
    """The actor does not hold the permission that the command or the read requires."""


@dataclass(frozen=True)
class Problem:
    """One field that was at fault, and what was wrong with it."""

    path: tuple[str | int, ...]
    """Where the field is: field names and list positions, outermost first. Empty for
    the whole of what was sent."""
    message: str
    document: str | None = None
    """The registered name of the document `path` leads into, or None when it leads into
    the command itself. A command's fields often share names with its document's, but
    only the module knows when they are the same field."""


class Rejected(RemitError):
    """A rule refused the command. Nothing was changed."""

    def __init__(self, message: str, *, problems: Iterable[Problem] = ()) -> None:
        super().__init__(message)
        self.problems: tuple[Problem, ...] = tuple(problems)


class NotFound(Rejected):
    """The command referred to something that does not exist."""


class ConcurrencyError(RemitError):
    """A document changed after it was read. Read it again and retry."""


class CurrencyMismatch(RemitError):
    """Two amounts in different currencies were combined without a conversion."""


def invalid(subject: str, error: ValidationError, *, document: str | None = None) -> Rejected:
    """A validation error as a refusal: one line to act on, and a problem for each field."""
    problems = [Problem(tuple(found["loc"]), said(found), document) for found in error.errors()]
    described = "; ".join(f"{_dotted(p.path) or 'value'}: {p.message}" for p in problems)
    return Rejected(f"{subject}: {described}", problems=problems)


def said(found: ErrorDetails) -> str:
    """What was wrong, as pydantic words it, with whatever context it gave filled in.

    Pydantic prefixes what a validator's own `ValueError` says with "Value error, ",
    which reads as noise beside a field.
    """
    if found["type"] == "value_error":
        return found["msg"].removeprefix("Value error, ")
    return found["msg"]


def _dotted(path: tuple[str | int, ...]) -> str:
    return ".".join(map(str, path))
