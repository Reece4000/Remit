"""What the API sends and receives, beyond the modules' own commands and documents.

Each is a pydantic model so that the OpenAPI document describes it exactly, and a client
generated from that document gets real types.
"""

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from remit.kernel import ActorKind, AuditEntry, Change, Changes, Recorded


class _Sent(BaseModel):
    model_config = ConfigDict(frozen=True)


class Written(_Sent):
    """A document as a unit of work wrote it."""

    kind: str
    """As the registry names it, such as `parties.party`."""
    id: UUID
    version: int


class Executed[R](_Sent):
    """What a command did, or would do if it was a dry run."""

    result: R
    documents: list[Written]
    """Every document the command wrote. Read them for their contents."""
    events: list[str]
    """The names of the events it emitted, in order."""
    committed: bool
    """False for a dry run, which changes nothing."""
    replayed: bool
    """True when an idempotency key had been used for this command already, so this is
    what it did then, and it did not run again."""


class Listed[D](_Sent):
    """One page of a listing."""

    items: list[D]
    next: str | None
    """Pass as `after` for the next page. Null on the last page."""


class ChangeSent(_Sent):
    """One unit of work that wrote documents the reader may read."""

    position: int
    at: datetime
    actor: str
    action: str
    documents: list[Written]

    @classmethod
    def of(cls, change: Change) -> ChangeSent:
        return cls(
            position=change.position,
            at=change.at,
            actor=change.actor,
            action=change.action,
            documents=[Written(kind=k, id=i, version=v) for k, i, v in change.documents],
        )


class ChangesSent(_Sent):
    """A stretch of the changes feed. Pass `position` as `after` to read on."""

    items: list[ChangeSent]
    position: int

    @classmethod
    def of(cls, changes: Changes) -> ChangesSent:
        return cls(items=[ChangeSent.of(c) for c in changes.items], position=changes.position)


class ActorSent(_Sent):
    id: str
    kind: ActorKind
    permissions: list[str]


class AuditEntrySent(_Sent):
    """A command or delivery that committed, with what it was sent."""

    position: int
    at: datetime
    actor: ActorSent
    action: str
    input: dict[str, Any]
    documents: list[Written]
    events: list[str]
    cause: int | None
    """The event log position that set off a delivery."""

    @classmethod
    def of(cls, entry: AuditEntry) -> AuditEntrySent:
        return cls(
            position=entry.position,
            at=entry.at,
            actor=ActorSent(
                id=entry.actor.id,
                kind=entry.actor.kind,
                permissions=sorted(entry.actor.permissions),
            ),
            action=entry.action,
            input=entry.input,
            documents=[Written(kind=k, id=i, version=v) for k, i, v in entry.documents],
            events=list(entry.events),
            cause=entry.cause,
        )


class AuditSent(_Sent):
    """A stretch of the audit trail. Pass `position` as `after` to read on."""

    items: list[AuditEntrySent]
    position: int


class EventSent(_Sent):
    position: int
    name: str
    event: dict[str, Any]
    at: datetime
    actor: str

    @classmethod
    def of(cls, recorded: Recorded) -> EventSent:
        return cls(
            position=recorded.position,
            name=recorded.name,
            event=recorded.event.model_dump(mode="json"),
            at=recorded.at,
            actor=recorded.actor,
        )


class EventsSent(_Sent):
    """A stretch of the event log. Pass `position` as `after` to read on."""

    items: list[EventSent]
    position: int


class Me(_Sent):
    """Who the request was made by, and what they may do."""

    id: str
    kind: ActorKind
    permissions: list[str]
    """As their roles grant them: names and patterns."""
    allowed: list[str]
    """Every permission of what is installed that they hold: commands by name, reads as
    `<module>.read.<name>`. A screen shows only what is in this list."""


class FieldProblem(_Sent):
    path: list[str | int]
    """Where the field is: names and list positions, outermost first. Empty for the whole
    of what was sent."""
    message: str
    document: str | None = None
    """The document the path leads into, when it leads into one rather than the input."""


class ProblemSent(_Sent):
    """An error, as RFC 9457 problem details."""

    type: str
    """Identifies the kind of problem, such as `urn:remit:problem:rejected`."""
    title: str
    status: int
    detail: str
    """What went wrong, written to be acted on by a person or an agent."""
    problems: list[FieldProblem] = []
    """The fields at fault, when it can be told which they are."""
