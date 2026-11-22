"""The items module's commands.

Items are read as any documents are: `page(Item, where={"code": "WASHER-M8"})` finds one
by its code, and `page(Item, search="washer")` finds those a person might mean.
"""

from remit.kernel import Code, Command, Context, Name, Rejected, UnitCode

from .model import (
    Description,
    Item,
    ItemAdded,
    ItemAmended,
    ItemDiscontinued,
    ItemId,
    ItemKind,
    ItemReinstated,
    module,
)


class AddItem(Command[Item]):
    code: Code
    kind: ItemKind
    name: Name
    unit: UnitCode = "EA"
    description: Description | None = None


class AmendItem(Command[Item]):
    """Replace an item's name and description.

    Both are required, so that nothing is cleared by being left out: send null to clear
    the description.
    """

    amends = "item_id"
    item_id: ItemId
    version: int
    """The version these details were read at. Refused if the item has changed since."""
    name: Name
    description: Description | None


class DiscontinueItem(Command[Item]):
    moves = "discontinue"
    item_id: ItemId


class ReinstateItem(Command[Item]):
    moves = "reinstate"
    item_id: ItemId


@module.command
def add_item(command: AddItem, context: Context) -> Item:
    """Add an item. Codes are unique and never reused."""
    item = context.create(Item.model_validate(command.model_dump()))
    context.emit(ItemAdded(item_id=item.id, code=item.code))
    return item


@module.command
def amend_item(command: AmendItem, context: Context) -> Item:
    """Change an item's name or description. Its code, kind and unit are fixed."""
    item = context.get(Item, command.item_id, version=command.version)
    if item.state == "discontinued":
        raise Rejected(f"item {item.code} is discontinued; reinstate it before amending it")
    amended, changed = context.revise(item, name=command.name, description=command.description)
    context.emit(ItemAmended(item_id=item.id, changed=changed))
    return amended


@module.command
def discontinue_item(command: DiscontinueItem, context: Context) -> Item:
    """Stop using an item. Its history is kept."""
    discontinued = context.transition(context.get(Item, command.item_id), "discontinue")
    context.emit(ItemDiscontinued(item_id=discontinued.id))
    return discontinued


@module.command
def reinstate_item(command: ReinstateItem, context: Context) -> Item:
    """Make a discontinued item active again."""
    reinstated = context.transition(context.get(Item, command.item_id), "reinstate")
    context.emit(ItemReinstated(item_id=reinstated.id))
    return reinstated
