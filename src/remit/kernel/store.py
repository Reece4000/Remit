"""Storage: what the kernel persists, and the contract a store must honour.

A store knows nothing about modules. It holds documents and events as JSON under their
registered names; the kernel does the typing. All work happens in a unit, which is one
serialisable transaction: whatever is read and written inside it behaves as though no
other unit were running. A store that cannot keep that promise for a unit raises
`ConcurrencyError`, and the kernel runs the work again from the start.

Events and audit entries are numbered in the order their units commit, and become
visible in that order: a reader that has seen position 10 will never later find a 9.
So anything that follows the log or the trail by position misses nothing.
"""

import threading
from collections import defaultdict
from collections.abc import Generator, Sequence
from contextlib import AbstractContextManager, contextmanager
from copy import deepcopy
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Any, Protocol
from uuid import UUID

from .actor import Actor
from .errors import ConcurrencyError, Rejected, RemitError
from .module import Module
from .selection import Json, Selection


@dataclass(frozen=True)
class StoredDocument:
    kind: str
    id: UUID
    version: int
    final: bool
    body: Json
    search: str | None = None
    """The folded text a listing's search looks in, when the document is searchable."""


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
    key: UUID | None = None
    """The data key personal values in `input` are sealed with, if it holds any."""
    position: int = 0
    """Where the entry sits in the trail, counting from 1. Zero until it is stored."""


@dataclass(frozen=True)
class SubjectKey:
    """A data key, and the data subjects whose personal values it seals."""

    id: UUID
    secret: bytes
    subjects: tuple[UUID, ...]


@dataclass(frozen=True)
class Commit:
    """Everything one unit of work changes. A store applies all of it or none of it."""

    documents: tuple[StoredDocument, ...]
    events: tuple[tuple[str, Json], ...]
    audit: AuditEntry
    cursor: tuple[str, int] | None = None
    idempotency: tuple[str, str, Json] | None = None
    """The actor, their idempotency key, and the record of what the command did."""
    keys: tuple[SubjectKey, ...] = ()
    erasures: tuple[UUID, ...] = ()
    """Data subjects whose keys are deleted, after `keys` are stored."""


class Unit(Protocol):
    def get(self, kind: str, id: UUID) -> Json | None: ...

    def select(self, selection: Selection) -> list[Json]:
        """The documents `selection.arranged` would pick from all those stored, in order."""
        ...

    def events(
        self, *, after: int = 0, name: str | None = None, limit: int | None = None
    ) -> list[StoredEvent]: ...

    def cursor(self, subscription: str) -> int: ...
    def idempotency(self, actor: str, key: str) -> Json | None:
        """The record an actor's idempotency key was used for. Keys are per actor."""
        ...

    def secret(self, key: UUID) -> bytes | None:
        """A data key's secret, or None once the data it sealed has been erased."""
        ...

    def audit(
        self, *, after: int = 0, document: tuple[str, UUID] | None = None, limit: int | None = None
    ) -> list[AuditEntry]:
        """Audit entries after a position, oldest first; only those that wrote `document`."""
        ...

    def commit(self, commit: Commit) -> None:
        """Apply the commit. Refused, changing nothing, if it breaks a rule the store keeps."""
        ...

    def rehearse(self, commit: Commit) -> None:
        """Refuse the commit for every reason `commit` would, then undo everything."""
        ...

    def sql(self, query: str, parameters: Sequence[object] = ()) -> list[tuple[Any, ...]]:
        """Run a module's own SQL inside this unit's transaction."""
        ...

    def rollback(self) -> None:
        """Undo everything this unit has done so far."""
        ...


class Listener(Protocol):
    def wait(self, timeout: float) -> bool:
        """Wait until a unit commits. True if one has since the last wait, or since listening
        began; False if none has by the timeout. It may wake for nothing, never miss one."""
        ...


class Store(Protocol):
    runs_sql: bool
    """Whether units can run a module's own SQL. Modules that ship SQL need this."""

    def unit(self, *, read_only: bool = False) -> AbstractContextManager[Unit]:
        """A unit of work. A read-only one refuses every change, including through SQL."""
        ...

    def listen(self) -> AbstractContextManager[Listener]:
        """Be told when units commit, so that a feed can wait instead of asking repeatedly."""
        ...

    def missing(self, modules: Sequence[Module]) -> tuple[str, ...]:
        """Why this store cannot yet run these modules. Empty when it can."""
        ...


class MemoryStore:
    """A store for tests and local runs. Units run one at a time, which is serialisable."""

    runs_sql = False

    def missing(self, modules: Sequence[Module]) -> tuple[str, ...]:
        """Nothing: a memory store needs no installation."""
        return ()

    def __init__(self) -> None:
        self._documents: defaultdict[str, dict[UUID, StoredDocument]] = defaultdict(dict)
        self._events: list[StoredEvent] = []
        self._audit: list[AuditEntry] = []
        self._cursors: dict[str, int] = {}
        self._idempotency: dict[tuple[str, str], Json] = {}
        self._secrets: dict[UUID, bytes] = {}
        self._subjects: dict[UUID, set[UUID]] = {}
        self._lock = threading.RLock()
        self._commits = _Commits()

    @contextmanager
    def unit(self, *, read_only: bool = False) -> Generator[Unit]:
        """Changes reach this store only through `commit`, which readers never call."""
        with self._lock:
            yield self

    def get(self, kind: str, id: UUID) -> Json | None:
        document = self._documents[kind].get(id)
        return deepcopy(document.body) if document else None

    def select(self, selection: Selection) -> list[Json]:
        stored = (document.body for document in self._documents[selection.kind].values())
        return deepcopy(selection.arranged(stored))

    def events(
        self, *, after: int = 0, name: str | None = None, limit: int | None = None
    ) -> list[StoredEvent]:
        found = [event for event in self._events[after:] if name is None or event.name == name]
        return found[:limit]

    def cursor(self, subscription: str) -> int:
        return self._cursors.get(subscription, 0)

    def idempotency(self, actor: str, key: str) -> Json | None:
        return deepcopy(self._idempotency.get((actor, key)))

    def audit(
        self, *, after: int = 0, document: tuple[str, UUID] | None = None, limit: int | None = None
    ) -> list[AuditEntry]:
        found = [
            entry
            for entry in self._audit[after:]
            if document is None or any(written[:2] == document for written in entry.documents)
        ]
        return found[:limit]

    @contextmanager
    def listen(self) -> Generator[Listener]:
        yield _MemoryListener(self._commits)

    def secret(self, key: UUID) -> bytes | None:
        return self._secrets.get(key)

    def rehearse(self, commit: Commit) -> None:
        self._check(commit)

    def _check(self, commit: Commit) -> None:
        for document in commit.documents:
            stored = self._documents[document.kind].get(document.id)
            if stored and stored.final:
                raise Rejected(f"{document.kind} {document.id} is final and cannot change")
            if (stored.version if stored else 0) != document.version - 1:
                raise ConcurrencyError(f"{document.kind} {document.id} changed since it was read")
        if commit.idempotency and commit.idempotency[:2] in self._idempotency:
            raise ConcurrencyError("the idempotency key was claimed since it was read")

    def commit(self, commit: Commit) -> None:
        self._check(commit)
        for document in commit.documents:
            self._documents[document.kind][document.id] = document
        for name, body in commit.events:
            position = len(self._events) + 1
            self._events.append(
                StoredEvent(position, name, body, commit.audit.at, commit.audit.actor.id)
            )
        self._audit.append(replace(commit.audit, position=len(self._audit) + 1))
        if commit.cursor:
            subscription, position = commit.cursor
            self._cursors[subscription] = position
        if commit.idempotency:
            actor, key, record = commit.idempotency
            self._idempotency[actor, key] = record
        for data_key in commit.keys:
            self._secrets[data_key.id] = data_key.secret
            self._subjects[data_key.id] = set(data_key.subjects)
        erased = set(commit.erasures)
        for key_id in [k for k, subjects in self._subjects.items() if subjects & erased]:
            del self._secrets[key_id], self._subjects[key_id]
        self._commits.announce()

    def sql(self, query: str, parameters: Sequence[object] = ()) -> list[tuple[Any, ...]]:
        raise RemitError("this needs a PostgreSQL store; the memory store cannot run SQL")

    def rollback(self) -> None:
        """Nothing to undo: writes only reach this store through `commit`."""


class _Commits:
    """A count of the commits to a memory store, which listeners wait on."""

    def __init__(self) -> None:
        self._changed = threading.Condition()
        self._count = 0

    def announce(self) -> None:
        with self._changed:
            self._count += 1
            self._changed.notify_all()

    def count(self) -> int:
        with self._changed:
            return self._count

    def after(self, seen: int, timeout: float) -> int:
        """The count, once it has moved on from `seen` or the timeout has passed."""
        with self._changed:
            self._changed.wait_for(lambda: self._count != seen, timeout)
            return self._count


class _MemoryListener:
    def __init__(self, commits: _Commits) -> None:
        self._commits = commits
        self._seen = commits.count()

    def wait(self, timeout: float) -> bool:
        count = self._commits.after(self._seen, timeout)
        committed, self._seen = count != self._seen, count
        return committed
