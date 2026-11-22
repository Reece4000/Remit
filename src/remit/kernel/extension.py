"""Extensions: fields a module adds to a document another module owns.

Purchasing adds a default supplier and a lead time to the standard item; a business's
own module adds a storage temperature. The module that owns the item knows nothing of
either. The fields live in the item's own stored body, beside the item's fields, so the
item keeps one version and one history, and a listing filters, orders and searches the
added fields as it does the item's. Screens show one item with every module's fields.

Each module writes only the fields it declares. The kernel keeps an extension's fields
as they are stored whenever the owning module writes its document, and the document's
own fields whenever the extending module writes its extension.
"""

import inspect
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Annotated, Any, ClassVar
from uuid import UUID

from pydantic import BaseModel, ConfigDict, GetJsonSchemaHandler, create_model
from pydantic.json_schema import JsonSchemaValue
from pydantic_core import CoreSchema

from .document import Document

_SCHEMA_KEY = "x-module"


class Extension[D: Document](BaseModel):
    """Fields a module adds to a document of a module it requires.

        @purchasing.extension
        class ItemPurchasing(Extension[items.Item]):
            '''What purchasing keeps about an item.'''

            lead_time_days: int = 0

    Every field needs a default, which documents written before the extension was
    installed read as holding. An extension adds fields and nothing else: no states or
    transitions, which stay the owning module's. A handler reads one with
    `context.extension` and changes it with `context.update` or `context.revise`.
    """

    model_config = ConfigDict(frozen=True, use_attribute_docstrings=True)

    searchable: ClassVar[tuple[str, ...]] = ()
    """Text fields of the extension that join the document's own in what `search` reads."""

    id: UUID
    """The id of the document extended."""
    version: int
    """The version of the document extended, as read. The extension's fields are the
    document's, so changing them moves it on."""


_VIEW = frozenset(Extension.model_fields)
"""The fields an extension's view takes from the document extended, not from the extension."""


def extends(kind: type[Extension[Any]]) -> type[Document] | None:
    """The document an extension names, as in `Extension[items.Item]`, if it names one."""
    for base in kind.__mro__:
        generic = getattr(base, "__pydantic_generic_metadata__", None)
        if generic and generic["origin"] is Extension:
            (named,) = generic["args"]
            return named if isinstance(named, type) and issubclass(named, Document) else None
    return None


def added(kind: type[Extension[Any]]) -> tuple[str, ...]:
    """The fields an extension adds to the document it extends, in order."""
    return tuple(name for name in kind.model_fields if name not in _VIEW)


def reserved(kind: type[Extension[Any]]) -> list[str]:
    """The fields every document has (`id`, `state` and `version`) that an extension
    declares itself, which would stand for the document's own."""
    below = kind.__mro__[: kind.__mro__.index(Extension)]
    named = {name for base in below for name in inspect.get_annotations(base)}
    return sorted(named & Document.model_fields.keys())


@dataclass(frozen=True)
class _Added:
    """Marks a field of an extended document with the module that added it."""

    module: str

    def __get_pydantic_json_schema__(
        self, core_schema: CoreSchema, handler: GetJsonSchemaHandler
    ) -> JsonSchemaValue:
        return {**handler(core_schema), _SCHEMA_KEY: self.module}


def extended[D: Document](
    document: type[D], extensions: Sequence[tuple[str, type[Extension[Any]]]]
) -> type[D]:
    """The document with the fields of each extension, given with the module that declares
    it, beside its own: what its stored body holds, and what is listed and described.

    Each added field's schema names that module under `x-module`. Searchable fields join
    the document's own. The schema keeps the document's title, since it describes the same
    document: a client matches what a command returns, such as an `Item`, to the kind by
    it. Only the model's name differs, so that an API's schemas can hold both.
    """
    fields: dict[str, Any] = {}
    for module, extension in extensions:
        for name in added(extension):
            info = extension.model_fields[name]
            fields[name] = Annotated[info.annotation, info, _Added(module)]
    model = create_model(
        f"Extended{document.__name__}", __base__=document, __doc__=document.__doc__, **fields
    )
    model.model_config["title"] = document.model_config.get("title", document.__name__)
    model.model_rebuild(force=True)
    model.searchable = (*document.searchable, *(f for _, e in extensions for f in e.searchable))
    return model
