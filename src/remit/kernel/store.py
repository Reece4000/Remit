"""Storage: what the kernel persists, and the contract a store must honour.

A store knows nothing about modules. It holds documents and events as JSON under their
registered names; the kernel does the typing. All work happens in a unit, which is one
serialisable transaction: whatever is read and written inside it behaves as though no
other unit were running. A store that cannot keep that promise for a unit raises
`ConcurrencyError`, and the kernel runs the work again from the start.
"""

import threading
from collections import defaultdict
from collections.abc import Generator, Mapping, Sequence
from contextlib import AbstractContextManager, contextmanager
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol
from uuid import UUID

from .actor import Actor
from .errors import ConcurrencyError, RemitError

type Json = dict[str, Any]


@dataclass(frozen=True)
class StoredDocument:
    kind: str
    id: UUID
    version: int
    final: bool
    body: Json


@dataclass(frozen=True)
class StoredEvent:
    position: int
    name: str
    body: Json
    at: datetime
    actor: str


@dataclass(frozen=True)
class AuditEntry:
    at: datetime
    actor: Actor
    action: str
    """The command or subscription that ran."""
    input: Json
    documents: tuple[tuple[str, UUID, int], ...]
    events: tuple[str, ...]
    cause: int | None = None
    """Log position of the event that triggered this, when a subscription ran."""


@dataclass(frozen=True)
class Commit:
    """Everything one unit of work changes. A store applies all of it or none of it."""

    documents: tuple[StoredDocument, ...]
    events: tuple[tuple[str, Json], ...]
    audit: AuditEntry
    cursor: tuple[str, int] | None = None
    idempotency: tuple[str, Json] | None = None


class Unit(Protocol):
    def get(self, kind: str, id: UUID) -> Json | None: ...

    def find(self, kind: str, equals: Mapping[str, object]) -> list[Json]:
        """Documents whose top-level fields equal the given JSON scalars, oldest first."""
        ...

    def events(
        self, *, after: int = 0, name: str | None = None, limit: int | None = None
    ) -> list[StoredEvent]: ...

    def cursor(self, subscription: str) -> int: ...
    def idempotency(self, key: str) -> Json | None: ...
    def audit(self) -> list[AuditEntry]: ...
    def commit(self, commit: Commit) -> None: ...

    def sql(self, query: str, parameters: Sequence[object] = ()) -> list[tuple[Any, ...]]:
        """Run a module's own SQL inside this unit's transaction."""
        ...

    def rollback(self) -> None:
        """Undo everything this unit has done so far."""
        ...


class Store(Protocol):
    runs_sql: bool
    """Whether units can run a module's own SQL. Modules that ship SQL need this."""

    def unit(self) -> AbstractContextManager[Unit]: ...


class MemoryStore:
    """A store for tests and local runs. Units run one at a time, which is serialisable."""

    runs_sql = False

    def __init__(self) -> None:
        self._documents: defaultdict[str, dict[UUID, StoredDocument]] = defaultdict(dict)
        self._events: list[StoredEvent] = []
        self._audit: list[AuditEntry] = []
        self._cursors: dict[str, int] = {}
        self._idempotency: dict[str, Json] = {}
        self._lock = threading.RLock()

    @contextmanager
    def unit(self) -> Generator[Unit]:
        with self._lock:
            yield self

    def get(self, kind: str, id: UUID) -> Json | None:
        document = self._documents[kind].get(id)
        return deepcopy(document.body) if document else None

    def find(self, kind: str, equals: Mapping[str, object]) -> list[Json]:
        return [
            deepcopy(document.body)
            for document in self._documents[kind].values()
            if all(document.body.get(field) == value for field, value in equals.items())
        ]

    def events(
        self, *, after: int = 0, name: str | None = None, limit: int | None = None
    ) -> list[StoredEvent]:
        found = [event for event in self._events[after:] if name is None or event.name == name]
        return found[:limit]

    def cursor(self, subscription: str) -> int:
        return self._cursors.get(subscription, 0)

    def idempotency(self, key: str) -> Json | None:
        return deepcopy(self._idempotency.get(key))

    def audit(self) -> list[AuditEntry]:
        return list(self._audit)

    def commit(self, commit: Commit) -> None:
        for document in commit.documents:
            stored = self._documents[document.kind].get(document.id)
            if stored and stored.final:
                raise RemitError(f"{document.kind} {document.id} is final and cannot change")
            if (stored.version if stored else 0) != document.version - 1:
                raise ConcurrencyError(f"{document.kind} {document.id} changed since it was read")

        for document in commit.documents:
            self._documents[document.kind][document.id] = document
        for name, body in commit.events:
            position = len(self._events) + 1
            self._events.append(
                StoredEvent(position, name, body, commit.audit.at, commit.audit.actor.id)
            )
        self._audit.append(commit.audit)
        if commit.cursor:
            subscription, position = commit.cursor
            self._cursors[subscription] = position
        if commit.idempotency:
            key, record = commit.idempotency
            self._idempotency[key] = record

    def sql(self, query: str, parameters: Sequence[object] = ()) -> list[tuple[Any, ...]]:
        raise RemitError("this needs a PostgreSQL store; the memory store cannot run SQL")

    def rollback(self) -> None:
        """Nothing to undo: writes only reach this store through `commit`."""
