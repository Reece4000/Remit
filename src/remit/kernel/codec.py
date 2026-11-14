"""Translation between the kernel's typed objects and the JSON a store holds."""

from collections.abc import Mapping
from typing import Any

from pydantic_core import to_jsonable_python

from .document import Document
from .errors import RemitError
from .message import Command, Event, Outcome, Recorded
from .module import CommandSpec
from .registry import Registry
from .store import Json, StoredDocument, StoredEvent


class Codec:
    def __init__(self, registry: Registry) -> None:
        self._registry = registry

    def dump(self, document: Document) -> StoredDocument:
        name = self._registry.document_name(type(document))
        body = document.model_dump(mode="json")
        if type(document).model_validate(body) != document:
            raise RemitError(f"{name} {document.id} does not survive serialisation")
        return StoredDocument(name, document.id, document.version, document.is_final, body)

    def load[D: Document](self, kind: type[D], body: Json) -> D:
        return kind.model_validate(body)

    def filters(self, equals: Mapping[str, object]) -> dict[str, object]:
        encoded = {field: to_jsonable_python(value) for field, value in equals.items()}
        for field, value in encoded.items():
            if isinstance(value, (dict, list)):
                raise ValueError(f"cannot filter on {field}: only scalar fields can be matched")
        return encoded

    def dump_event(self, event: Event) -> tuple[str, Json]:
        return self._registry.event_name(type(event)), event.model_dump(mode="json")

    def load_event(self, stored: StoredEvent) -> Recorded:
        event = self._registry.event_type(stored.name).model_validate(stored.body)
        return Recorded(stored.position, stored.name, event, stored.at, stored.actor)

    def dump_outcome(self, spec: CommandSpec, command: Command[Any], outcome: Outcome[Any]) -> Json:
        return {
            "command": command.model_dump(mode="json"),
            "result": spec.returns.dump_python(outcome.result, mode="json"),
            "documents": [
                [stored.kind, stored.body] for stored in map(self.dump, outcome.documents)
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
