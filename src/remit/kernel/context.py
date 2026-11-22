"""The reader and the context: one unit of work, as handlers see it.

A query's handler is given a `Reader`, which reads documents and settings and has no
way to change anything. A command's or subscription's handler is given a `Context`, a
reader that also writes. Reads come from the unit's transaction; writes are held in the
context until the kernel commits them together, so a handler that raises leaves nothing
behind.

A module reads any document but writes only its own, and emits only its own events. To
change another module's documents it executes that module's commands, so the rules
those commands enforce (a ledger's balance, a code that never changes) cannot be gone
around. The context knows which module's code is running because it is told each time
control passes from one module to another: when the kernel runs a command, a query or a
subscription, and when a handler executes another module's command or asks its query.

A handler reads on the authority of whatever it is handling. The kernel checks an
actor's permission once, where the actor issues a command or asks to read; what the
handler then reads in order to answer is not checked again.

A command moves the documents its input refers to only through the transition it
declares (`Command.moves`), so that what the registry tells a screen about where a
command applies is true. The context knows which command's handler is running, and
holds each transition to that command's declaration alone, not to the declaration of
a command that executed it.

A document's stored body holds the fields of its installed extensions beside its own
(see `remit.kernel.extension`). Each module writes only the fields it declares: the
context keeps the extensions' fields as they are stored when the owning module writes
its document, and the document's own when a module writes its extension.
"""

from collections.abc import Generator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Any, cast
from uuid import UUID

from pydantic import BaseModel, ValidationError

from .actor import Actor
from .codec import Codec
from .document import Document
from .errors import NotFound, Problem, Rejected, RemitError, invalid
from .extension import Extension, added
from .message import Command, Event, Query
from .module import CommandSpec, Module
from .personal import carries_personal, holds_personal, unerased
from .reference import references
from .registry import Registry
from .selection import DEFAULT_LIMIT, Page, Selection, defaults, select
from .store import Json, StoredDocument, Unit

_RESERVED = frozenset({"id", "state", "version"})


@dataclass(frozen=True)
class _Acting:
    """A command whose handler is running, and the documents its input refers to."""

    spec: CommandSpec
    moved: str | None
    """The registered name of the document it may move, if it declares a move."""
    referred: frozenset[tuple[str, UUID]]
    """(kind, id) of each document its input refers to."""


class Reader:
    """Read access to one unit of work."""

    def __init__(
        self,
        *,
        actor: Actor,
        now: datetime,
        unit: Unit,
        registry: Registry,
        settings: Mapping[type[BaseModel], BaseModel],
        module: str,
    ) -> None:
        self.actor = actor
        self.now = now
        self._unit = unit
        self._registry = registry
        self._codec = Codec(registry)
        self._settings = settings
        self._modules = [module]
        self._written: dict[tuple[type[Document], UUID], Document] = {}
        self._extended: dict[tuple[type[Document], UUID], Json] = {}
        """What each document written holds of its extensions' fields, to be stored with it."""

    @property
    def module(self) -> str:
        """The module whose code is running now."""
        return self._modules[-1]

    def get[D: Document](self, kind: type[D], id: UUID, *, version: int | None = None) -> D:
        """A document by id. With `version`, refuse if it has changed since that version.

        Pass the version a command was written against, so that a change made by someone
        working from an out-of-date copy is refused rather than overwriting another's.
        """
        document = self._get(kind, id)
        if version is not None and document.version != version:
            name = self._registry.document_name(kind)
            raise Rejected(
                f"{name} {id} has changed since version {version} (it is now at version "
                f"{document.version}); read it again and decide whether your change still holds"
            )
        return document

    def _get[D: Document](self, kind: type[D], id: UUID) -> D:
        if written := self._written.get((kind, id)):
            return cast(D, written)
        name = self._registry.document_name(kind)
        body = self._unit.get(name, id)
        if body is None:
            raise NotFound(f"there is no {name} with id {id}")
        return self._codec.load(kind, body)

    def extension[X: Extension[Any]](
        self, kind: type[X], document: UUID | Document, *, version: int | None = None
    ) -> X:
        """What an extension adds to a document, given the document or its id, with the
        document's id and version.

        A field the document has never been given reads as its default. With `version`,
        refuse if the document has changed since that version, as `get` does.
        """
        id = document.id if isinstance(document, Document) else document
        host = self.get(self._registry.host(kind), id, version=version)
        held = self._extension_values(type(host), id)
        return kind.model_validate({**held, "id": id, "version": host.version})

    def find[D: Document](self, kind: type[D], /, **equals: object) -> list[D]:
        """Every document whose fields equal the given values, oldest first.

        For the rules a handler checks, which must see all of them. To show documents
        to someone, read a `page`.
        """
        name, extended = self._registry.document_name(kind), self._registry.extended(kind)
        selection = select(name, extended, where=equals)
        return [self._codec.load(kind, body) for body in self._select(kind, selection)]

    def page[D: Document](
        self,
        kind: type[D],
        *,
        where: Mapping[str, object] | None = None,
        search: str | None = None,
        order: str | None = None,
        after: str | None = None,
        limit: int = DEFAULT_LIMIT,
    ) -> Page[D]:
        """One page of the documents of a kind, filtered, searched and ordered, with how
        many there are on every page together.

        `where`, `search` and `order` are described in `remit.kernel.selection`, and
        reach the fields of the kind's extensions as they do its own. Pass the page's
        `next` as `after` to read the page that follows it.
        """
        selection = select(
            self._registry.document_name(kind),
            self._registry.extended(kind),
            where=where,
            search=search,
            order=order,
            after=after,
            limit=limit,
        )
        # One more than asked for, to learn whether another page follows.
        found = self._select(kind, replace(selection, limit=limit + 1))
        following = selection.cursor(found[limit - 1]) if len(found) > limit else None
        items = tuple(self._codec.load(kind, body) for body in found[:limit])
        return Page(items, following, self._count(kind, selection))

    def ask[R](self, query: Query[R]) -> R:
        """Ask a query inside this unit of work, on the authority of what is running.

        The query may belong to this module or to one it requires.
        """
        spec = self._registry.query(type(query))
        self._require_reach(self._registry.modules[spec.module], f"ask {spec.name}")
        with self._running(spec.module):
            return cast(R, spec.handler(query, self._reading()))

    def settings[S: BaseModel](self, kind: type[S]) -> S:
        return cast(S, self._settings[kind])

    def sql(self, query: str, parameters: Sequence[object] = ()) -> list[tuple[Any, ...]]:
        """Run a module's own SQL in this unit of work. It commits or rolls back with it.

        A module's SQL must touch only the tables its own migrations create, and read
        the kernel's tables at most. Unlike `update` and `emit`, the kernel cannot yet
        hold SQL to that: the statement runs with the kernel's own database rights, so
        it could change another module's tables, or the documents table itself, without
        an audit entry. The database refuses edits to final documents and to history
        whatever runs them, and refuses every change made while reading; giving each
        module a database role of its own, limited to its tables, is the planned way to
        enforce the rest.
        """
        return self._unit.sql(query, parameters)

    def _reading(self) -> Reader:
        """What a query's handler is given: this unit of work, with no way to write to it."""
        return self

    def _select(self, kind: type[Document], selection: Selection) -> list[Json]:
        """What the store selects, as it will stand once this unit's own writes commit."""
        written = [document for (wrote, _), document in self._written.items() if wrote is kind]
        if not written:
            return self._unit.select(selection)
        # Each write can displace at most one stored document from the page.
        enough = selection.limit and selection.limit + len(written)
        found = {body["id"]: body for body in self._unit.select(replace(selection, limit=enough))}
        for document in written:
            found[str(document.id)] = self._stored(document).body
        return selection.arranged(found.values())

    def _count(self, kind: type[Document], selection: Selection) -> int:
        """How many documents the selection matches, as they will stand once this unit's
        own writes commit."""
        counted = self._unit.count(selection)
        written = [document for (wrote, _), document in self._written.items() if wrote is kind]
        for document in written:
            stored = self._unit.get(selection.kind, document.id)
            if stored is not None and selection.matches(stored):
                counted -= 1
            if selection.matches(self._stored(document).body):
                counted += 1
        return counted

    def _stored(self, document: Document) -> StoredDocument:
        """A document this unit has written, as the store is to keep it."""
        extended = self._extended.get((type(document), document.id))
        return self._codec.dump(document, extended=extended)

    def _extension_values(self, kind: type[Document], id: UUID) -> Json:
        """What a document holds of its extensions' fields, as stored, as this unit stands.
        A document not yet stored holds none."""
        if (held := self._extended.get((kind, id))) is not None:
            return held
        extensions = self._registry.extensions(kind)
        if not extensions:
            return {}
        body = self._unit.get(self._registry.document_name(kind), id) or {}
        return {f: body[f] for extension in extensions for f in added(extension) if f in body}

    @contextmanager
    def _running(self, module: str) -> Generator[None]:
        self._modules.append(module)
        try:
            yield
        finally:
            self._modules.pop()

    def _require_reach(self, callee: Module, action: str) -> None:
        caller = self._registry.modules[self.module]
        if callee is not caller and callee not in caller.requires:
            raise RemitError(f"{caller.name} cannot {action}: it does not require {callee.name}")


class Context(Reader):
    """A unit of work a handler can change."""

    def __init__(
        self,
        *,
        actor: Actor,
        now: datetime,
        unit: Unit,
        registry: Registry,
        settings: Mapping[type[BaseModel], BaseModel],
        module: str,
    ) -> None:
        super().__init__(
            actor=actor, now=now, unit=unit, registry=registry, settings=settings, module=module
        )
        self._read_versions: dict[tuple[type[Document], UUID], int] = {}
        self._emitted: list[Event] = []
        self._erasures: dict[UUID, type[Document]] = {}
        self._acting: list[_Acting] = []

    @property
    def written(self) -> tuple[Document, ...]:
        return tuple(self._written.values())

    @property
    def stored(self) -> tuple[StoredDocument, ...]:
        """Every document written, as the store is to keep it: with its extensions' fields."""
        return tuple(self._stored(document) for document in self._written.values())

    @property
    def emitted(self) -> tuple[Event, ...]:
        return tuple(self._emitted)

    @property
    def erasures(self) -> tuple[UUID, ...]:
        return tuple(self._erasures)

    def finish(self) -> None:
        """Check what can only be checked once the handler has returned.

        An erased document must hold no personal values when the unit commits, however
        it was written after `erase` was called.
        """
        for id, kind in self._erasures.items():
            self._require_erased(self._get(kind, id))

    def _reading(self) -> Reader:
        reader = Reader(
            actor=self.actor,
            now=self.now,
            unit=self._unit,
            registry=self._registry,
            settings=self._settings,
            module=self.module,
        )
        # The reader stands in this unit of work: it sees what the unit has written,
        # and runs as whichever module the unit is running.
        reader._modules = self._modules
        reader._written = self._written
        reader._extended = self._extended
        return reader

    def create[D: Document](self, document: D) -> D:
        """Add a new document, in its lifecycle's initial state."""
        kind = type(document)
        name = self._registry.document_name(kind)
        if document.version or document.state != document.lifecycle.initial:
            raise Rejected(f"a new {name} must be unversioned and in its initial state")
        if (kind, document.id) in self._written or self._unit.get(name, document.id):
            raise Rejected(f"there is already a {name} with id {document.id}")
        return self._write(document)

    def update[T: Document | Extension[Any]](self, held: T, /, **changes: object) -> T:
        """Change fields without changing state. Final documents cannot be updated.

        Given an extension, change its fields, which are the document's, so the document's
        version moves on. Only the module that declares the extension can.
        """
        document = self._require_current(held)
        self._require_open(document)
        if not isinstance(held, Extension):
            return self._write(self._revise(held, changes))
        extension = self._revise(held, changes)
        name = self._registry.extension_name(type(extension))
        self._require_owner(self._registry.extension_owner(type(extension)).name, f"write {name}")
        self._require_referenced(extension, self._registry.document_name(type(document)))
        key = (type(document), document.id)
        own = extension.model_dump(mode="json", include=set(added(type(extension))))
        self._extended[key] = {**self._extension_values(*key), **own}
        return extension.model_copy(update={"version": self._hold(document).version})

    def transition[D: Document](self, document: D, name: str, /, **changes: object) -> D:
        """Move a document along its lifecycle, optionally filling in fields as it goes."""
        self._require_current(document)
        self._require_declared(document, name)
        step = document.lifecycle.find(name, document.state)
        revised = self._revise(document, changes, state=step.target)
        missing = [field for field in step.requires if _blank(getattr(revised, field))]
        if missing:
            kind = self._registry.document_name(type(document))
            raise Rejected(
                f"cannot {name} {kind}: fill in {', '.join(missing)} first",
                problems=(Problem((field,), f"needed to {name}", kind) for field in missing),
            )
        return self._write(revised)

    def revise[T: Document | Extension[Any]](
        self, held: T, /, **changes: object
    ) -> tuple[T, tuple[str, ...]]:
        """Replace some of a document's fields, or an extension's, refusing if none would
        change.

        Returns the revised copy and the names of the fields that changed, in the order
        given, for the event that reports the change.
        """
        changed = tuple(field for field, value in changes.items() if getattr(held, field) != value)
        if not changed:
            name = self._registry.document_name(self._document_type(type(held)))
            raise Rejected(f"nothing would change: {name} {held.id} already has these details")
        return self.update(held, **changes), changed

    def erase(self, document: Document) -> None:
        """Erase what the audit trail holds about this data subject, when this unit commits.

        Replace the document's personal fields first (with `ERASED`, null or empty): the
        audit trail's copies become unreadable, and the live document must not keep them.
        Fields its extensions add that hold personal data are returned to their defaults,
        which hold none, since the module erasing it knows nothing of them.
        """
        kind = type(document)
        name = self._registry.document_name(kind)
        self._require_owner(self._registry.document_owner(kind).name, f"erase {name}")
        if not carries_personal(kind):
            raise RemitError(f"{name} holds no personal data to erase")
        current = self.get(kind, document.id)
        cleared = {
            field: defaults(extension)[field]
            for extension in self._registry.extensions(kind)
            for field in added(extension)
            if holds_personal(extension.model_fields[field])
        }
        held = self._extension_values(kind, current.id)
        if any(held.get(field, default) != default for field, default in cleared.items()):
            self._extended[kind, current.id] = {**held, **cleared}
            current = self._write(current)
        self._require_erased(current)
        self._erasures[document.id] = kind

    def emit(self, event: Event) -> None:
        """Publish an event. It is recorded only if this unit of work commits."""
        name = self._registry.event_name(type(event))
        self._require_owner(self._registry.event_owner(type(event)).name, f"emit {name}")
        self._emitted.append(event)

    def execute[R](self, command: Command[R]) -> R:
        """Run a command inside this unit of work, on the authority of what is running.

        The command may belong to this module or to one it requires. Its handler runs
        as its own module, so it writes that module's documents, and is held to the
        move its command declares.
        """
        spec = self._registry.command(type(command))
        self._require_reach(self._registry.modules[spec.module], f"execute {spec.name}")
        with self._running(spec.module), self._acting_on(spec, command):
            return spec.handler(command, self)

    @contextmanager
    def _acting_on(self, spec: CommandSpec, command: Command[Any]) -> Generator[None]:
        referred = frozenset((found.document, found.id) for found in references(command))
        self._acting.append(_Acting(spec, self._registry.moved(spec), referred))
        try:
            yield
        finally:
            self._acting.pop()

    def _require_declared(self, document: Document, transition: str) -> None:
        """A command takes a document its input refers to only through the move it declares.

        A mistake in the module, not a refusal: raised where it happens, so that the
        module's own tests find it.
        """
        if not self._acting:
            return
        acting = self._acting[-1]
        kind = self._registry.document_name(type(document))
        if (kind, document.id) not in acting.referred:
            return
        declared = acting.spec.command.moves
        if (kind, transition) == (acting.moved, declared):
            return
        took = f"{acting.spec.name} took {kind}, which its input refers to, through {transition!r}"
        command = acting.spec.command.__name__
        if declared is None:
            raise RemitError(f"{took}; declare moves = {transition!r} on {command}")
        raise RemitError(
            f"{took}, but {command} declares moves = {declared!r}, a transition of {acting.moved}"
        )

    def _require_owner(self, owner: str, action: str) -> None:
        if owner != self.module:
            raise RemitError(
                f"{self.module} cannot {action}: it belongs to {owner}, so only {owner}'s "
                f"own code can do that; execute one of {owner}'s commands instead"
            )

    def _require_erased(self, document: Document) -> None:
        extensions = self._registry.extensions(type(document))
        views = [self.extension(extension, document) for extension in extensions]
        if kept := [field for held in (document, *views) for field in unerased(held)]:
            name = self._registry.document_name(type(document))
            raise RemitError(
                f"replace {name}'s personal fields before erasing it: {', '.join(kept)}"
            )

    def _require_current(self, held: Document | Extension[Any]) -> Document:
        """The document as this unit holds it, refusing a copy of it, or of what an
        extension adds to it, from before the unit last changed it."""
        document = self.get(self._document_type(type(held)), held.id)
        if document.version != held.version:
            raise RemitError(
                f"{type(held).__name__} {held.id} was changed earlier in this unit of "
                "work; use the copy that change returned"
            )
        return document

    def _require_open(self, document: Document) -> None:
        if document.is_final:
            name = self._registry.document_name(type(document))
            raise Rejected(f"{name} {document.id} is {document.state} and can no longer change")

    def _require_referenced(self, held: Document | Extension[Any], name: str) -> None:
        """What a document, or an extension of one named `name`, refers to exists."""
        for found in references(held):
            try:
                self._get(self._registry.document_type(found.document), found.id)
            except NotFound:
                where = ".".join(map(str, found.path))
                missing = f"there is no {found.document} with id {found.id}"
                raise Rejected(
                    f"{name} {where} refers to nothing: {missing}",
                    problems=(Problem(found.path, missing, name),),
                ) from None

    def _require_unique(self, document: Document) -> None:
        for fields in document.unique_together():
            values = {field: getattr(document, field) for field in fields}
            if any(value is None for value in values.values()):
                continue
            if any(other.id != document.id for other in self.find(type(document), **values)):
                raise _taken(self._registry.document_name(type(document)), values)

    def _write[D: Document](self, document: D) -> D:
        name = self._registry.document_name(type(document))
        self._require_owner(self._registry.document_owner(type(document)).name, f"write {name}")
        self._require_referenced(document, name)
        self._require_unique(document)
        return self._hold(document)

    def _hold[D: Document](self, document: D) -> D:
        """Hold a document to be stored when the unit commits, at the version after the one
        it was first read at in this unit."""
        key = (type(document), document.id)
        if key not in self._extended:
            # The module writing a document owns its own fields, not its extensions', which
            # are kept as they are stored.
            self._extended[key] = self._extension_values(*key)
        read_version = self._read_versions.setdefault(key, document.version)
        written = document.model_copy(update={"version": read_version + 1})
        self._written[key] = written
        return written

    def _revise[T: Document | Extension[Any]](
        self, held: T, changes: dict[str, object], **kernel_set: object
    ) -> T:
        model = type(held)
        if reserved := _RESERVED & changes.keys():
            raise RemitError(f"{', '.join(sorted(reserved))} are managed by the kernel")
        if unknown := changes.keys() - model.model_fields.keys():
            declared = (
                self._registry.extension_name(model)
                if issubclass(model, Extension)
                else self._registry.document_name(model)
            )
            raise RemitError(f"{declared} has no fields {', '.join(sorted(unknown))}")
        try:
            return model.model_validate({**dict(held), **changes, **kernel_set})
        except ValidationError as error:
            name = self._registry.document_name(self._document_type(model))
            raise invalid(f"{name} {held.id}", error, document=name) from None

    def _document_type(self, model: type[Document] | type[Extension[Any]]) -> type[Document]:
        """The document type itself, or the one an extension extends."""
        return self._registry.host(model) if issubclass(model, Extension) else model


def _blank(value: object) -> bool:
    return value is None or value == "" or (isinstance(value, (tuple, list)) and not value)


def _taken(name: str, values: dict[str, object]) -> Rejected:
    """The refusal of a value, or values unique together, that another document holds."""
    if len(values) == 1:
        ((field, value),) = values.items()
        said = f"{value!r} is already in use"
        return Rejected(f"{name} {field} {said}", problems=(Problem((field,), said, name),))
    together = " and ".join(values)
    return Rejected(
        f"{name} {together} {tuple(values.values())!r} are already in use together",
        problems=tuple(
            Problem((field,), f"{value!r} is already in use with the same {together}", name)
            for field, value in values.items()
        ),
    )
