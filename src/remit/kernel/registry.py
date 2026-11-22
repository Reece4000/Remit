"""The registry: every declaration from every module, checked once at startup.

It is the single source the kernel resolves names from, and `describe()` is the single
source everything outside the kernel (API schema, agent tools, docs) is generated from.
"""

from collections.abc import Iterable
from typing import Any, cast

from pydantic import BaseModel

from .document import Document
from .errors import RegistrationError
from .extension import Extension, added, extended, extends, reserved
from .message import Command, Event, Query
from .module import (
    KERNEL,
    CommandSpec,
    Module,
    QuerySpec,
    Subscription,
    installation_order,
    read_permission,
)
from .personal import carries_personal, holds_personal, is_erased, unsealable, unsealable_result
from .reference import declared, misplaced, refers_to, unnarrowable
from .selection import defaults, holds_text, listing

READ_AUDIT = read_permission(f"{KERNEL}.audit")
"""The permission to read the audit trail, which holds every command's input."""
READ_EVENTS = read_permission(f"{KERNEL}.events")
"""The permission to read the event log."""


class Registry:
    def __init__(self, modules: Iterable[Module]) -> None:
        """Register the modules and everything they require, dependencies first."""
        self.modules: dict[str, Module] = {}
        self._documents: dict[type[Document], str] = {}
        self._document_types: dict[str, type[Document]] = {}
        self._document_owners: dict[type[Document], Module] = {}
        self._extensions: dict[type[Extension[Any]], str] = {}
        self._extension_owners: dict[type[Extension[Any]], Module] = {}
        self._extension_hosts: dict[type[Extension[Any]], type[Document]] = {}
        self._extending: dict[type[Document], list[type[Extension[Any]]]] = {}
        self._extended: dict[type[Document], type[Document]] = {}
        """Each document type something extends, to the type with the extensions' fields."""
        self._unextended: dict[type[Document], type[Document]] = {}
        self._events: dict[type[Event], str] = {}
        self._event_owners: dict[type[Event], Module] = {}
        self._commands: dict[type[Command[Any]], CommandSpec] = {}
        self._command_names: dict[str, CommandSpec] = {}
        self._queries: dict[type[Query[Any]], QuerySpec] = {}
        self._query_names: dict[str, QuerySpec] = {}
        self._moved: dict[str, str] = {}

        for module in installation_order(modules):
            if module.name in self.modules:
                raise RegistrationError(f"module {module.name!r} is installed twice")
            self.modules[module.name] = module
            for name, document in module.documents.items():
                _check_unclaimed(name, self._documents.get(document))
                _check_lifecycle(name, document)
                _check_searchable(name, document)
                _check_sealable(name, document)
                self._documents[document] = name
                self._document_types[name] = document
                self._document_owners[document] = module
            for name, extension in module.extensions.items():
                _check_unclaimed(name, self._extensions.get(extension))
                host = self._check_extension(module, name, extension)
                _check_searchable(name, extension)
                _check_sealable(name, extension)
                self._extensions[extension] = name
                self._extension_owners[extension] = module
                self._extension_hosts[extension] = host
                self._extending.setdefault(host, []).append(extension)
            for name, event in module.events.items():
                _check_unclaimed(name, self._events.get(event))
                _check_sealable(name, event)
                if carries_personal(event):
                    raise RegistrationError(
                        f"{name} carries personal data; events are kept for good and sent to "
                        "every subscriber, so they carry ids and codes, never personal values"
                    )
                self._events[event] = name
                self._event_owners[event] = module
            for spec in module.commands.values():
                claimed = self._commands.get(spec.command)
                _check_unclaimed(spec.name, claimed.name if claimed else None)
                _check_sealable(spec.name, spec.command)
                if reason := unsealable_result(spec.result):
                    raise RegistrationError(
                        f"{spec.name}: what it returns {reason}, where it could not be sealed "
                        "in its idempotency record"
                    )
                self._commands[spec.command] = spec
                self._command_names[spec.name] = spec
            for query in module.queries.values():
                asked = self._queries.get(query.query)
                _check_unclaimed(query.name, asked.name if asked else None)
                self._queries[query.query] = query
                self._query_names[query.name] = query

        for host, extensions in self._extending.items():
            declared_by = [(self._extension_owners[e].name, e) for e in extensions]
            self._extended[host] = extended(host, declared_by)
            self._unextended[self._extended[host]] = host

        self.subscriptions: tuple[Subscription, ...] = tuple(
            subscription
            for module in self.modules.values()
            for subscription in module.subscriptions
        )
        for subscription in self.subscriptions:
            self._check_reach(subscription)
        for module in self.modules.values():
            self._check_references(module)
            for name, document in module.documents.items():
                if document.part_of is not None:
                    _check_part_of(name, document, document.part_of)
            for spec in module.commands.values():
                if spec.command.moves is not None:
                    self._moved[spec.name] = self._check_moves(spec, spec.command.moves)
                if spec.command.amends is not None:
                    self._check_amends(spec, spec.command.amends)

    def document_name(self, kind: type[Document]) -> str:
        """The name a document type is registered as, which is also its extended type's."""
        return _resolve(self._documents, self._unextended.get(kind, kind), "document")

    def extended[D: Document](self, kind: type[D]) -> type[D]:
        """The document type with every installed extension's fields beside its own: what
        its stored body holds, and what is listed and described. The type itself when
        nothing extends it.

        For reading only. A module writes the type it declares, and an extension writes
        its own fields.
        """
        return cast(type[D], self._extended.get(kind, kind))

    def extensions(self, kind: type[Document]) -> tuple[type[Extension[Any]], ...]:
        """The installed extensions of a document type, in the order they were installed."""
        return tuple(self._extending.get(kind, ()))

    def extension_name(self, kind: type[Extension[Any]]) -> str:
        return _resolve(self._extensions, kind, "extension")

    def extension_owner(self, kind: type[Extension[Any]]) -> Module:
        """The module that declares this extension, and alone may write its fields."""
        return _resolve(self._extension_owners, kind, "extension")

    def host(self, kind: type[Extension[Any]]) -> type[Document]:
        """The document type this extension extends."""
        return _resolve(self._extension_hosts, kind, "extension")

    def event_name(self, kind: type[Event]) -> str:
        return _resolve(self._events, kind, "event")

    def command(self, kind: type[Command[Any]]) -> CommandSpec:
        return _resolve(self._commands, kind, "command")

    def find_command(self, name: str) -> CommandSpec | None:
        """The command registered as `name`, such as `parties.register_party`, if any."""
        return self._command_names.get(name)

    def moved(self, spec: CommandSpec) -> str | None:
        """The registered name of the document the command may move, if it declares a move."""
        return self._moved.get(spec.name)

    def query(self, kind: type[Query[Any]]) -> QuerySpec:
        return _resolve(self._queries, kind, "query")

    def find_query(self, name: str) -> QuerySpec | None:
        """The query registered as `name`, such as `ledger.trial_balance`, if any."""
        return self._query_names.get(name)

    def find_document(self, name: str) -> type[Document] | None:
        """The document type registered as `name`, such as `parties.party`, if any."""
        return self._document_types.get(name)

    def read_permission(self, kind: type[Document]) -> str:
        """The permission that reading documents of this kind takes."""
        return read_permission(self.document_name(kind))

    def document_owner(self, kind: type[Document]) -> Module:
        """The module that declares this document type, and alone may write it."""
        return _resolve(self._document_owners, kind, "document")

    def event_owner(self, kind: type[Event]) -> Module:
        """The module that declares this event, and alone may emit it."""
        return _resolve(self._event_owners, kind, "event")

    def document_type(self, name: str) -> type[Document]:
        return _resolve(self._document_types, name, "document")

    def event_type(self, name: str) -> type[Event]:
        return _resolve({name: kind for kind, name in self._events.items()}, name, "event")

    def _check_reach(self, subscription: Subscription) -> None:
        event = self.event_name(subscription.event)
        owner = self._event_owners[subscription.event]
        module = self.modules[subscription.module]
        if owner is not module and owner not in module.requires:
            raise RegistrationError(
                f"{subscription.name} reacts to {event}, "
                f"but {module.name} does not require {owner.name}"
            )

    def _check_references(self, module: Module) -> None:
        """References name documents of the module or of those it requires, and sit on ids."""
        reachable = (module, *module.requires)
        names = {
            name
            for kind, name in self._documents.items()
            if self._document_owners[kind] in reachable
        }
        messages = (
            *(spec.command for spec in module.commands.values()),
            *(spec.query for spec in module.queries.values()),
        )
        declarations = (*module.documents.values(), *module.extensions.values())
        documents = {name: kind for kind, name in self._documents.items()}
        for model in (*declarations, *module.events.values(), *messages):
            if reason := misplaced(model) or unnarrowable(model, documents):
                raise RegistrationError(reason)
            for where, document in declared(model):
                if document not in names:
                    raise RegistrationError(
                        f"{where} refers to {document}, which is not a document of "
                        f"{module.name} or of a module it requires"
                    )

    def _check_extension(
        self, module: Module, name: str, extension: type[Extension[Any]]
    ) -> type[Document]:
        """The document an extension extends: one of a module this one requires, to which
        it adds fields of its own, each with a default, holding personal data only if the
        document is a data subject already."""
        host = extends(extension)
        if host is None:
            raise RegistrationError(
                f"{name} names no document to extend; declare it as "
                f"{extension.__name__}(Extension[TheDocument])"
            )
        document = self._documents.get(host)
        if document is None:
            raise RegistrationError(
                f"{name} extends {host.__name__}, which is not a document of an installed module"
            )
        owner = self._document_owners[host]
        if owner is module:
            raise RegistrationError(
                f"{name} extends {document}, a document of {module.name} itself; declare its "
                "fields there instead"
            )
        if owner not in module.requires:
            raise RegistrationError(
                f"{name} extends {document}, but {module.name} does not require {owner.name}"
            )
        if clashing := reserved(extension):
            raise RegistrationError(
                f"{name} declares {', '.join(clashing)}, which every document has already"
            )
        for field in added(extension):
            if field in host.model_fields:
                raise RegistrationError(f"{name} declares {field!r}, which {document} has already")
            for other in self._extending.get(host, ()):
                if field in added(other):
                    raise RegistrationError(
                        f"{name} declares {field!r}, which {self._extensions[other]} already "
                        f"adds to {document}"
                    )
        own = {field: extension.model_fields[field] for field in added(extension)}
        if undefaulted := [f for f, i in own.items() if i.is_required() or i.default_factory]:
            raise RegistrationError(
                f"{name} needs a default value for {', '.join(undefaulted)}: documents written "
                "before it was installed hold nothing for its fields, so read as holding "
                "their defaults"
            )
        personal = [field for field, info in own.items() if holds_personal(info)]
        if personal and not carries_personal(host):
            raise RegistrationError(
                f"{name} holds personal data, but {document} holds none and is erased by no "
                "one; only a document that is a data subject can be given personal fields"
            )
        if kept := [field for field in personal if not is_erased(defaults(extension)[field])]:
            raise RegistrationError(
                f"{name}.{kept[0]} holds personal data, so its default must hold none (null, "
                f"empty or ERASED): erasing a {document} returns the field to its default"
            )
        return host

    def _check_moves(self, spec: CommandSpec, transition: str) -> str:
        """The document a command moves: of those its input refers to, the one kind whose
        lifecycle has the transition the command declares."""
        referred = dict.fromkeys(document for _, document in declared(spec.command))
        having = [
            name
            for name in referred
            if any(t.name == transition for t in self._document_types[name].lifecycle.transitions)
        ]
        declaring = f"{spec.name} declares moves = {transition!r}"
        if len(having) > 1:
            raise RegistrationError(
                f"{declaring}, but its input refers to {' and '.join(having)}, each with that "
                "transition, so which it moves is ambiguous"
            )
        if not having:
            raise RegistrationError(
                f"{declaring}, but no document its input refers to has that transition; "
                f"it refers to {', '.join(referred) or 'none'}"
            )
        return having[0]

    def _check_amends(self, spec: CommandSpec, by: str) -> None:
        """A command that amends a document refers to it by one field, returns it or one of
        its extensions, carries the version it was read at, and otherwise holds only the
        fields of what it returns, declared the same way, so that whatever the document
        holds can be sent back."""
        command = spec.command
        if by not in command.model_fields:
            raise RegistrationError(
                f"{spec.name} declares amends = {by!r}, but {command.__name__} has no such field"
            )
        name = refers_to(command, by)
        if name is None:
            raise RegistrationError(
                f"{spec.name} declares amends = {by!r}, but {command.__name__}.{by} does not "
                "hold a single document's id"
            )
        document = self._document_types[name]
        if spec.result is document:
            holder, held = name, document.model_fields
        elif spec.result in self._extending.get(document, ()):
            extension: type[Extension[Any]] = spec.result
            holder = self._extensions[extension]
            held = {field: extension.model_fields[field] for field in added(extension)}
        else:
            raise RegistrationError(
                f"{spec.name} amends {name}, so it must return {document.__name__}, or an "
                "extension of it"
            )
        version = command.model_fields.get("version")
        if version is None or version.annotation is not int:
            raise RegistrationError(
                f"{spec.name} amends {name}, so it needs version: int, the version the document "
                "was read at, so that a change made from an out-of-date copy is refused"
            )
        for field in _amended(command, by):
            own = held.get(field)
            if own is None:
                raise RegistrationError(
                    f"{spec.name} amends {holder}, which has no field {field!r}; besides "
                    f"{by} and version, the command holds only {holder}'s own fields"
                )
            given = command.model_fields[field]
            if (given.annotation, given.metadata) != (own.annotation, own.metadata):
                raise RegistrationError(
                    f"{spec.name} declares {field} unlike {holder} does; a screen copies the "
                    "document's value into it, so declare it with the same type and constraints"
                )

    def permissions(self) -> list[str]:
        """Every permission there is to hold, for what is installed: each command by name,
        each read as `<module>.read.<name>`, then the audit trail's and the event log's."""
        modules = self.modules.values()
        return [
            *(name for module in modules for name in module.commands),
            *(read_permission(name) for module in modules for name in module.documents),
            *(spec.permission for module in modules for spec in module.queries.values()),
            READ_AUDIT,
            READ_EVENTS,
        ]

    def describe(self) -> dict[str, Any]:
        """Everything installed, as plain JSON-ready data.

        The kernel's own permissions, to read the audit trail (`kernel.read.audit`) and
        the event log (`kernel.read.events`), belong to no module and are not listed.
        """
        return {name: self._describe(module) for name, module in self.modules.items()}

    def _describe(self, module: Module) -> dict[str, Any]:
        return {
            "requires": [dependency.name for dependency in module.requires],
            "settings": module.settings.model_json_schema() if module.settings else None,
            "documents": {
                name: {
                    "summary": (kind.__doc__ or "").strip(),
                    "permission": read_permission(name),
                    "schema": self.extended(kind).model_json_schema(),
                    "listing": listing(self.extended(kind)),
                    "part_of": (
                        {"document": refers_to(kind, kind.part_of), "field": kind.part_of}
                        if kind.part_of is not None
                        else None
                    ),
                    "lifecycle": {
                        "initial": kind.lifecycle.initial,
                        "states": list(kind.lifecycle.states),
                        "final": sorted(kind.lifecycle.final),
                        "transitions": [
                            {
                                "name": t.name,
                                "from": t.source,
                                "to": t.target,
                                "requires": list(t.requires),
                            }
                            for t in kind.lifecycle.transitions
                        ],
                    },
                }
                for name, kind in module.documents.items()
            },
            "extensions": {
                name: {
                    "summary": (kind.__doc__ or "").strip(),
                    "extends": self.document_name(self.host(kind)),
                    "fields": list(added(kind)),
                }
                for name, kind in module.extensions.items()
            },
            "commands": {
                name: {
                    "summary": spec.summary,
                    "permission": name,
                    "input": spec.command.model_json_schema(),
                    "returns": spec.returns.json_schema(mode="serialization"),
                    "moves": (
                        {"document": self._moved[name], "transition": spec.command.moves}
                        if name in self._moved
                        else None
                    ),
                    "amends": (
                        {
                            "document": refers_to(spec.command, spec.command.amends),
                            "by": spec.command.amends,
                            "fields": _amended(spec.command, spec.command.amends),
                        }
                        if spec.command.amends is not None
                        else None
                    ),
                }
                for name, spec in module.commands.items()
            },
            "queries": {
                name: {
                    "summary": spec.summary,
                    "permission": spec.permission,
                    "input": spec.query.model_json_schema(),
                    "returns": spec.returns.json_schema(mode="serialization"),
                }
                for name, spec in module.queries.items()
            },
            "events": {
                name: {"summary": (kind.__doc__ or "").strip(), "schema": kind.model_json_schema()}
                for name, kind in module.events.items()
            },
            "subscriptions": [
                {"name": s.name, "event": self.event_name(s.event), "summary": s.summary}
                for s in module.subscriptions
            ],
        }


def _check_lifecycle(name: str, document: type[Document]) -> None:
    lifecycle = getattr(document, "lifecycle", None)
    if lifecycle is None:
        raise RegistrationError(f"{name} declares no lifecycle")
    together = document.unique_together()
    if unknown := [f for fields in together for f in fields if f not in document.model_fields]:
        raise RegistrationError(f"{name} declares unknown fields {unknown} unique")
    if any(len(fields) == 0 or len(set(fields)) != len(fields) for fields in together):
        raise RegistrationError(f"{name} declares a unique group that is empty or repeats a field")
    for transition in lifecycle.transitions:
        unknown = [field for field in transition.requires if field not in document.model_fields]
        if unknown:
            raise RegistrationError(
                f"{name}: transition {transition.name!r} requires unknown fields {unknown}"
            )


def _check_part_of(name: str, document: type[Document], field: str) -> None:
    if field not in document.model_fields:
        raise RegistrationError(f"{name} declares part_of = {field!r}, but has no such field")
    whole = refers_to(document, field)
    if whole is None:
        raise RegistrationError(
            f"{name} declares part_of = {field!r}, but {document.__name__}.{field} does not "
            "hold a single document's id"
        )
    if whole == name:
        raise RegistrationError(
            f"{name} declares part_of = {field!r}, which refers to {name} itself; a line is "
            "part of another kind of document"
        )


def _check_searchable(name: str, declaring: type[Document] | type[Extension[Any]]) -> None:
    for field in declaring.searchable:
        info = declaring.model_fields.get(field)
        if info is None:
            raise RegistrationError(f"{name} declares an unknown field {field!r} searchable")
        if not holds_text(info.annotation):
            raise RegistrationError(
                f"{name}.{field} is declared searchable but does not hold text; "
                "search reads text fields, and tuples or lists of text"
            )


def _check_unclaimed(name: str, claimed: str | None) -> None:
    if claimed is not None:
        raise RegistrationError(
            f"{name} is the class already registered as {claimed}; each module declares "
            "its own classes"
        )


def _check_sealable(name: str, model: type[BaseModel]) -> None:
    if reason := unsealable(model):
        raise RegistrationError(f"{name}: {reason}, where it could not be sealed")


def _amended(command: type[Command[Any]], by: str) -> list[str]:
    """The fields a command that amends a document replaces: all but `by` and `version`."""
    return [field for field in command.model_fields if field not in (by, "version")]


def _resolve[K, V](declared: dict[K, V], kind: K, what: str) -> V:
    try:
        return declared[kind]
    except KeyError:
        raise RegistrationError(
            f"{getattr(kind, '__name__', kind)} is not a {what} of any installed module"
        ) from None
