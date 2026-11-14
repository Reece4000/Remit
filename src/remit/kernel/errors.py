"""Errors the kernel raises.

`Rejected` is the one callers handle. Its message is written to be acted on by whoever
issued the command, whether that is a person or an agent deciding what to try next.
"""


class RemitError(Exception):
    """Base class for every error the framework raises."""


class RegistrationError(RemitError):
    """A module is declared incorrectly. Raised at startup, never while serving."""


class Unauthorised(RemitError):
    """The actor does not hold the permission the command requires."""


class Rejected(RemitError):
    """A rule refused the command. Nothing was changed."""


class NotFound(Rejected):
    """The command referred to a document that does not exist."""


class ConcurrencyError(RemitError):
    """A document changed after it was read. Read it again and retry."""


class CurrencyMismatch(RemitError):
    """Two amounts in different currencies were combined without a conversion."""
