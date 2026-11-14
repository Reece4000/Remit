"""The context: one unit of work, handed to every handler.

Everything a handler reads, writes or emits goes through it. Reads come from the unit's
transaction; writes are held here until the kernel commits them together, so a handler
that raises leaves nothing behind.
"""

from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any, Protocol, cast
from uuid import UUID

from pydantic import BaseModel

from .actor import Actor
from .codec import Codec
from .document import Document
from .errors import NotFound, Rejected, RemitError
from .message import Command, Event
from .registry import Registry
from .store import Unit

_RESERVED = frozenset({"id", "state", "version"})


class Reader(Protocol):
    """Read access to documents and settings. The kernel and the context both provide it."""

    def get[D: Document](self, kind: type[D], id: UUID) -> D: ...
    def find[D: Document](self, kind: type[D], **equals: object) -> list[D]: ...
    def settings[S: BaseModel](self, kind: type[S]) -> S: ...
    def sql(self, query: str, parameters: Sequence[object] = ()) -> list[tuple[Any, ...]]: ...


class Context:
    def __init__(
        self,
        *,
        actor: Actor,
        now: datetime,
        unit: Unit,
        registry: Registry,
        settings: Mapping[type[BaseModel], BaseModel],
    ) -> None:
        self.actor = actor
        self.now = now
        self._unit = unit
        self._registry = registry
        self._codec = Codec(registry)
        self._settings = settings
        self._written: dict[tuple[type[Document], UUID], Document] = {}
        self._read_versions: dict[tuple[type[Document], UUID], int] = {}
        self._emitted: list[Event] = []

    @property
    def written(self) -> tuple[Document, ...]:
        return tuple(self._written.values())

    @property
    def emitted(self) -> tuple[Event, ...]:
        return tuple(self._emitted)

    def get[D: Document](self, kind: type[D], id: UUID) -> D:
        if written := self._written.get((kind, id)):
            return cast(D, written)
        name = self._registry.document_name(kind)
        body = self._unit.get(name, id)
        if body is None:
            raise NotFound(f"there is no {name} with id {id}")
        return self._codec.load(kind, body)

    def find[D: Document](self, kind: type[D], **equals: object) -> list[D]:
        """Documents whose fields equal the given values, oldest first."""
        stored = self._unit.find(self._registry.document_name(kind), self._codec.filters(equals))
        found = {document.id: document for document in (self._codec.load(kind, b) for b in stored)}
        for (written_kind, id), document in self._written.items():
            if written_kind is kind:
                if all(getattr(document, field) == value for field, value in equals.items()):
                    found[id] = cast(D, document)
                else:
                    found.pop(id, None)
        return list(found.values())

    def settings[S: BaseModel](self, kind: type[S]) -> S:
        return cast(S, self._settings[kind])

    def sql(self, query: str, parameters: Sequence[object] = ()) -> list[tuple[Any, ...]]:
        """Run a module's own SQL in this unit of work. It commits or rolls back with it."""
        return self._unit.sql(query, parameters)

    def create[D: Document](self, document: D) -> D:
        """Add a new document, in its lifecycle's initial state."""
        name = self._registry.document_name(type(document))
        if document.version or document.state != document.lifecycle.initial:
            raise Rejected(f"a new {name} must be unversioned and in its initial state")
        return self._write(document)

    def update[D: Document](self, document: D, **changes: object) -> D:
        """Change fields without changing state. Final documents cannot be updated."""
        self._require_current(document)
        if document.is_final:
            name = self._registry.document_name(type(document))
            raise Rejected(f"{name} {document.id} is {document.state} and can no longer change")
        return self._write(_revise(document, changes))

    def transition[D: Document](self, document: D, name: str, **changes: object) -> D:
        """Move a document along its lifecycle, optionally filling in fields as it goes."""
        self._require_current(document)
        step = document.lifecycle.find(name, document.state)
        revised = _revise(document, changes, state=step.target)
        missing = [field for field in step.requires if _blank(getattr(revised, field))]
        if missing:
            kind = self._registry.document_name(type(document))
            raise Rejected(f"cannot {name} {kind}: fill in {', '.join(missing)} first")
        return self._write(revised)

    def emit(self, event: Event) -> None:
        """Publish an event. It is recorded only if this unit of work commits."""
        self._registry.event_name(type(event))
        self._emitted.append(event)

    def execute[R](self, command: Command[R]) -> R:
        """Run another command inside this unit of work, on this command's authority."""
        return self._registry.command(type(command)).handler(command, self)

    def _require_current(self, document: Document) -> None:
        if self.get(type(document), document.id).version != document.version:
            raise RemitError(
                f"{type(document).__name__} {document.id} was changed earlier in this unit of "
                "work; use the copy that change returned"
            )

    def _write[D: Document](self, document: D) -> D:
        key = (type(document), document.id)
        read_version = self._read_versions.setdefault(key, document.version)
        written = document.model_copy(update={"version": read_version + 1})
        self._written[key] = written
        return written


def _revise[D: Document](document: D, changes: dict[str, object], **kernel_set: object) -> D:
    if reserved := _RESERVED & changes.keys():
        raise ValueError(f"{', '.join(sorted(reserved))} are managed by the kernel")
    return type(document).model_validate({**dict(document), **changes, **kernel_set})


def _blank(value: object) -> bool:
    return value is None or value == "" or (isinstance(value, (tuple, list)) and not value)
