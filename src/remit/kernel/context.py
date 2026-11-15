"""The context: one unit of work, handed to every handler.

Everything a handler reads, writes or emits goes through it. Reads come from the unit's
transaction; writes are held here until the kernel commits them together, so a handler
that raises leaves nothing behind.

A module reads any document but writes only its own, and emits only its own events. To
change another module's documents it executes that module's commands, so the rules
those commands enforce (a ledger's balance, a code that never changes) cannot be gone
around. The context knows which module's code is running because it is told each time
control passes from one module to another: when the kernel runs a command or a
subscription, and when a handler executes another module's command.
"""

from collections.abc import Generator, Mapping, Sequence
from contextlib import contextmanager
from datetime import datetime
from typing import Any, Protocol, cast
from uuid import UUID

from pydantic import BaseModel, ValidationError

from .actor import Actor
from .codec import Codec
from .document import Document
from .errors import NotFound, Problem, Rejected, RemitError, invalid
from .message import Command, Event
from .personal import carries_personal, unerased
from .registry import Registry
from .store import Unit

_RESERVED = frozenset({"id", "state", "version"})


class Reader(Protocol):
    """Read access to documents and settings. The kernel and the context both provide it."""

    def get[D: Document](self, kind: type[D], id: UUID) -> D: ...
    def find[D: Document](self, kind: type[D], /, **equals: object) -> list[D]: ...
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
        self._read_versions: dict[tuple[type[Document], UUID], int] = {}
        self._emitted: list[Event] = []
        self._erasures: dict[UUID, type[Document]] = {}

    @property
    def module(self) -> str:
        """The module whose code is running now."""
        return self._modules[-1]

    @property
    def written(self) -> tuple[Document, ...]:
        return tuple(self._written.values())

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

    def find[D: Document](self, kind: type[D], /, **equals: object) -> list[D]:
        """Documents whose fields equal the given values, oldest first."""
        filters = self._codec.filters(kind, equals)
        stored = self._unit.find(self._registry.document_name(kind), filters)
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
        """Run a module's own SQL in this unit of work. It commits or rolls back with it.

        A module's SQL must touch only the tables its own migrations create, and read
        the kernel's tables at most. Unlike `update` and `emit`, the kernel cannot yet
        hold SQL to that: the statement runs with the kernel's own database rights, so
        it could change another module's tables, or the documents table itself, without
        an audit entry. The database refuses edits to final documents and to history
        whatever runs them; giving each module a database role of its own, limited to
        its tables, is the planned way to enforce the rest.
        """
        return self._unit.sql(query, parameters)

    def create[D: Document](self, document: D) -> D:
        """Add a new document, in its lifecycle's initial state."""
        kind = type(document)
        name = self._registry.document_name(kind)
        if document.version or document.state != document.lifecycle.initial:
            raise Rejected(f"a new {name} must be unversioned and in its initial state")
        if (kind, document.id) in self._written or self._unit.get(name, document.id):
            raise Rejected(f"there is already a {name} with id {document.id}")
        return self._write(document)

    def update[D: Document](self, document: D, /, **changes: object) -> D:
        """Change fields without changing state. Final documents cannot be updated."""
        self._require_current(document)
        if document.is_final:
            name = self._registry.document_name(type(document))
            raise Rejected(f"{name} {document.id} is {document.state} and can no longer change")
        return self._write(self._revise(document, changes))

    def transition[D: Document](self, document: D, name: str, /, **changes: object) -> D:
        """Move a document along its lifecycle, optionally filling in fields as it goes."""
        self._require_current(document)
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

    def revise[D: Document](self, document: D, /, **changes: object) -> tuple[D, tuple[str, ...]]:
        """Replace some of a document's fields, refusing if none would change.

        Returns the revised document and the names of the fields that changed, in the
        order given, for the event that reports the change.
        """
        changed = tuple(
            field for field, value in changes.items() if getattr(document, field) != value
        )
        if not changed:
            name = self._registry.document_name(type(document))
            raise Rejected(f"nothing would change: {name} {document.id} already has these details")
        return self.update(document, **changes), changed

    def erase(self, document: Document) -> None:
        """Erase what the audit trail holds about this data subject, when this unit commits.

        Replace the document's personal fields first (with `ERASED`, null or empty): the
        audit trail's copies become unreadable, and the live document must not keep them.
        """
        kind = type(document)
        name = self._registry.document_name(kind)
        self._require_owner(self._registry.document_owner(kind).name, f"erase {name}")
        if not carries_personal(kind):
            raise RemitError(f"{name} holds no personal data to erase")
        self._require_erased(self.get(kind, document.id))
        self._erasures[document.id] = kind

    def emit(self, event: Event) -> None:
        """Publish an event. It is recorded only if this unit of work commits."""
        name = self._registry.event_name(type(event))
        self._require_owner(self._registry.event_owner(type(event)).name, f"emit {name}")
        self._emitted.append(event)

    def execute[R](self, command: Command[R]) -> R:
        """Run another command inside this unit of work, on this command's authority.

        The command may belong to this module or to one it requires. Its handler runs
        as its own module, so it writes that module's documents.
        """
        spec = self._registry.command(type(command))
        caller = self._registry.modules[self.module]
        callee = self._registry.modules[spec.module]
        if callee is not caller and callee not in caller.requires:
            raise RemitError(
                f"{caller.name} cannot execute {spec.name}: it does not require {callee.name}"
            )
        with self._running(spec.module):
            return spec.handler(command, self)

    @contextmanager
    def _running(self, module: str) -> Generator[None]:
        self._modules.append(module)
        try:
            yield
        finally:
            self._modules.pop()

    def _require_owner(self, owner: str, action: str) -> None:
        if owner != self.module:
            raise RemitError(
                f"{self.module} cannot {action}: it belongs to {owner}, so only {owner}'s "
                f"own code can do that; execute one of {owner}'s commands instead"
            )

    def _require_erased(self, document: Document) -> None:
        if kept := unerased(document):
            name = self._registry.document_name(type(document))
            raise RemitError(
                f"replace {name}'s personal fields before erasing it: {', '.join(kept)}"
            )

    def _require_current(self, document: Document) -> None:
        if self.get(type(document), document.id).version != document.version:
            raise RemitError(
                f"{type(document).__name__} {document.id} was changed earlier in this unit of "
                "work; use the copy that change returned"
            )

    def _require_unique(self, document: Document) -> None:
        for field in document.unique:
            value = getattr(document, field)
            if value is None:
                continue
            if any(
                other.id != document.id for other in self.find(type(document), **{field: value})
            ):
                name = self._registry.document_name(type(document))
                raise Rejected(
                    f"{name} {field} {value!r} is already in use",
                    problems=(Problem((field,), f"{value!r} is already in use", name),),
                )

    def _write[D: Document](self, document: D) -> D:
        name = self._registry.document_name(type(document))
        self._require_owner(self._registry.document_owner(type(document)).name, f"write {name}")
        self._require_unique(document)
        key = (type(document), document.id)
        read_version = self._read_versions.setdefault(key, document.version)
        written = document.model_copy(update={"version": read_version + 1})
        self._written[key] = written
        return written

    def _revise[D: Document](
        self, document: D, changes: dict[str, object], **kernel_set: object
    ) -> D:
        if reserved := _RESERVED & changes.keys():
            raise RemitError(f"{', '.join(sorted(reserved))} are managed by the kernel")
        if unknown := changes.keys() - type(document).model_fields.keys():
            name = self._registry.document_name(type(document))
            raise RemitError(f"{name} has no fields {', '.join(sorted(unknown))}")
        try:
            return type(document).model_validate({**dict(document), **changes, **kernel_set})
        except ValidationError as error:
            name = self._registry.document_name(type(document))
            raise invalid(f"{name} {document.id}", error, document=name) from None


def _blank(value: object) -> bool:
    return value is None or value == "" or (isinstance(value, (tuple, list)) and not value)
