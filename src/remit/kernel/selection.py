"""Selections: which documents a listing asks for, in what order, and from where.

A listing is described the same way whether Python or JSON asks for it:

    where={"state": "active", "posting_date": {"gte": "2026-01-01", "lt": "2026-02-01"}}
    search="acme"
    order="-posting_date"

`where` maps a field to the value it must equal, or to operators (`eq`, `ne`, `in`,
`gt`, `gte`, `lt`, `lte`) and what to compare with. Values are checked against the
field's type, so a date compares as a date and a misspelt field is refused rather than
matching nothing. `order` names one field, with `-` in front for descending; documents
that tie are ordered by id, which is also the order of their creation.

Pages are reached by cursor, not by number: `after` is the place the last page ended,
so documents added or removed between pages are neither skipped nor repeated.

What a selection means is defined here, by `Selection.arranged`. The memory store runs
that; the PostgreSQL store translates the selection to SQL with the same meaning, and
the kernel's tests hold the two to the same answers. Text compares by code point, the
one ordering every store and every platform agrees on.
"""

import base64
import hashlib
import json
import operator
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from functools import cache
from typing import Any, cast
from uuid import UUID

from pydantic import AwareDatetime, FiniteFloat, TypeAdapter, ValidationError

from .document import Document
from .errors import Problem, Rejected, said
from .search import MAX_LENGTH, holds, rank, searched, terms

DEFAULT_LIMIT = 50
MAX_LIMIT = 200

type Json = dict[str, Any]


class Comparison(StrEnum):
    """How a field's stored values compare with one another."""

    TEXT = "text"
    """By code point. Dates and ids are stored as text that orders correctly this way."""
    NUMBER = "number"
    INSTANT = "instant"
    """As points in time, whatever offset each was written with."""


_EQUALITY = ("eq", "ne", "in")
_RANGES = {"gt": operator.gt, "gte": operator.ge, "lt": operator.lt, "lte": operator.le}
OPERATORS = (*_EQUALITY, *_RANGES)


@dataclass(frozen=True)
class Column:
    """A field that holds a single value, as a listing can filter and order by it."""

    name: str
    comparison: Comparison | None
    """None when values can be matched but have no order, as with true and false."""
    operand: TypeAdapter[Any]
    """Checks what the field is compared with."""
    nullable: bool

    @property
    def operators(self) -> tuple[str, ...]:
        return OPERATORS if self.comparison else _EQUALITY


@dataclass(frozen=True)
class Condition:
    field: str
    operator: str
    value: Any
    """What to compare with, as the field is stored; a tuple of those for `in`."""
    comparison: Comparison | None


@dataclass(frozen=True)
class Page[D]:
    """One page of a listing."""

    items: tuple[D, ...]
    next: str | None
    """Pass as `after` to read the page that follows. None when this is the last."""


@dataclass(frozen=True)
class Selection:
    """What a store is asked for. Every value in it is JSON, as documents are stored."""

    kind: str
    where: tuple[Condition, ...] = ()
    searchable: tuple[str, ...] = ()
    terms: tuple[str, ...] = ()
    """Folded words, every one of which a document's searchable text must hold."""
    order: str = "id"
    comparison: Comparison = Comparison.TEXT
    descending: bool = False
    ranked: bool = False
    """Best match first, then by id: the order of a search that names no other."""
    after: tuple[Any, str] | None = None
    """The leading value and id of the document the last page ended on."""
    limit: int | None = None

    def arranged(self, bodies: Iterable[Json]) -> list[Json]:
        """The documents selected from `bodies`, in order. This is what a selection means."""
        found = sorted(filter(self.admits, bodies), key=self.place, reverse=self.descending)
        return found[: self.limit]

    def admits(self, body: Json) -> bool:
        if not all(_holds(condition, body) for condition in self.where):
            return False
        if self.terms and not holds(self.text(body), self.terms):
            return False
        if self.after is None:
            return True
        ended = self._placed(*self.after)
        return self.place(body) < ended if self.descending else self.place(body) > ended

    def place(self, body: Json) -> tuple[bool, Any, str]:
        """Where a document sorts: nulls after every value, ties by id."""
        return self._placed(self.leading(body), body["id"])

    def leading(self, body: Json) -> Any:
        """The value a document is ordered by before its id, as stored. None for id alone."""
        if self.ranked:
            return rank(self.text(body), self.terms)
        return None if self.order == "id" else body.get(self.order)

    def text(self, body: Json) -> str:
        return searched(body, self.searchable)

    def cursor(self, body: Json) -> str:
        """The place of this document in this listing, to pass back as `after`."""
        position = json.dumps([self._digest(), self.leading(body), body["id"]])
        return base64.urlsafe_b64encode(position.encode()).decode().rstrip("=")

    def resumed(self, cursor: str) -> Selection:
        """This selection, continuing after the place the cursor marks.

        A cursor is a place, not a permission: it carries nothing the reader could not
        have asked for outright, so it is checked for sense and not signed. One made
        for a different listing is refused.
        """
        try:
            padded = cursor + "=" * (-len(cursor) % 4)
            digest, leading, id = json.loads(base64.urlsafe_b64decode(padded))
            if digest != self._digest() or not isinstance(id, str):
                raise ValueError("made for another listing")
            if leading is not None and not self.ranked and self.order == "id":
                raise ValueError("a listing in order of id has no other place to resume from")
            comparison = Comparison.NUMBER if self.ranked else self.comparison
            return replace(self, after=(_stored(leading, comparison), str(UUID(id))))
        except ValueError, TypeError, InvalidOperation:
            raise _refusal(
                self.kind,
                [
                    Problem(
                        ("after",),
                        "is not a cursor from this listing; a cursor continues the listing "
                        "that gave it, with the same where, search and order",
                    )
                ],
            ) from None

    def _placed(self, leading: Any, id: str) -> tuple[bool, Any, str]:
        comparison = Comparison.NUMBER if self.ranked else self.comparison
        return leading is None, comparable(leading, comparison), id

    def _digest(self) -> str:
        asked = [
            self.kind,
            [[c.field, c.operator, c.value] for c in self.where],
            self.terms,
            self.order,
            self.descending,
            self.ranked,
        ]
        return hashlib.sha256(json.dumps(asked).encode()).hexdigest()[:16]


def select(
    name: str,
    kind: type[Document],
    *,
    where: Mapping[str, object] | None = None,
    search: str | None = None,
    order: str | None = None,
    after: str | None = None,
    limit: int | None = None,
) -> Selection:
    """The selection a listing of `kind` asks for, refusing what it cannot mean.

    Every fault is reported at once, each against the part of the request at fault.
    """
    problems: list[Problem] = []
    conditions = [
        condition
        for field, wanted in (where or {}).items()
        for condition in _conditions(kind, field, wanted, problems)
    ]

    wanted_terms: tuple[str, ...] = ()
    if search and not kind.searchable:
        problems.append(Problem(("search",), "cannot be searched; filter it with where instead"))
    elif search and len(search) > MAX_LENGTH:
        problems.append(Problem(("search",), f"is longer than {MAX_LENGTH} characters"))
    elif search:
        wanted_terms = terms(search)

    field, comparison, descending = "id", Comparison.TEXT, False
    if order is not None:
        descending, field = order.startswith("-"), order.removeprefix("-")
        column = columns(kind).get(field)
        if column is None or column.comparison is None:
            problems.append(Problem(("order",), f"cannot order by {field!r}; {_orders(kind)}"))
        else:
            comparison = column.comparison

    if limit is not None and not 1 <= limit <= MAX_LIMIT:
        problems.append(Problem(("limit",), f"must be between 1 and {MAX_LIMIT}"))
    if problems:
        raise _refusal(name, problems)

    selection = Selection(
        kind=name,
        where=tuple(conditions),
        searchable=kind.searchable,
        terms=wanted_terms,
        order=field,
        comparison=comparison,
        descending=descending,
        ranked=bool(wanted_terms) and order is None,
        limit=limit,
    )
    return selection if after is None else selection.resumed(after)


@cache
def columns(kind: type[Document]) -> dict[str, Column]:
    """The fields of a document that a listing can filter on."""
    found = (_column(name, info.annotation) for name, info in kind.model_fields.items())
    return {column.name: column for column in found if column}


def holds_text(annotation: Any) -> bool:
    """Whether a field holds text, or a tuple or list of it, which is what search reads."""
    schema, _ = _plain(TypeAdapter(annotation).core_schema)
    if schema["type"] == "tuple":
        items: list[Any] = schema["items_schema"]
    elif schema["type"] == "list":
        items = [schema["items_schema"]]
    else:
        return schema["type"] == "str"
    return all(_plain(item)[0]["type"] == "str" for item in items)


def listing(kind: type[Document]) -> dict[str, Any]:
    """How a document can be listed, for the registry to describe."""
    found = columns(kind)
    return {
        "filters": {name: list(column.operators) for name, column in found.items()},
        "orders": [name for name, column in found.items() if column.comparison],
        "searchable": list(kind.searchable),
        "limit": {"default": DEFAULT_LIMIT, "maximum": MAX_LIMIT},
    }


def comparable(value: Any, comparison: Comparison) -> Any:
    """A stored value as something that orders the way its field does."""
    if value is None or comparison is Comparison.TEXT:
        return value
    if comparison is Comparison.NUMBER:
        return Decimal(str(value))
    return datetime.fromisoformat(value)


def _stored(value: Any, comparison: Comparison) -> Any:
    """A value from a cursor, refused unless a field compared this way could hold it."""
    expected = (int, float, str) if comparison is Comparison.NUMBER else str
    if value is not None and (isinstance(value, bool) or not isinstance(value, expected)):
        raise TypeError("not a value of this field")
    place = comparable(value, comparison)
    if isinstance(place, Decimal) and not place.is_finite():
        raise ValueError("not a number that has a place in an order")
    return value


def _holds(condition: Condition, body: Json) -> bool:
    stored = body.get(condition.field)
    if condition.operator == "in":
        return any(_same(stored, value, condition.comparison) for value in condition.value)
    if condition.operator in _EQUALITY:
        return _same(stored, condition.value, condition.comparison) == (condition.operator == "eq")
    if stored is None or condition.comparison is None:
        return False
    compare = _RANGES[condition.operator]
    return compare(
        comparable(stored, condition.comparison),
        comparable(condition.value, condition.comparison),
    )


def _same(stored: Any, value: Any, comparison: Comparison | None) -> bool:
    if stored is None or value is None or comparison is None:
        return stored == value
    return comparable(stored, comparison) == comparable(value, comparison)


def _conditions(
    kind: type[Document], field: str, wanted: object, problems: list[Problem]
) -> list[Condition]:
    path = ("where", field)
    column = columns(kind).get(field)
    if column is None:
        reason = (
            "holds more than one value, so it cannot be filtered on"
            if field in kind.model_fields
            else "it has no such field"
        )
        problems.append(Problem(path, reason))
        return []

    tests = cast(Mapping[str, object], wanted) if isinstance(wanted, Mapping) else {"eq": wanted}
    conditions: list[Condition] = []
    for name, operand in tests.items():
        try:
            if name not in column.operators:
                allowed = ", ".join(column.operators)
                raise ValueError(f"cannot be compared with {name!r}; use one of {allowed}")
            if name == "in":
                if not isinstance(operand, (list, tuple)):
                    raise ValueError("`in` takes a list of values")
                values: list[object] = list(operand)  # pyright: ignore[reportUnknownArgumentType]
                value: Any = tuple(_operand(column, item) for item in values)
            elif name in _RANGES and operand is None:
                raise ValueError(f"cannot be compared with null using {name!r}")
            else:
                value = _operand(column, operand)
        except ValidationError as error:
            problems += [Problem((*path, name), said(found)) for found in error.errors()]
        except ValueError as error:
            problems.append(Problem((*path, name), str(error)))
        else:
            conditions.append(Condition(field, name, value, column.comparison))
    return conditions


def _operand(column: Column, value: object) -> Any:
    """What a field is compared with, checked against its type and as it would be stored."""
    if value is None:
        if not column.nullable:
            raise ValueError("is never null")
        return None
    return column.operand.dump_python(column.operand.validate_python(value), mode="json")


def _orders(kind: type[Document]) -> str:
    fields = [name for name, column in columns(kind).items() if column.comparison]
    return f"it can be ordered by {', '.join(fields)}"


def _refusal(name: str, problems: list[Problem]) -> Rejected:
    described = "; ".join(f"{'.'.join(map(str, p.path))}: {p.message}" for p in problems)
    return Rejected(f"cannot list {name}: {described}", problems=problems)


_WRAPPERS = frozenset({"nullable", "default", "function-after", "function-before", "function-wrap"})

_SCALARS: dict[str, tuple[Comparison | None, Any]] = {
    "str": (Comparison.TEXT, str),
    "uuid": (Comparison.TEXT, UUID),
    "date": (Comparison.TEXT, date),
    "int": (Comparison.NUMBER, int),
    "float": (Comparison.NUMBER, FiniteFloat),
    "decimal": (Comparison.NUMBER, Decimal),
    "bool": (None, bool),
}


def _plain(schema: Any) -> tuple[dict[str, Any], bool]:
    """The schema of the value itself, inside whatever makes it optional or checks it.

    Read from pydantic's core schema, which says what a field validates to whatever the
    annotation that produced it looked like.
    """
    nullable = False
    while schema["type"] in _WRAPPERS:
        nullable = nullable or schema["type"] == "nullable"
        schema = schema["schema"]
    return schema, nullable


def _column(name: str, annotation: Any) -> Column | None:
    schema, nullable = _plain(TypeAdapter(annotation).core_schema)
    kind = schema["type"]
    if kind in _SCALARS:
        comparison, operand = _SCALARS[kind]
    elif kind == "enum":
        ordered = {"str": Comparison.TEXT, "int": Comparison.NUMBER}
        comparison, operand = ordered.get(schema.get("sub_type", "")), schema["cls"]
    elif kind == "datetime":
        # A time without a zone is not a point in time, so it can be matched but not ordered.
        aware = schema.get("tz_constraint") == "aware"
        comparison, operand = (Comparison.INSTANT, AwareDatetime) if aware else (None, datetime)
    else:
        return None
    return Column(name, comparison, _adapter(operand), nullable)


@cache
def _adapter(kind: Any) -> TypeAdapter[Any]:
    return TypeAdapter(kind)
