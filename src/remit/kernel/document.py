"""Documents: the records a business keeps, each with a declared lifecycle.

A document is immutable in memory; handlers change one by asking the context for a
revised copy. A document in a final state (one with no way out) can never change again,
which is what makes a posted journal entry or an issued invoice trustworthy.
"""

from dataclasses import dataclass
from typing import Any, ClassVar, cast
from uuid import UUID, uuid7

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .errors import Rejected


@dataclass(frozen=True)
class Transition:
    name: str
    source: str
    target: str
    requires: tuple[str, ...] = ()
    """Fields that must be filled in before this transition is allowed."""


@dataclass(frozen=True)
class Lifecycle:
    initial: str
    transitions: tuple[Transition, ...] = ()

    @property
    def states(self) -> tuple[str, ...]:
        named = (state for t in self.transitions for state in (t.source, t.target))
        return tuple(dict.fromkeys((self.initial, *named)))

    @property
    def final(self) -> frozenset[str]:
        return frozenset(self.states) - {t.source for t in self.transitions}

    def find(self, name: str, state: str) -> Transition:
        for transition in self.transitions:
            if transition.name == name and transition.source == state:
                return transition
        allowed = sorted({t.name for t in self.transitions if t.source == state})
        hint = f"allowed here: {', '.join(allowed)}" if allowed else "this state is final"
        raise Rejected(f"cannot {name} from state {state!r}; {hint}")


class Document(BaseModel):
    # A docstring under a field describes it wherever the registry describes the
    # document: in the API's schema, on screen, and to agents.
    model_config = ConfigDict(frozen=True, use_attribute_docstrings=True)

    lifecycle: ClassVar[Lifecycle]
    unique: ClassVar[tuple[str | tuple[str, ...], ...]] = ()
    """Fields no two documents of this kind may share, whatever their state.

    A tuple of fields is unique together: `("issuer", "subject")` lets two documents share
    an issuer or a subject, but not both. A value left unset never collides. The kernel
    refuses a write that would duplicate one, and on PostgreSQL a unique index makes the
    database refuse it too.
    """
    searchable: ClassVar[tuple[str, ...]] = ()
    """Text fields a listing's `search` looks in, such as a code and a name.

    A search finds the documents whose searchable text holds every word of it, whatever
    the case or accents. See `remit.kernel.search`.
    """

    id: UUID = Field(default_factory=uuid7)
    """Identifies the document to the system, for good."""
    state: str = ""
    """Where the document is in its lifecycle."""
    version: int = 0
    """How many times the document has been written. Changes that name the version they
    were read at are refused once it has moved on."""

    @model_validator(mode="before")
    @classmethod
    def _start_in_initial_state(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        fields = cast(dict[str, Any], data)
        return fields if fields.get("state") else {**fields, "state": cls.lifecycle.initial}

    @field_validator("state")
    @classmethod
    def _state_is_declared(cls, state: str) -> str:
        if state not in cls.lifecycle.states:
            raise ValueError(f"{state!r} is not a state of {cls.__name__}")
        return state

    @property
    def is_final(self) -> bool:
        return self.state in self.lifecycle.final

    @classmethod
    def unique_together(cls) -> tuple[tuple[str, ...], ...]:
        """Each of `unique` as the fields that are unique together."""
        return tuple((each,) if isinstance(each, str) else each for each in cls.unique)
