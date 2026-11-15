"""Commands and events: the two kinds of message, and what becomes of them.

A command asks for something to happen and may be refused. An event states that
something did happen and cannot be. `Command[R]` names the type its handler returns.
"""

from dataclasses import dataclass
from datetime import datetime

from pydantic import BaseModel, ConfigDict

from .document import Document


class Command[R](BaseModel):
    # Unknown fields are refused, not ignored: a caller who names a field that does not
    # exist (or cannot be changed) must be told, not left believing it took effect.
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
