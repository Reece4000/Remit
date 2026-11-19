"""Modules: how a piece of the business declares itself to the kernel.

A module is a plain object that collects document types, events, command and query
handlers, and subscriptions. Everything else (permissions, the API, agent tools,
documentation) is derived from these declarations, so each one must carry a docstring.
"""

import inspect
import re
from collections.abc import Callable, Container, Iterable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, TypeAdapter

from .document import Document
from .errors import RegistrationError
from .message import Command, Event, Query

if TYPE_CHECKING:
    from .context import Context, Reader

type Handler[M, R] = Callable[[M, Context], R]
type Answer[Q, R] = Callable[[Q, Reader], R]
"""A query's handler. It is given a reader, so it has no way to change anything."""

KERNEL = "kernel"
"""The name the kernel's own permissions are declared under. No module can take it."""

_NAME = re.compile(r"[a-z][a-z0-9_]*")


@dataclass(frozen=True)
class Migration:
    """SQL a PostgreSQL store applies exactly once, identified by its name."""

    name: str
    sql: str


@dataclass(frozen=True)
class CommandSpec:
    name: str
    module: str
    command: type[Command[Any]]
    handler: Handler[Any, Any]
    result: Any
    """The return annotation, from which `returns` is built."""
    returns: TypeAdapter[Any]
    summary: str


@dataclass(frozen=True)
class QuerySpec:
    name: str
    module: str
    query: type[Query[Any]]
    handler: Answer[Any, Any]
    result: Any
    """The return annotation, from which `returns` is built."""
    returns: TypeAdapter[Any]
    summary: str

    @property
    def permission(self) -> str:
        return read_permission(self.name)


@dataclass(frozen=True)
class Subscription:
    name: str
    module: str
    event: type[Event]
    handler: Handler[Any, None]
    summary: str


class Module:
    def __init__(
        self,
        name: str,
        *,
        requires: tuple[Module, ...] = (),
        settings: type[BaseModel] | None = None,
        migrations: tuple[Migration, ...] = (),
        sql: str = "",
    ) -> None:
        if not _NAME.fullmatch(name):
            raise RegistrationError(
                f"module name {name!r} must be lower case letters, digits and underscores, "
                "starting with a letter: it prefixes every name the module declares, so it "
                "cannot contain the dot that separates the two"
            )
        if name == KERNEL:
            raise RegistrationError(
                f"a module cannot be named {KERNEL!r}: the kernel declares its own "
                "permissions under that name"
            )
        self.name = name
        self._requires = requires
        self.settings = settings
        self.migrations = migrations
        """Schema changes, each applied once, in order, when a PostgreSQL store is installed."""
        self.sql = sql
        """Rules the database enforces itself. Idempotent; reapplied on every install."""
        self.documents: dict[str, type[Document]] = {}
        self.events: dict[str, type[Event]] = {}
        self.commands: dict[str, CommandSpec] = {}
        self.queries: dict[str, QuerySpec] = {}
        self.subscriptions: list[Subscription] = []

    @property
    def requires(self) -> tuple[Module, ...]:
        """The modules this one builds on: it may use their public API and react to their events.

        A dependency is a module object, so it must exist before the module that names
        it, and requirements cannot change afterwards. A cycle therefore cannot be declared.
        """
        return self._requires

    def document[D: Document](self, kind: type[D]) -> type[D]:
        """Declare a document type."""
        _summary(kind)
        self.documents[self._claim(kind.__name__, self._readable)] = kind
        return kind

    def event[E: Event](self, kind: type[E]) -> type[E]:
        """Declare an event this module publishes."""
        _summary(kind)
        self.events[self._claim(kind.__name__, self.events)] = kind
        return kind

    def command[C: Command[Any], R](self, handler: Handler[C, R]) -> Handler[C, R]:
        """Declare a command by its handler; the command type is the first parameter's."""
        command, result = _signature(handler, Command)
        name = self._claim(command.__name__, self._actions)
        self.commands[name] = CommandSpec(
            name, self.name, command, handler, result, TypeAdapter(result), _summary(handler)
        )
        return handler

    def query[Q: Query[Any], R](self, handler: Answer[Q, R]) -> Answer[Q, R]:
        """Declare a query by its handler; the query type is the first parameter's.

        For reads that take code: a balance, a report, anything a listing of documents
        cannot answer. The handler is given a reader, which reads and cannot write.
        """
        query, result = _signature(handler, Query)
        name = self._claim(query.__name__, self._readable)
        self.queries[name] = QuerySpec(
            name, self.name, query, handler, result, TypeAdapter(result), _summary(handler)
        )
        return handler

    def subscribe[E: Event](
        self, event: type[E], *, name: str | None = None
    ) -> Callable[[Handler[E, None]], Handler[E, None]]:
        """Declare a reaction to an event, from this module or any other.

        A subscription's name is where it keeps its place in the event log, so renaming
        it would deliver every event to it again from the start. It defaults to the
        handler's name; give `name` to keep it fixed while the handler is renamed.
        """

        def declare(handler: Handler[E, None]) -> Handler[E, None]:
            chosen = handler.__name__ if name is None else name
            if not _NAME.fullmatch(chosen):
                raise RegistrationError(
                    f"subscription name {chosen!r} must be lower case letters, digits and "
                    "underscores, starting with a letter"
                )
            claimed = self._claim(chosen, self._actions)
            self.subscriptions.append(
                Subscription(claimed, self.name, event, handler, _summary(handler))
            )
            return handler

        return declare

    @property
    def _actions(self) -> set[str]:
        """Names of commands and subscriptions: both name the action in the audit trail."""
        return {*self.commands, *(subscription.name for subscription in self.subscriptions)}

    @property
    def _readable(self) -> set[str]:
        """Names of documents and queries: both name what the permission to read covers."""
        return {*self.documents, *self.queries}

    def _claim(self, declared: str, taken: Container[str]) -> str:
        name = f"{self.name}.{re.sub(r'(?<!^)(?=[A-Z])', '_', declared).lower()}"
        if name in taken:
            raise RegistrationError(f"{name} is declared twice")
        return name


def installation_order(modules: Iterable[Module]) -> tuple[Module, ...]:
    """The given modules and everything they require, each after all of its dependencies.

    Otherwise modules keep the order they were given in, so installation is deterministic.
    """
    ordered: dict[int, Module] = {}

    def visit(module: Module) -> None:
        if id(module) not in ordered:
            for dependency in module.requires:
                visit(dependency)
            ordered[id(module)] = module

    for module in modules:
        visit(module)
    return tuple(ordered.values())


def read_permission(name: str) -> str:
    """The permission to read what is registered as `name`: `ledger.read.account`.

    Reading is named apart from commands, so that a role can be given a module's reads
    (`ledger.read.*`) without its commands, while `ledger.*` still grants both.
    """
    module, _, declared = name.partition(".")
    return f"{module}.read.{declared}"


def _signature[M: BaseModel](handler: Callable[..., Any], message: type[M]) -> tuple[type[M], Any]:
    signature = inspect.signature(handler, eval_str=True)
    parameters = list(signature.parameters.values())
    declared = parameters[0].annotation if parameters else None
    if not (isinstance(declared, type) and issubclass(declared, message)):
        raise RegistrationError(
            f"{handler.__qualname__}: the first parameter must be annotated with a "
            f"{message.__name__}"
        )
    if signature.return_annotation is inspect.Signature.empty:
        raise RegistrationError(f"{handler.__qualname__} must declare what it returns")
    return declared, signature.return_annotation


def _summary(declared: Any) -> str:
    if not declared.__doc__:
        raise RegistrationError(
            f"{declared.__qualname__} needs a docstring: it is the documentation"
        )
    return inspect.cleandoc(declared.__doc__)
