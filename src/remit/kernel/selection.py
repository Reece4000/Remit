"""Selections: which documents a listing asks for, in what order, and from where.

A listing is described the same way whether Python or JSON asks for it:

    where={"state": "active", "posting_date": {"gte": "2026-01-01", "lt": "2026-02-01"}}
    search="acme"
    order="-posting_date,code"

`where` maps a field to the value it must equal, or to operators (`eq`, `ne`, `in`,
`gt`, `gte`, `lt`, `lte`) and what to compare with. Values are checked against the
field's type, so a date compares as a date and a misspelt field is refused rather than
matching nothing. `order` names fields separated by commas, most significant first,
each with `-` in front for descending. Documents that tie on all of them are ordered by
id, which is also the order of their creation: in the direction of the last field
named, or as `id` is named itself, which ends the order.

A money field is compared with money, `{"currency": "GBP", "amount": "10.00"}`, which
`where` reads as the money to equal unless it names an operator. It is ordered by
currency and then by amount, so a listing in one currency is ordered by
amount and one in several is grouped by currency. `eq`, `ne` and `in` compare both
exactly; `gt`, `gte`, `lt` and `lte` find only amounts in the currency compared with,
since an amount in one currency is neither more nor less than an amount in another.

Pages are reached by cursor, not by number: `after` is the place the last page ended,
so documents added or removed between pages are neither skipped nor repeated. Every
page says how many documents the listing holds in all.

The values one field holds across a listing can be counted, each value once with how
many documents hold it, for a screen to offer as the choices of a filter (`tally`).

A document is listed as the kernel reads it: a field its stored body holds nothing for,
because the field was added to its kind or by an extension after it was written, is
filtered, ordered, searched and counted as holding its default.

What a selection means is defined here, by `Selection.arranged`, and what a tally
means by `Tally.counted`. The memory store runs those; the PostgreSQL store translates
them to SQL with the same meaning, and the kernel's tests hold the two to the same
answers. Text compares by code point, the one ordering every store and every platform
agrees on.
"""

import base64
import hashlib
import json
import operator
from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from functools import cache, cmp_to_key
from typing import Any, cast
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, FiniteFloat, TypeAdapter, ValidationError

from .document import Document
from .errors import Problem, Rejected, said
from .money import Money
from .search import MAX_LENGTH, holds, rank, searched, terms

DEFAULT_LIMIT = 50
MAX_LIMIT = 200
DEFAULT_VALUES = 100
MAX_VALUES = 500

type Json = dict[str, Any]


class Comparison(StrEnum):
    """How a field's stored values compare with one another."""

    TEXT = "text"
    """By code point. Dates and ids are stored as text that orders correctly this way."""
    NUMBER = "number"
    INSTANT = "instant"
    """As points in time, whatever offset each was written with."""
    MONEY = "money"
    """By currency code, then by amount. Amounts compare as more or less only within one
    currency."""


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
class Key:
    """A field a listing is ordered by."""

    field: str
    comparison: Comparison
    descending: bool = False


@dataclass(frozen=True)
class Page[D]:
    """One page of a listing."""

    items: tuple[D, ...]
    next: str | None
    """Pass as `after` to read the page that follows. None when this is the last."""
    total: int
    """How many documents the listing holds, on every page together: all that its
    `where` and `search` find, wherever the page starts and however long it is."""


@dataclass(frozen=True)
class Counted:
    """A value a field holds, and how many documents of a listing hold it."""

    value: Any
    """As the documents hold it in JSON."""
    count: int
    """How many documents filtering on the value finds."""


@dataclass(frozen=True)
class Values:
    """The values one field holds across a listing: null first, then lowest to highest."""

    items: tuple[Counted, ...]
    more: bool
    """Whether there are values beyond these, which the limit left out."""


@dataclass(frozen=True)
class Selection:
    """What a store is asked for. Every value in it is JSON, as documents are stored."""

    kind: str
    defaults: Mapping[str, Any]
    """What each field with a default holds, as stored, where a body holds nothing for it."""
    where: tuple[Condition, ...] = ()
    searchable: tuple[str, ...] = ()
    terms: tuple[str, ...] = ()
    """Folded words, every one of which a document's searchable text must hold."""
    order: tuple[Key, ...] = ()
    """The fields documents are ordered by before their id, most significant first."""
    newest_first: bool = False
    """Whether documents that tie on every field are ordered by id descending."""
    ranked: bool = False
    """Best match first, then by id: the order of a search that names no other."""
    after: tuple[tuple[Any, ...], str] | None = None
    """The leading values and id of the document the last page ended on."""
    limit: int | None = None

    def arranged(self, bodies: Iterable[Json]) -> list[Json]:
        """The documents selected from `bodies`, in order. This is what a selection means."""
        placed = cmp_to_key(self._compared)
        found = sorted(filter(self.admits, bodies), key=lambda body: placed(self.place(body)))
        return found[: self.limit]

    def matches(self, body: Json) -> bool:
        """Whether a document is one the listing holds, on whichever page."""
        if not all(_holds(c, self.held(body, c.field)) for c in self.where):
            return False
        return not self.terms or holds(self.text(body), self.terms)

    def admits(self, body: Json) -> bool:
        """Whether a document is one the listing holds, after the place the last page ended."""
        if not self.matches(body):
            return False
        return self.after is None or self._compared(self.place(body), self._placed(*self.after)) > 0

    def place(self, body: Json) -> tuple[Any, ...]:
        """Where a document sorts: by each leading value, nulls after values, then by id."""
        return self._placed(self.leading(body), body["id"])

    def leading(self, body: Json) -> tuple[Any, ...]:
        """The values a document is ordered by before its id, as stored."""
        if self.ranked:
            return (rank(self.text(body), self.terms),)
        return tuple(self.held(body, key.field) for key in self.order)

    def held(self, body: Json, field: str) -> Any:
        """What a document holds in a field, as stored: its default if its body has none."""
        return body.get(field, self.defaults.get(field))

    def text(self, body: Json) -> str:
        return searched({**self.defaults, **body}, self.searchable)

    def cursor(self, body: Json) -> str:
        """The place of this document in this listing, to pass back as `after`."""
        position = json.dumps([self._digest(), list(self.leading(body)), body["id"]])
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
            if digest != self._digest() or not isinstance(id, str) or not isinstance(leading, list):
                raise ValueError("made for another listing")
            # A strict zip refuses a cursor without one value for each the listing orders by.
            values = zip(cast(list[Any], leading), self._comparisons(), strict=True)
            ended = tuple(_stored(value, comparison) for value, (comparison, _) in values)
            return replace(self, after=(ended, str(UUID(id))))
        except ValueError, TypeError, InvalidOperation:
            raise _refusal(
                f"cannot list {self.kind}",
                [
                    Problem(
                        ("after",),
                        "is not a cursor from this listing; a cursor continues the listing "
                        "that gave it, with the same where, search and order",
                    )
                ],
            ) from None

    def _comparisons(self) -> tuple[tuple[Comparison, bool], ...]:
        """How each leading value compares, and whether it is ordered from the highest down."""
        if self.ranked:
            return ((Comparison.NUMBER, False),)
        return tuple((key.comparison, key.descending) for key in self.order)

    def _placed(self, leading: tuple[Any, ...], id: str) -> tuple[Any, ...]:
        values = zip(leading, self._comparisons(), strict=True)
        return (*((value is None, comparable(value, c)) for value, (c, _) in values), id)

    def _compared(self, one: tuple[Any, ...], other: tuple[Any, ...]) -> int:
        """Below zero when the place `one` comes before `other` in this listing, above when
        it comes after: by the first value they differ in, each in its own direction."""
        directions = (*(descending for _, descending in self._comparisons()), self.newest_first)
        for mine, theirs, descending in zip(one, other, directions, strict=True):
            if mine != theirs:
                return -1 if (mine < theirs) != descending else 1
        return 0

    def _digest(self) -> str:
        asked = [
            self.kind,
            [[c.field, c.operator, c.value] for c in self.where],
            self.terms,
            [[key.field, key.descending] for key in self.order],
            self.newest_first,
            self.ranked,
        ]
        return hashlib.sha256(json.dumps(asked).encode()).hexdigest()[:16]


@dataclass(frozen=True)
class Tally:
    """What a store is asked to count: the values one field holds across a selection."""

    selection: Selection
    field: str
    comparison: Comparison | None
    """How the field's values compare, which decides when two are the same value."""
    limit: int

    def counted(self, bodies: Iterable[Json]) -> list[tuple[Any, int]]:
        """The values of the field in the documents the selection matches, each with how
        many hold it: null first, then lowest to highest. This is what a tally means.

        Values a filter finds equal are one value, so that each count is how many
        documents filtering on that value finds; of the ways it is written, such as 10.5
        and 10.50, it is given as the first in code point order. Values that have no
        order, such as true and false, are ordered as their text.
        """
        held: defaultdict[Any, list[Any]] = defaultdict(list)
        for body in filter(self.selection.matches, bodies):
            value = self.selection.held(body, self.field)
            held[comparable(value, self.comparison or Comparison.TEXT)].append(value)
        ordered = sorted(held.items(), key=lambda found: (found[0] is not None, found[0]))
        return [(min(written, key=str), len(written)) for _, written in ordered][: self.limit]


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
    conditions = _where(kind, where, problems)
    wanted_terms = _search(kind, search, problems)
    keys, newest_first = _ordering(kind, order, problems)
    if limit is not None and not 1 <= limit <= MAX_LIMIT:
        problems.append(Problem(("limit",), f"must be between 1 and {MAX_LIMIT}"))
    if problems:
        raise _refusal(f"cannot list {name}", problems)

    selection = Selection(
        kind=name,
        defaults=defaults(kind),
        where=conditions,
        searchable=kind.searchable,
        terms=wanted_terms,
        order=keys,
        newest_first=newest_first,
        ranked=bool(wanted_terms) and order is None,
        limit=limit,
    )
    return selection if after is None else selection.resumed(after)


def tally(
    name: str,
    kind: type[Document],
    field: str,
    *,
    where: Mapping[str, object] | None = None,
    search: str | None = None,
    limit: int = DEFAULT_VALUES,
) -> Tally:
    """What counting the values of `field` across a listing of `kind` asks for, refusing
    what it cannot mean.

    Only a field a listing can filter on has its values counted, so that each value can
    be asked for with `where`. Every fault is reported at once, as for a listing.
    """
    problems: list[Problem] = []
    column = columns(kind).get(field)
    if column is None:
        reason = (
            "holds more than one value, so it cannot be filtered on"
            if field in kind.model_fields
            else "is not a field"
        )
        countable = ", ".join(columns(kind))
        problems.append(
            Problem(("field",), f"{field!r} {reason}; the values of {countable} can be counted")
        )
    conditions = _where(kind, where, problems)
    wanted_terms = _search(kind, search, problems)
    if not 1 <= limit <= MAX_VALUES:
        problems.append(Problem(("limit",), f"must be between 1 and {MAX_VALUES}"))
    if column is None or problems:
        raise _refusal(f"cannot count the values in {name}", problems)

    selection = Selection(
        kind=name,
        defaults=defaults(kind),
        where=conditions,
        searchable=kind.searchable,
        terms=wanted_terms,
    )
    return Tally(selection, field, column.comparison, limit)


@cache
def columns(kind: type[Document]) -> dict[str, Column]:
    """The fields of a document that a listing can filter on."""
    found = (_column(name, info.annotation) for name, info in kind.model_fields.items())
    return {column.name: column for column in found if column}


@cache
def defaults(model: type[BaseModel]) -> dict[str, Any]:
    """What each field with a default holds, as stored, in a body that holds nothing for it."""
    held: dict[str, Any] = {}
    for name, info in model.model_fields.items():
        if not info.is_required() and info.default_factory is None:
            annotation: Any = info.annotation
            held[name] = TypeAdapter(annotation).dump_python(info.default, mode="json")
    return held


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
    if comparison is Comparison.MONEY:
        return value["currency"], Decimal(value["amount"])
    return datetime.fromisoformat(value)


def _stored(value: Any, comparison: Comparison) -> Any:
    """A value from a cursor, refused unless a field compared this way could hold it."""
    if comparison is Comparison.MONEY and value is not None:
        return Money.model_validate(value).model_dump(mode="json")
    expected = (int, float, str) if comparison is Comparison.NUMBER else str
    if value is not None and (isinstance(value, bool) or not isinstance(value, expected)):
        raise TypeError("not a value of this field")
    place = comparable(value, comparison)
    if isinstance(place, Decimal) and not place.is_finite():
        raise ValueError("not a number that has a place in an order")
    return value


def _holds(condition: Condition, stored: Any) -> bool:
    if condition.operator == "in":
        return any(_same(stored, value, condition.comparison) for value in condition.value)
    if condition.operator in _EQUALITY:
        return _same(stored, condition.value, condition.comparison) == (condition.operator == "eq")
    if stored is None or condition.comparison is None:
        return False
    found = comparable(stored, condition.comparison)
    wanted = comparable(condition.value, condition.comparison)
    # An amount in one currency is neither more nor less than an amount in another.
    if condition.comparison is Comparison.MONEY and found[0] != wanted[0]:
        return False
    return _RANGES[condition.operator](found, wanted)


def _same(stored: Any, value: Any, comparison: Comparison | None) -> bool:
    if stored is None or value is None or comparison is None:
        return stored == value
    return comparable(stored, comparison) == comparable(value, comparison)


def _where(
    kind: type[Document], where: Mapping[str, object] | None, problems: list[Problem]
) -> tuple[Condition, ...]:
    return tuple(
        condition
        for field, wanted in (where or {}).items()
        for condition in _conditions(kind, field, wanted, problems)
    )


def _search(kind: type[Document], search: str | None, problems: list[Problem]) -> tuple[str, ...]:
    if search and not kind.searchable:
        problems.append(Problem(("search",), "cannot be searched; filter it with where instead"))
    elif search and len(search) > MAX_LENGTH:
        problems.append(Problem(("search",), f"is longer than {MAX_LENGTH} characters"))
    elif search:
        return terms(search)
    return ()


def _ordering(
    kind: type[Document], order: str | None, problems: list[Problem]
) -> tuple[tuple[Key, ...], bool]:
    """The fields `order` names before id, and whether documents that tie on all of them
    come newest first: as `id` is named, if it is, and otherwise as the last field is."""
    keys: list[Key] = []
    newest_first: bool | None = None
    for named in order.split(",") if order is not None else ():
        descending, field = named.startswith("-"), named.removeprefix("-")
        column = columns(kind).get(field)
        if column is None or column.comparison is None:
            problems.append(Problem(("order",), f"cannot order by {field!r}; {_orders(kind)}"))
        elif newest_first is not None:
            reason = f"cannot order by {field!r} after id, which no two documents share"
            problems.append(Problem(("order",), reason))
        elif any(key.field == field for key in keys):
            problems.append(Problem(("order",), f"names {field!r} twice"))
        elif field == "id":
            newest_first = descending
        else:
            keys.append(Key(field, column.comparison, descending))
    if newest_first is None:
        newest_first = bool(keys) and keys[-1].descending
    return tuple(keys), newest_first


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

    tests: Mapping[str, object] = {"eq": wanted}
    if isinstance(wanted, Mapping):
        given = cast(Mapping[str, object], wanted)
        # Money is a mapping itself, so a mapping that names no operator is money to equal.
        if column.comparison is not Comparison.MONEY or any(name in OPERATORS for name in given):
            tests = given
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


def _refusal(refused: str, problems: list[Problem]) -> Rejected:
    """`refused` says what cannot be done, such as `cannot list lab.sample`."""
    described = "; ".join(f"{'.'.join(map(str, p.path))}: {p.message}" for p in problems)
    return Rejected(f"{refused}: {described}", problems=problems)


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
    elif kind == "model" and schema["cls"] is Money:
        comparison, operand = Comparison.MONEY, Money
    else:
        return None
    return Column(name, comparison, _adapter(operand), nullable)


@cache
def _adapter(kind: Any) -> TypeAdapter[Any]:
    return TypeAdapter(kind)
