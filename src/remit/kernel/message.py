"""Commands, queries and events: the kinds of message, and what becomes of them.

A command asks for something to happen and may be refused. A query asks what is so and
changes nothing. An event states that something did happen and cannot be refused.
`Command[R]` names the type its handler returns.
"""

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from .document import Document


class Command[R](BaseModel):
    # Unknown fields are refused, not ignored: a caller who names a field that does not
    # exist (or cannot be changed) must be told, not left believing it took effect.
    model_config = ConfigDict(frozen=True, extra="forbid", use_attribute_docstrings=True)


class Query[R](BaseModel):
    """Asks for something to be read. `Query[R]` names the type its handler returns."""

    model_config = ConfigDict(frozen=True, extra="forbid", use_attribute_docstrings=True)


class Event(BaseModel):
    model_config = ConfigDict(frozen=True, use_attribute_docstrings=True)


@dataclass(frozen=True)
class Outcome[R]:
    """What a command did, or would do if it was a dry run."""

    result: R
    documents: tuple[Document, ...]
    events: tuple[Event, ...]
    committed: bool
    replayed: bool = False


@dataclass(frozen=True)
class Recorded:
    """An event as it sits in the log."""

    position: int
    name: str
    event: Event
    at: datetime
    actor: str


@dataclass(frozen=True)
class Change:
    """One unit of work that wrote documents, as anyone who may read them may see it.

    It names who acted and what they did, and the documents written that the reader may
    read, each at the version it was written as. What was sent is in the audit trail,
    for those who may read that.
    """

    position: int
    """Where this sits in the audit trail."""
    at: datetime
    actor: str
    action: str
    """The command or subscription that ran."""
    documents: tuple[tuple[str, UUID, int], ...]
    """(kind, id, version) for each document written."""


@dataclass(frozen=True)
class Changes:
    """A stretch of the changes feed.

    `position` is where the stretch ends: pass it as `after` to read on. It moves on even
    when nothing in the stretch was for this reader, so that reading on always advances.
    """

    items: tuple[Change, ...]
    position: int
