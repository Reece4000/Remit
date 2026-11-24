"""The kernel: the only way anything changes.

`execute` runs a command for an actor: authorise, run the handler in a unit of work,
commit the documents, events and audit entry together. `dispatch` delivers recorded
events to subscriptions, each delivery its own unit of work.

Every unit is a serialisable transaction, so the checks a handler makes still hold when
it commits, whatever else is running. When the store cannot serialise a unit, the kernel
runs it again; handlers only touch the context, which makes that safe.

Reads (`get`, `page`, `ask`, `changes`, `history`, `events`, `audit`) name the actor
who is reading, and are refused unless that actor holds the permission to read what is
asked for. They run in read-only units: nothing can be changed through them, and on
PostgreSQL they wait for a snapshot that can never conflict, so they are never aborted.
"""

import random
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from functools import cache, partial
from typing import Any, cast
from uuid import UUID

from pydantic import BaseModel, ValidationError

from .actor import Actor, ActorKind
from .codec import Codec, unstorable
from .context import Context, Reader
from .document import Document
from .errors import (
    ConcurrencyError,
    NotFound,
    Problem,
    RegistrationError,
    Rejected,
    RemitError,
    Unauthorised,
    invalid,
)
from .message import Change, Changes, Command, Outcome, Query, Recorded
from .module import CommandSpec, Module, Subscription, read_permission
from .personal import DataKey, carries_personal, seal, unseal
from .registry import READ_AUDIT, READ_EVENTS, Registry
from .selection import DEFAULT_LIMIT, MAX_LIMIT, Page
from .store import AuditEntry, Commit, Json, MemoryStore, Store, SubjectKey, Unit


@dataclass(frozen=True)
class DeliveryFailure:
    subscription: str
    position: int
    error: str


@dataclass(frozen=True)
class Dispatched:
    delivered: int
    failures: tuple[DeliveryFailure, ...]


class Kernel:
    def __init__(
        self,
        modules: Iterable[Module],
        *,
        settings: Iterable[BaseModel] = (),
        store: Store | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        attempts: int = 8,
    ) -> None:
        self.registry = Registry(modules)
        self.store: Store = store or MemoryStore()
        self._codec = Codec(self.registry)
        self._clock = clock
        self._attempts = attempts
        self._settings = {type(value): value for value in settings}
        for module in self.registry.modules.values():
            if module.settings and module.settings not in self._settings:
                raise RegistrationError(
                    f"module {module.name!r} needs {module.settings.__name__} settings"
                )
            if (module.migrations or module.sql) and not self.store.runs_sql:
                raise RegistrationError(f"module {module.name!r} needs a PostgreSQL store")
        if problems := self.store.missing(tuple(self.registry.modules.values())):
            raise RegistrationError(
                "; ".join(problems) + "; install the modules into the store before running them"
            )

    def execute[R](
        self,
        command: Command[R],
        *,
        actor: Actor,
        idempotency_key: str | None = None,
        dry_run: bool = False,
    ) -> Outcome[R]:
        """Run a command. With `dry_run`, report what it would do and change nothing."""
        spec = self.registry.command(type(command))
        _authorise(actor, spec.name)

        def work(unit: Unit) -> Outcome[R]:
            if idempotency_key and (stored := unit.idempotency(actor.id, idempotency_key)):
                seen = _opened(unit, stored)
                if seen is None:
                    raise Rejected(
                        f"the record of idempotency key {idempotency_key!r} was erased with the "
                        "personal data it held; send the command again with a new key"
                    )
                if seen["command"] != command.model_dump(mode="json"):
                    raise Rejected(
                        f"idempotency key {idempotency_key!r} was already used for another command"
                    )
                return cast(Outcome[R], self._codec.load_outcome(spec, seen))

            context = self._context(unit, actor, spec.module)
            result = spec.handler(command, context)
            context.finish()
            outcome = Outcome(result, context.written, context.emitted, committed=not dry_run)
            replay = (actor.id, idempotency_key, spec, outcome) if idempotency_key else None
            commit = self._commit(context, spec.name, command, replay=replay)
            # A dry run sends the commit too, and then undoes it, so that it is refused for
            # the same reasons as the real thing, including rules the database enforces.
            (unit.rehearse if dry_run else unit.commit)(commit)
            return outcome

        return self._in_unit(work)

    def command(self, name: str, input: Mapping[str, object]) -> Command[Any]:
        """The command registered as `name`, built from JSON input, as an API receives it.

        Refuses a name no installed module declares, and input the command does not
        accept, with a problem for each field at fault.
        """
        spec = self.registry.find_command(name)
        if spec is None:
            raise NotFound(f"there is no command {name!r}; the registry describes those installed")
        try:
            return spec.command.model_validate(input)
        except ValidationError as error:
            raise invalid(name, error) from None

    def dispatch(self) -> Dispatched:
        """Deliver recorded events to subscriptions, once each and in order.

        A subscription's writes commit together with its cursor, so its effects inside
        the store happen exactly once, however many workers are dispatching. One that
        fails keeps its place and is retried on the next call. Events emitted during a
        pass are delivered on the next one, so a worker calls this until `delivered` is
        zero.
        """
        delivered = 0
        failures: list[DeliveryFailure] = []
        for subscription in self.registry.subscriptions:
            while True:
                result = self._in_unit(partial(self._deliver, subscription))
                if isinstance(result, DeliveryFailure):
                    failures.append(result)
                if result is not True:
                    break
                delivered += 1
        return Dispatched(delivered, tuple(failures))

    def query(self, name: str, input: Mapping[str, object]) -> Query[Any]:
        """The query registered as `name`, built from JSON input, as an API receives it."""
        spec = self.registry.find_query(name)
        if spec is None:
            raise NotFound(f"there is no query {name!r}; the registry describes those installed")
        try:
            return spec.query.model_validate(input)
        except ValidationError as error:
            raise invalid(name, error) from None

    def get[D: Document](self, kind: type[D], id: UUID, *, actor: Actor) -> D:
        """A document by id, for an actor who may read documents of its kind."""
        _authorise(actor, self.registry.read_permission(kind))
        return self._read(lambda unit: self._reader(unit, actor).get(kind, id))

    def page[D: Document](
        self,
        kind: type[D],
        *,
        actor: Actor,
        where: Mapping[str, object] | None = None,
        search: str | None = None,
        order: str | None = None,
        after: str | None = None,
        limit: int = DEFAULT_LIMIT,
    ) -> Page[D]:
        """One page of the documents of a kind, for an actor who may read them.

        `where` filters, `search` looks in the kind's searchable fields, `order` names
        the field to sort by, and `after` takes the last page's `next`. See
        `remit.kernel.selection`.
        """
        _authorise(actor, self.registry.read_permission(kind))
        return self._read(
            lambda unit: self._reader(unit, actor).page(
                kind, where=where, search=search, order=order, after=after, limit=limit
            )
        )

    def ask[R](self, query: Query[R], *, actor: Actor) -> R:
        """Answer a query, for an actor who holds its permission."""
        spec = self.registry.query(type(query))
        _authorise(actor, spec.permission)
        return self._read(
            lambda unit: cast(R, spec.handler(query, self._reader(unit, actor, spec.module)))
        )

    def settings[S: BaseModel](self, kind: type[S]) -> S:
        return cast(S, self._settings[kind])

    def changes(self, *, actor: Actor, after: int = 0, limit: int = DEFAULT_LIMIT) -> Changes:
        """What has been written since `after`, in the order it was committed.

        Each change lists only the documents the actor may read, and a change that wrote
        none of those is left out. No permission is needed to follow the feed itself, so
        a screen can learn what to refresh without seeing anything it could not read.
        Looks at up to `limit` units of work at a time.
        """
        _limited(limit)
        return self._read(
            lambda unit: self._changes(actor, unit.audit(after=after, limit=limit), after)
        )

    def history[D: Document](
        self, kind: type[D], id: UUID, *, actor: Actor, after: int = 0, limit: int = DEFAULT_LIMIT
    ) -> Changes:
        """The changes that wrote one document, oldest first, for an actor who may read it."""
        _authorise(actor, self.registry.read_permission(kind))
        _limited(limit)
        wrote = (self.registry.document_name(kind), id)
        return self._read(
            lambda unit: self._changes(
                actor, unit.audit(after=after, document=wrote, limit=limit), after
            )
        )

    def events(self, *, actor: Actor, after: int = 0, limit: int | None = None) -> list[Recorded]:
        """The event log after a position, in the order events were committed."""
        _authorise(actor, READ_EVENTS)
        stored = self._read(lambda unit: unit.events(after=after, limit=limit))
        return [self._codec.load_event(event) for event in stored]

    def audit[D: Document](
        self,
        *,
        actor: Actor,
        after: int = 0,
        document: tuple[type[D], UUID] | None = None,
        limit: int | None = None,
    ) -> list[AuditEntry]:
        """The commands and deliveries that committed after a position, oldest first.

        With `document`, only those that wrote it. Personal values read as they were
        given, or as `ERASED` once their subject has been erased.
        """
        _authorise(actor, READ_AUDIT)
        wrote = (self.registry.document_name(document[0]), document[1]) if document else None

        def read(unit: Unit) -> list[AuditEntry]:
            secret = cache(unit.secret)
            return [
                replace(entry, input=unseal(entry.input, secret, entry.key))
                for entry in unit.audit(after=after, document=wrote, limit=limit)
            ]

        return self._read(read)

    def _changes(self, actor: Actor, entries: list[AuditEntry], after: int) -> Changes:
        @cache
        def readable(kind: str) -> bool:
            return actor.may(read_permission(kind))

        changes = (
            Change(
                entry.position,
                entry.at,
                entry.actor.id,
                entry.action,
                tuple(written for written in entry.documents if readable(written[0])),
            )
            for entry in entries
        )
        return Changes(
            tuple(change for change in changes if change.documents),
            entries[-1].position if entries else after,
        )

    def _deliver(self, subscription: Subscription, unit: Unit) -> DeliveryFailure | bool:
        """Deliver the subscription's next event. False when it has none waiting."""
        name = self.registry.event_name(subscription.event)
        waiting = unit.events(after=unit.cursor(subscription.name), name=name, limit=1)
        if not waiting:
            return False
        recorded = self._codec.load_event(waiting[0])
        actor = Actor(id=f"system:{subscription.module}", kind=ActorKind.SYSTEM)
        context = self._context(unit, actor, subscription.module)
        try:
            subscription.handler(recorded.event, context)
            context.finish()
            unit.commit(
                self._commit(
                    context,
                    subscription.name,
                    recorded.event,
                    cause=recorded.position,
                    cursor=(subscription.name, recorded.position),
                )
            )
        except ConcurrencyError:
            raise
        except Exception as error:
            # Whatever went wrong, in the handler or in committing what it did, went wrong
            # in this subscription alone: it keeps its place, and every other subscription
            # is still delivered to.
            unit.rollback()
            reason = str(error) if isinstance(error, RemitError) else _described(error)
            return DeliveryFailure(subscription.name, recorded.position, reason)
        return True

    def _read[T](self, work: Callable[[Unit], T]) -> T:
        return self._in_unit(work, read_only=True)

    def _in_unit[T](self, work: Callable[[Unit], T], *, read_only: bool = False) -> T:
        for attempt in range(1, self._attempts + 1):
            try:
                with self.store.unit(read_only=read_only) as unit:
                    return work(unit)
            except ConcurrencyError:
                if attempt == self._attempts:
                    raise
                time.sleep(random.uniform(0, 0.002 * 2**attempt))  # noqa: S311  jitter, not secrecy
        raise AssertionError("unreachable")

    def _reader(self, unit: Unit, actor: Actor, module: str = "") -> Reader:
        """A reader for an actor, or for a query of `module` asked on the actor's behalf."""
        return Reader(
            actor=actor,
            now=self._clock(),
            unit=unit,
            registry=self.registry,
            settings=self._settings,
            module=module,
        )

    def _context(self, unit: Unit, actor: Actor, module: str) -> Context:
        """A context for code of `module`."""
        return Context(
            actor=actor,
            now=self._clock(),
            unit=unit,
            registry=self.registry,
            settings=self._settings,
            module=module,
        )

    def _commit(
        self,
        context: Context,
        action: str,
        input: BaseModel,
        *,
        cause: int | None = None,
        cursor: tuple[str, int] | None = None,
        replay: tuple[str, str, CommandSpec, Outcome[Any]] | None = None,
    ) -> Commit:
        """Everything the unit changes, with personal values sealed under a new data key.

        Refused if any of it holds text no store can keep, so that every store refuses
        the same things.
        """
        data_key = DataKey()
        sealed_input = seal(input, input.model_dump(mode="json"), data_key)
        idempotency: tuple[str, str, Json] | None = None
        if replay:
            actor, key, spec, outcome = replay
            record = self._codec.dump_outcome(spec, cast(Command[Any], input), outcome, data_key)
            sealed = {**record, "key": str(data_key.id)} if data_key.used else record
            idempotency = (actor, key, sealed)

        keys: tuple[SubjectKey, ...] = ()
        if data_key.used:
            # A subject erased in this unit holds nothing personal any more, so the new key
            # is not linked to it: if it were, the erasure would delete the key of its own
            # record, and a retry of the erasure could never be answered.
            erased = set(context.erasures)
            subjects = tuple(
                d.id for d in context.written if carries_personal(type(d)) and d.id not in erased
            )
            if not subjects:
                raise RemitError(
                    f"{action} carries personal data but writes no document it belongs to, "
                    "so it could never be erased; personal values belong in data subjects"
                )
            keys = (SubjectKey(data_key.id, data_key.secret, subjects),)

        documents = tuple(self._codec.dump(document) for document in context.written)
        events = tuple(self._codec.dump_event(event) for event in context.emitted)
        kept = [
            ("input", sealed_input),
            *((d.kind, d.body) for d in documents),
            *events,
            *([("result", idempotency[2]["result"])] if idempotency else []),
        ]
        if problem := next(filter(None, (unstorable(body, where) for where, body in kept)), None):
            raise Rejected(f"{action}: {problem}")
        audit = AuditEntry(
            at=context.now,
            actor=context.actor,
            action=action,
            input=sealed_input,
            documents=tuple((d.kind, d.id, d.version) for d in documents),
            events=tuple(name for name, _ in events),
            cause=cause,
            key=data_key.id if data_key.used else None,
        )
        return Commit(
            documents,
            events,
            audit,
            cursor=cursor,
            idempotency=idempotency,
            keys=keys,
            erasures=context.erasures,
        )


def _limited(limit: int) -> None:
    if not 1 <= limit <= MAX_LIMIT:
        said = f"must be between 1 and {MAX_LIMIT}"
        raise Rejected(f"limit {said}", problems=(Problem(("limit",), said),))


def _authorise(actor: Actor, permission: str) -> None:
    if not actor.may(permission):
        raise Unauthorised(f"{actor.id} does not hold the permission {permission}")


def _described(error: Exception) -> str:
    return f"{type(error).__name__}: {error}" if str(error) else type(error).__name__


def _opened(unit: Unit, record: Json) -> Json | None:
    """An idempotency record with its personal values opened; None if they were erased."""
    if "key" not in record:
        return record
    key = UUID(record["key"])
    if unit.secret(key) is None:
        return None
    return cast(Json, unseal(record, unit.secret, key))
