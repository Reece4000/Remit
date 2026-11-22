"""References: fields that hold the id of another document.

Marking a field with the kind of document its id belongs to does two things. The
registry describes the field as a reference, so a screen can offer a choice of those
documents and link to the one chosen, and an agent knows what to look up. And the
kernel refuses to write a document whose reference leads nowhere: documents are never
deleted, so a reference that was sound when written stays sound.

A reference can also say which of those documents are worth offering, given what is
beside it, as a lookup in an accounting system is narrowed by the record it is on.
"""

from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from functools import cache
from types import MappingProxyType, NoneType, UnionType
from typing import Annotated, Any, Union, get_args, get_origin
from uuid import UUID

from pydantic import BaseModel, GetJsonSchemaHandler
from pydantic.json_schema import JsonSchemaValue
from pydantic_core import CoreSchema

_SCHEMA_KEY = "x-references"
_WHERE_KEY = "x-where"


@dataclass(frozen=True)
class References:
    """Marks a field as holding the id of a document, as registered.

    `party_id: Annotated[UUID, References("parties.party")]`. The document is named as
    the registry names it, which is also how it is stored, so the name is as stable as
    the stored documents are. It must be a document of the module declaring the field,
    or of one it requires; the registry checks that at startup. The mark goes on the
    whole field, which may hold an id, an optional id, or a tuple or list of ids.

    `where` narrows the documents a screen offers, and an agent looks among, to those
    whose field holds what a path leads to:

        sales_order_line_id: Annotated[UUID, References(
            "sales.sales_order_line", where={"sales_order_id": "purchase_order_id.sales_order_id"}
        )]

    offers the lines of the sales order the purchase order was raised for. A path starts
    at a field beside the reference and may go on, once, into the document that field
    refers to. One that leads to nothing narrows nothing, so a purchase order raised for
    stock is offered the lines of every order. It narrows what is offered, not what is
    accepted: the command still decides that. The registry checks at startup that each
    path can be followed and that the documents offered hold the field compared.
    """

    document: str
    where: Mapping[str, str] = field(default_factory=dict[str, str])

    def __post_init__(self) -> None:
        object.__setattr__(self, "where", MappingProxyType(dict(self.where)))

    def __hash__(self) -> int:
        return hash((self.document, tuple(self.where.items())))

    def __get_pydantic_json_schema__(
        self, core_schema: CoreSchema, handler: GetJsonSchemaHandler
    ) -> JsonSchemaValue:
        schema = {**handler(core_schema), _SCHEMA_KEY: self.document}
        if self.where:
            schema[_WHERE_KEY] = dict(self.where)
        return schema


@dataclass(frozen=True)
class Found:
    """A reference held by a document, and where in the document it sits."""

    path: tuple[str | int, ...]
    document: str
    id: UUID


def declared(model: type[BaseModel]) -> Iterator[tuple[str, str]]:
    """Where the model and the models nested in it declare references, and to what."""
    for name, info in model.model_fields.items():
        for mark in info.metadata:
            if isinstance(mark, References):
                yield f"{model.__name__}.{name}", mark.document
        for nested in _models_in(info.annotation):
            yield from declared(nested)


def misplaced(model: type[BaseModel]) -> str | None:
    """Why a reference mark in the model, or a model nested in it, cannot be followed.

    The kernel follows a mark on a whole field that holds an id, an optional id, or a
    tuple or list of ids. A mark on part of a field's type, as in
    `Annotated[UUID, References(...)] | None`, is refused rather than quietly unchecked.
    """
    for name, info in model.model_fields.items():
        where = f"{model.__name__}.{name}"
        if _marked_within(info.annotation):
            return (
                f"{where} marks part of its type as a reference; mark the whole field "
                'instead, as in Annotated[UUID | None, References("parties.party")]'
            )
        marked = any(isinstance(mark, References) for mark in info.metadata)
        if marked and not _holds_ids(info.annotation):
            return f"{where} is marked as a reference but does not hold ids"
        for nested in _models_in(info.annotation):
            if reason := misplaced(nested):
                return reason
    return None


def unnarrowable(model: type[BaseModel], documents: Mapping[str, type[BaseModel]]) -> str | None:
    """Why a reference's `where`, in the model or a model nested in it, cannot be followed.

    `documents` are the documents registered, by name. A document not among them is
    reported by the check of what references lead to, so it is passed over here.
    """
    for name, info in model.model_fields.items():
        where = f"{model.__name__}.{name}"
        for mark in info.metadata:
            if not isinstance(mark, References):
                continue
            offered = documents.get(mark.document)
            for compared, path in mark.where.items():
                if offered is not None and compared not in offered.model_fields:
                    return f"{where} is narrowed by {compared}, which {mark.document} does not hold"
                if reason := _unfollowable(model, path, documents):
                    return f"{where} is narrowed by {path}, {reason}"
        for nested in _models_in(info.annotation):
            if reason := unnarrowable(nested, documents):
                return reason
    return None


def _unfollowable(
    model: type[BaseModel], path: str, documents: Mapping[str, type[BaseModel]]
) -> str | None:
    """Why a path cannot be followed from the model, if it cannot."""
    first, *rest = path.split(".")
    if first not in model.model_fields:
        return f"but {model.__name__} has no field {first}"
    if not rest:
        return None
    if len(rest) > 1:
        return "but a path goes into one document at most"
    through = _refers_once(model, first)
    if through is None:
        return f"but {first} does not hold the id of a document"
    held = documents.get(through)
    if held is not None and rest[0] not in held.model_fields:
        return f"but {through} has no field {rest[0]}"
    return None


def _refers_once(model: type[BaseModel], field: str) -> str | None:
    """The document a field holds the id of, if it holds one id or none."""
    info = model.model_fields[field]
    annotation = info.annotation
    union = get_origin(annotation) in (Union, UnionType)
    if annotation is not UUID and not (union and set(get_args(annotation)) == {UUID, NoneType}):
        return None
    return next((mark.document for mark in info.metadata if isinstance(mark, References)), None)


def refers_to(model: type[BaseModel], field: str) -> str | None:
    """The document a field of the model holds the id of, if it holds exactly one.

    Not an optional id, nor a tuple or list of ids: one document, always there.
    """
    info = model.model_fields[field]
    if info.annotation is not UUID:
        return None
    return next((mark.document for mark in info.metadata if isinstance(mark, References)), None)


def references(value: BaseModel) -> list[Found]:
    """Every reference a model holds, at any depth, with where it sits."""
    return list(_found(value, ()))


def _found(value: object, path: tuple[str | int, ...]) -> Iterator[Found]:
    if isinstance(value, BaseModel):
        if not _carries(type(value)):
            return
        for name, info in type(value).model_fields.items():
            held = getattr(value, name)
            mark = next((m for m in info.metadata if isinstance(m, References)), None)
            if mark is None:
                yield from _found(held, (*path, name))
            else:
                yield from _ids(held, mark.document, (*path, name))
    elif isinstance(value, (tuple, list)):
        items: list[object] = list(value)  # pyright: ignore[reportUnknownArgumentType]
        for number, item in enumerate(items):
            yield from _found(item, (*path, number))


def _ids(held: object, document: str, path: tuple[str | int, ...]) -> Iterator[Found]:
    if isinstance(held, UUID):
        yield Found(path, document, held)
    elif isinstance(held, (tuple, list)):
        items: list[object] = list(held)  # pyright: ignore[reportUnknownArgumentType]
        for number, item in enumerate(items):
            yield from _ids(item, document, (*path, number))


@cache
def _carries(model: type[BaseModel]) -> bool:
    return next(declared(model), None) is not None


def _holds_ids(annotation: Any) -> bool:
    """An id, an optional id, or a tuple or list of ids."""
    if annotation is UUID:
        return True
    origin = get_origin(annotation)
    arguments = [a for a in get_args(annotation) if a not in (NoneType, Ellipsis)]
    if origin in (Union, UnionType):
        return all(_holds_ids(a) for a in arguments)
    return origin in (tuple, list) and bool(arguments) and all(a is UUID for a in arguments)


def _marked_within(annotation: Any) -> bool:
    if get_origin(annotation) is Annotated:
        if any(isinstance(mark, References) for mark in annotation.__metadata__):
            return True
        return _marked_within(get_args(annotation)[0])
    return any(_marked_within(argument) for argument in get_args(annotation))


def _models_in(annotation: Any) -> Iterator[type[BaseModel]]:
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        yield annotation
        return
    for argument in get_args(annotation):
        yield from _models_in(argument)
