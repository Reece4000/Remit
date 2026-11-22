"""Personal data: marking it, and sealing it wherever the kernel keeps a permanent copy.

See docs/personal-data.md. Fields marked `Personal` are encrypted in the audit trail and
in idempotency records, under a data key linked to the people they are about. Deleting
those keys erases the data without altering the records that held it.
"""

import base64
import json
import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, is_dataclass
from functools import cache
from typing import Annotated, Any, cast, get_args, get_origin, get_type_hints, is_typeddict
from uuid import UUID, uuid7

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from pydantic import BaseModel
from pydantic.fields import FieldInfo

ERASED = "[erased]"
"""What a personal value reads as once it has been erased."""

_SEALED = "$sealed"
_NONCE = 12


@dataclass(frozen=True)
class _PersonalMark:
    def __repr__(self) -> str:
        return "Personal"


Personal = _PersonalMark()
"""Marks a field as personal data: `email: Annotated[EmailStr | None, Personal]`."""


def is_personal(info: FieldInfo) -> bool:
    return any(isinstance(meta, _PersonalMark) for meta in info.metadata)


def holds_personal(info: FieldInfo) -> bool:
    """Whether a field can hold personal data: it is marked, or holds a model that can."""
    return is_personal(info) or any(
        carries_personal(nested) for nested in _models_in(info.annotation)
    )


@cache
def carries_personal(model: type[BaseModel]) -> bool:
    """Whether instances of this model can hold personal data, directly or nested."""
    return any(holds_personal(info) for info in model.model_fields.values())


def unsealable(model: type[BaseModel]) -> str | None:
    """Why personal data in this model could not be sealed, if it could not.

    Sealing follows fields, nested models, and tuples or lists of them, to any depth.
    Two things it cannot follow are refused rather than stored in the clear: personal
    data inside a mapping or a set, and a mark on part of a field's type (such as
    `Annotated[str, Personal] | None`) instead of on the field.
    """
    for name, info in model.model_fields.items():
        if _marked_within(info.annotation):
            return (
                f"{model.__name__}.{name} marks part of its type as personal; mark the whole "
                "field instead, as in Annotated[str | None, Personal]"
            )
        for container, nested in _containers(info.annotation):
            if container and carries_personal(nested):
                return f"{model.__name__}.{name} holds personal data inside a {container}"
        for nested in _models_in(info.annotation):
            if reason := unsealable(nested):
                return reason
    return None


def unsealable_result(annotation: Any) -> str | None:
    """Why personal data in a value of this type could not be sealed, if it could not.

    For what a command returns, which is kept in its idempotency record. Sealing follows
    models and tuples or lists of them; anything else that could carry personal data (a
    mapping, a set, a dataclass, a bare value marked personal) is refused.
    """
    if _marked_within(annotation):
        return "is marked personal itself; return the document the value belongs to"
    return _unsealable_in(annotation, "", set())


def _unsealable_in(annotation: Any, within: str, seen: set[int]) -> str | None:
    if id(annotation) in seen:
        return None
    seen.add(id(annotation))
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        if within and carries_personal(annotation):
            return f"holds personal data inside a {within}"
        return unsealable(annotation)
    if _is_record(annotation):
        record = cast(type[Any], annotation)
        hints = get_type_hints(record, include_extras=True).values()
        inner = within or record.__name__
        return next((r for h in hints if (r := _unsealable_in(h, inner, seen))), None)
    origin = get_origin(annotation)
    if origin is None:
        return None
    if isinstance(origin, type) and issubclass(origin, (Mapping, set, frozenset)):
        within = within or origin.__name__
    arguments = get_args(annotation)
    return next((r for a in arguments if (r := _unsealable_in(a, within, seen))), None)


def _is_record(annotation: Any) -> bool:
    """A dataclass or TypedDict: a type with fields that sealing cannot follow."""
    return isinstance(annotation, type) and (
        is_dataclass(annotation) or is_typeddict(cast(Any, annotation))
    )


def _marked_within(annotation: Any) -> bool:
    if get_origin(annotation) is Annotated:
        if any(isinstance(meta, _PersonalMark) for meta in annotation.__metadata__):
            return True
        return _marked_within(get_args(annotation)[0])
    return any(_marked_within(argument) for argument in get_args(annotation))


def _models_in(annotation: Any) -> list[type[BaseModel]]:
    return [nested for _, nested in _containers(annotation)]


def _containers(annotation: Any, within: str = "") -> list[tuple[str, type[BaseModel]]]:
    """Every model type in an annotation, with the unsupported container it sits in."""
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return [(within, annotation)]
    origin = get_origin(annotation)
    if origin is None:
        return []
    if isinstance(origin, type) and issubclass(origin, (Mapping, set, frozenset)):
        within = within or origin.__name__
    return [found for argument in get_args(annotation) for found in _containers(argument, within)]


def is_erased(value: object) -> bool:
    """Whether a personal value holds nothing: null, empty, `ERASED`, or only those."""
    if isinstance(value, (tuple, list)):
        items: list[object] = list(value)  # pyright: ignore[reportUnknownArgumentType]
        return all(is_erased(item) for item in items)
    return value is None or value == "" or value == ERASED


def unerased(value: object, path: str = "") -> list[str]:
    """Where personal values that have not been erased sit in a model, at any depth."""
    if isinstance(value, BaseModel):
        found: list[str] = []
        for name, info in type(value).model_fields.items():
            part = getattr(value, name)
            where = f"{path}.{name}" if path else name
            if is_personal(info):
                found += [] if is_erased(part) else [where]
            else:
                found += unerased(part, where)
        return found
    if isinstance(value, (tuple, list)):
        items: list[object] = list(value)  # pyright: ignore[reportUnknownArgumentType]
        return [
            where
            for number, item in enumerate(items)
            for where in unerased(item, f"{path}[{number}]")
        ]
    return []


@dataclass
class DataKey:
    """The key one unit of work seals personal values with, and the people it covers."""

    id: UUID = field(default_factory=uuid7)
    secret: bytes = field(default_factory=lambda: AESGCM.generate_key(bit_length=256))
    used: bool = False

    def seal(self, value: Any) -> dict[str, str]:
        self.used = True
        nonce = os.urandom(_NONCE)
        plain = json.dumps(value, separators=(",", ":")).encode()
        sealed = nonce + AESGCM(self.secret).encrypt(nonce, plain, self.id.bytes)
        return {_SEALED: base64.b64encode(sealed).decode()}


def seal(value: Any, dumped: Any, key: DataKey) -> Any:
    """`dumped` (the JSON form of `value`) with every personal value sealed under `key`.

    `value` may be a model, a tuple or list of them nested to any depth, or anything
    else, which is returned as it is. Values that hold nothing personal (null, empty,
    `ERASED`) are left in the clear, so a record of an erasure needs no key.
    """
    if isinstance(value, BaseModel):
        if not carries_personal(type(value)):
            return dumped
        fields: dict[str, Any] = dict(dumped)  # pyright: ignore[reportUnknownArgumentType]
        for name, info in type(value).model_fields.items():
            part = getattr(value, name)
            if is_personal(info):
                if not is_erased(part):
                    fields[name] = key.seal(fields[name])
            else:
                fields[name] = seal(part, fields[name], key)
        return fields
    if isinstance(value, (tuple, list)):
        items: list[Any] = list(value)  # pyright: ignore[reportUnknownArgumentType]
        return [seal(item, part, key) for item, part in zip(items, dumped, strict=True)]
    if isinstance(value, Mapping):
        entries: Mapping[Any, Any] = value  # pyright: ignore[reportUnknownVariableType]
        if any(_holds_personal(item) for item in entries.values()):
            raise TypeError("personal data inside a mapping cannot be sealed; use a tuple")
    return dumped


def _holds_personal(value: Any) -> bool:
    if isinstance(value, BaseModel):
        return carries_personal(type(value))
    parts: list[Any] = []
    if isinstance(value, Mapping):
        parts = list(value.values())  # pyright: ignore[reportUnknownArgumentType, reportUnknownMemberType]
    elif isinstance(value, (tuple, list)):
        parts = list(value)  # pyright: ignore[reportUnknownArgumentType]
    return any(_holds_personal(part) for part in parts)


def unseal(value: Any, secret_for: Callable[[UUID], bytes | None], key_id: UUID | None) -> Any:
    """`value` with sealed values opened, or read as `ERASED` if their key is gone."""
    if isinstance(value, dict):
        entries: dict[str, Any] = value  # pyright: ignore[reportUnknownVariableType]
        if key_id is not None and set(entries) == {_SEALED}:
            return _open(entries[_SEALED], secret_for(key_id), key_id)
        return {name: unseal(part, secret_for, key_id) for name, part in entries.items()}
    if isinstance(value, list):
        parts: list[Any] = value  # pyright: ignore[reportUnknownVariableType]
        return [unseal(part, secret_for, key_id) for part in parts]
    return value


def _open(sealed: str, secret: bytes | None, key_id: UUID) -> Any:
    if secret is None:
        return ERASED
    raw = base64.b64decode(sealed)
    return json.loads(AESGCM(secret).decrypt(raw[:_NONCE], raw[_NONCE:], key_id.bytes))
