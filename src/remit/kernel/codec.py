"""Translation between the kernel's typed objects and the JSON a store holds."""

from typing import Any

from .document import Document
from .errors import RemitError
from .message import Command, Event, Outcome, Recorded
from .module import CommandSpec
from .personal import DataKey, seal
from .registry import Registry
from .search import searched
from .store import Json, StoredDocument, StoredEvent


class Codec:
    def __init__(self, registry: Registry) -> None:
        self._registry = registry

    def dump(self, document: Document) -> StoredDocument:
        name = self._registry.document_name(type(document))
        body = document.model_dump(mode="json")
        if type(document).model_validate(body) != document:
            raise RemitError(f"{name} {document.id} does not survive serialisation")
        fields = type(document).searchable
        search = searched(body, fields) if fields else None
        return StoredDocument(name, document.id, document.version, document.is_final, body, search)

    def load[D: Document](self, kind: type[D], body: Json) -> D:
        return kind.model_validate(body)

    def dump_event(self, event: Event) -> tuple[str, Json]:
        return self._registry.event_name(type(event)), event.model_dump(mode="json")

    def load_event(self, stored: StoredEvent) -> Recorded:
        event = self._registry.event_type(stored.name).model_validate(stored.body)
        return Recorded(stored.position, stored.name, event, stored.at, stored.actor)

    def dump_outcome(
        self, spec: CommandSpec, command: Command[Any], outcome: Outcome[Any], key: DataKey
    ) -> Json:
        """What an idempotent command did, with its personal values sealed under `key`."""
        return {
            "command": seal(command, command.model_dump(mode="json"), key),
            "result": seal(
                outcome.result, spec.returns.dump_python(outcome.result, mode="json"), key
            ),
            "documents": [
                [stored.kind, seal(document, stored.body, key)]
                for document, stored in zip(
                    outcome.documents, map(self.dump, outcome.documents), strict=True
                )
            ],
            "events": [list(self.dump_event(event)) for event in outcome.events],
        }

    def load_outcome(self, spec: CommandSpec, record: Json) -> Outcome[Any]:
        return Outcome(
            result=spec.returns.validate_python(record["result"]),
            documents=tuple(
                self._registry.document_type(kind).model_validate(body)
                for kind, body in record["documents"]
            ),
            events=tuple(
                self._registry.event_type(name).model_validate(body)
                for name, body in record["events"]
            ),
            committed=True,
            replayed=True,
        )


def unstorable(value: object, path: str = "") -> str | None:
    """Where JSON holds text that no store can keep, if it does.

    PostgreSQL's JSON cannot hold a NUL character, and no store can encode a lone
    surrogate. Shared field types (`Name`, `SingleLine`) refuse them on input; this
    catches them in any other field, so that every store refuses the same writes.
    """
    if isinstance(value, str):
        for character in value:
            if character == "\x00" or "\ud800" <= character <= "\udfff":
                return (
                    f"{path or 'a value'} contains U+{ord(character):04X}, which cannot be stored"
                )
        return None
    if isinstance(value, dict):
        entries: dict[str, Any] = value  # pyright: ignore[reportUnknownVariableType]
        for name, part in entries.items():
            where = f"{path}.{name}" if path else name
            if problem := unstorable(name, where) or unstorable(part, where):
                return problem
    if isinstance(value, list):
        parts: list[Any] = value  # pyright: ignore[reportUnknownVariableType]
        for number, part in enumerate(parts):
            if problem := unstorable(part, f"{path}[{number}]"):
                return problem
    return None
