"""Modules: how a piece of the business declares itself to the kernel.

A module is a plain object that collects document types, events, command handlers and
subscriptions. Everything else (permissions, the API, agent tools, documentation) is
derived from these declarations, so each one must carry a docstring.
"""

import inspect
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, cast

from pydantic import BaseModel, TypeAdapter

from .document import Document
from .errors import RegistrationError
from .message import Command, Event

if TYPE_CHECKING:
    from .context import Context

type Handler[M, R] = Callable[[M, Context], R]


@dataclass(frozen=True)
class Migration:
    """SQL a PostgreSQL store applies exactly once, identified by its name."""

    name: str
    sql: str


@dataclass(frozen=True)
class CommandSpec:
    name: str
    command: type[Command[Any]]
    handler: Handler[Any, Any]
    returns: TypeAdapter[Any]
    summary: str


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
        self.documents[self._claim(kind.__name__, self.documents)] = kind
        return kind

    def event[E: Event](self, kind: type[E]) -> type[E]:
        """Declare an event this module publishes."""
        _summary(kind)
        self.events[self._claim(kind.__name__, self.events)] = kind
        return kind

    def command[C: Command[Any], R](self, handler: Handler[C, R]) -> Handler[C, R]:
        """Declare a command by its handler; the command type is the first parameter's."""
        command, returns = _signature(handler)
        name = self._claim(command.__name__, self.commands)
        self.commands[name] = CommandSpec(name, command, handler, returns, _summary(handler))
        return handler

    def subscribe[E: Event](self, event: type[E]) -> Callable[[Handler[E, None]], Handler[E, None]]:
        """Declare a reaction to an event, from this module or any other."""

        def declare(handler: Handler[E, None]) -> Handler[E, None]:
            name = f"{self.name}.{handler.__name__}"
            self.subscriptions.append(
                Subscription(name, self.name, event, handler, _summary(handler))
            )
            return handler

        return declare

    def _claim(self, class_name: str, taken: dict[str, Any]) -> str:
        name = f"{self.name}.{re.sub(r'(?<!^)(?=[A-Z])', '_', class_name).lower()}"
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


def _signature(handler: Callable[..., Any]) -> tuple[type[Command[Any]], TypeAdapter[Any]]:
    signature = inspect.signature(handler, eval_str=True)
    parameters = list(signature.parameters.values())
    command = parameters[0].annotation if parameters else None
    if not (isinstance(command, type) and issubclass(command, Command)):
        raise RegistrationError(
            f"{handler.__qualname__}: the first parameter must be annotated with a Command"
        )
    if signature.return_annotation is inspect.Signature.empty:
        raise RegistrationError(f"{handler.__qualname__} must declare what it returns")
    return cast(type[Command[Any]], command), TypeAdapter(signature.return_annotation)


def _summary(declared: Any) -> str:
    if not declared.__doc__:
        raise RegistrationError(
            f"{declared.__qualname__} needs a docstring: it is the documentation"
        )
    return inspect.cleandoc(declared.__doc__)
