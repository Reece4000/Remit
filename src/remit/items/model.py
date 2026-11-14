"""The items module's documents and events.

An item records what something is. How it is priced, taxed, stocked, bought or made
belongs to the modules concerned, which refer to the item by id.
"""

from enum import StrEnum
from typing import Annotated
from uuid import UUID

from pydantic import StringConstraints

from remit.kernel import Code, Document, Event, Lifecycle, Module, Name, Transition

module = Module("items")

Unit = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=20)]
Description = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class ItemKind(StrEnum):
    GOODS = "goods"
    """Something physical, which can be held, counted and shipped."""
    SERVICE = "service"
    """Work or a right, which cannot be held."""


@module.document
class Item(Document):
    """Something the business buys, sells, makes or gives away.

    Its code, kind and unit are fixed once it is added: quantities everywhere are
    recorded in its unit, so changing the unit would change what they mean.
    Discontinued items keep their history and can be reinstated.
    """

    lifecycle = Lifecycle(
        initial="active",
        transitions=(
            Transition("discontinue", source="active", target="discontinued"),
            Transition("reinstate", source="discontinued", target="active"),
        ),
    )
    unique = ("code",)

    code: Code
    kind: ItemKind
    name: Name
    unit: Unit = "each"
    """What one of this item is, such as each, kg, hour or box of 100."""
    description: Description | None = None


@module.event
class ItemAdded(Event):
    """An item was added."""

    item_id: UUID
    code: str


@module.event
class ItemAmended(Event):
    """An item's name or description was changed. `changed` names the fields that differ."""

    item_id: UUID
    changed: tuple[str, ...]


@module.event
class ItemDiscontinued(Event):
    """An item was discontinued."""

    item_id: UUID


@module.event
class ItemReinstated(Event):
    """A discontinued item was made active again."""

    item_id: UUID
