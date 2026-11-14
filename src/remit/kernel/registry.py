"""The registry: every declaration from every module, checked once at startup.

It is the single source the kernel resolves names from, and `describe()` is the single
source everything outside the kernel (API schema, agent tools, docs) is generated from.
"""

from collections.abc import Iterable
from typing import Any

from .document import Document
from .errors import RegistrationError
from .message import Command, Event
from .module import CommandSpec, Module, Subscription, installation_order


class Registry:
    def __init__(self, modules: Iterable[Module]) -> None:
        """Register the modules and everything they require, dependencies first."""
        self.modules: dict[str, Module] = {}
        self._documents: dict[type[Document], str] = {}
        self._events: dict[type[Event], str] = {}
        self._event_owners: dict[type[Event], Module] = {}
        self._commands: dict[type[Command[Any]], CommandSpec] = {}

        for module in installation_order(modules):
            if module.name in self.modules:
                raise RegistrationError(f"module {module.name!r} is installed twice")
            self.modules[module.name] = module
            for name, document in module.documents.items():
                _check_lifecycle(name, document)
                self._documents[document] = name
            for name, event in module.events.items():
                self._events[event] = name
                self._event_owners[event] = module
            for spec in module.commands.values():
                self._commands[spec.command] = spec

        self.subscriptions: tuple[Subscription, ...] = tuple(
            subscription
            for module in self.modules.values()
            for subscription in module.subscriptions
        )
        for subscription in self.subscriptions:
            self._check_reach(subscription)

    def document_name(self, kind: type[Document]) -> str:
        return _resolve(self._documents, kind, "document")

    def event_name(self, kind: type[Event]) -> str:
        return _resolve(self._events, kind, "event")

    def command(self, kind: type[Command[Any]]) -> CommandSpec:
        return _resolve(self._commands, kind, "command")

    def document_type(self, name: str) -> type[Document]:
        return _resolve({name: kind for kind, name in self._documents.items()}, name, "document")

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

    def describe(self) -> dict[str, Any]:
        """Everything installed, as plain JSON-ready data."""
        return {name: self._describe(module) for name, module in self.modules.items()}

    def _describe(self, module: Module) -> dict[str, Any]:
        return {
            "requires": [dependency.name for dependency in module.requires],
            "settings": module.settings.model_json_schema() if module.settings else None,
            "documents": {
                name: {
                    "summary": (kind.__doc__ or "").strip(),
                    "schema": kind.model_json_schema(),
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
            "commands": {
                name: {
                    "summary": spec.summary,
                    "permission": name,
                    "input": spec.command.model_json_schema(),
                    "returns": spec.returns.json_schema(),
                }
                for name, spec in module.commands.items()
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
    if unknown := [field for field in document.unique if field not in document.model_fields]:
        raise RegistrationError(f"{name} declares unknown fields {unknown} unique")
    for transition in lifecycle.transitions:
        unknown = [field for field in transition.requires if field not in document.model_fields]
        if unknown:
            raise RegistrationError(
                f"{name}: transition {transition.name!r} requires unknown fields {unknown}"
            )


def _resolve[K, V](declared: dict[K, V], kind: K, what: str) -> V:
    try:
        return declared[kind]
    except KeyError:
        raise RegistrationError(
            f"{getattr(kind, '__name__', kind)} is not a {what} of any installed module"
        ) from None
