"""Commands that amend a document: the fields a screen can edit in place.

Such a command names the field that refers to the document, carries the version it was
read at, and otherwise holds only the document's own fields, declared the same way, so
that a screen can copy each value from the document into it.
"""

from collections.abc import Callable
from decimal import Decimal
from typing import Annotated, Any
from uuid import UUID

import pytest
from pydantic import Field

from remit.kernel import (
    Command,
    Context,
    Document,
    Lifecycle,
    Module,
    References,
    RegistrationError,
    Registry,
)

shop = Module("shop")

ProductId = Annotated[UUID, References("shop.product")]
Price = Annotated[Decimal, Field(ge=0, max_digits=9, decimal_places=2)]


@shop.document
class Product(Document):
    """Something for sale."""

    lifecycle = Lifecycle(initial="listed")

    name: str
    price: Price
    note: str | None = None
    """Anything a buyer should know."""


class ListProduct(Command[Product]):
    name: str
    price: Price


class RepriceProduct(Command[Product]):
    amends = "product_id"
    product_id: ProductId
    version: int
    """The version the product was read at. Refused if it has changed since."""
    price: Price
    note: str | None


@shop.command
def list_product(command: ListProduct, context: Context) -> Product:
    """Put a product up for sale."""
    return context.create(Product(name=command.name, price=command.price))


@shop.command
def reprice_product(command: RepriceProduct, context: Context) -> Product:
    """Change a product's price, and the note beside it."""
    product = context.get(Product, command.product_id, version=command.version)
    return context.update(product, price=command.price, note=command.note)


def test_the_registry_describes_what_a_command_amends() -> None:
    commands = Registry([shop]).describe()["shop"]["commands"]

    assert commands["shop.reprice_product"]["amends"] == {
        "document": "shop.product",
        "by": "product_id",
        "fields": ["price", "note"],
    }
    assert commands["shop.list_product"]["amends"] is None


class AmendByName(Command[Product]):
    amends = "product"
    product_id: ProductId
    version: int


def amend_by_name(command: AmendByName, context: Context) -> None:
    """Amend a product, naming a field the command does not have."""


class AmendPerhaps(Command[Product]):
    amends = "product_id"
    product_id: Annotated[UUID | None, References("shop.product")]
    version: int


def amend_perhaps(command: AmendPerhaps, context: Context) -> None:
    """Amend a product that may not be named."""


class AmendIntoNothing(Command[None]):
    amends = "product_id"
    product_id: ProductId
    version: int


def amend_into_nothing(command: AmendIntoNothing, context: Context) -> None:
    """Amend a product and return nothing."""


class RepriceUnversioned(Command[Product]):
    amends = "product_id"
    product_id: ProductId
    price: Price


def reprice_unversioned(command: RepriceUnversioned, context: Context) -> Product:
    """Change a product's price, from whatever copy it was read from."""
    return context.get(Product, command.product_id)


class RepriceOptionally(Command[Product]):
    amends = "product_id"
    product_id: ProductId
    version: int | None = None
    price: Price


def reprice_optionally(command: RepriceOptionally, context: Context) -> Product:
    """Change a product's price, naming the version it was read at if the caller likes."""
    return context.get(Product, command.product_id)


class RestockProduct(Command[Product]):
    amends = "product_id"
    product_id: ProductId
    version: int
    stock: int


def restock_product(command: RestockProduct, context: Context) -> Product:
    """Set a field the product does not have."""
    return context.get(Product, command.product_id)


class RepriceLoosely(Command[Product]):
    amends = "product_id"
    product_id: ProductId
    version: int
    price: Decimal


def reprice_loosely(command: RepriceLoosely, context: Context) -> Product:
    """Change a product's price, accepting any number at all."""
    return context.get(Product, command.product_id)


@pytest.mark.parametrize(
    ("handler", "said"),
    [
        (
            amend_by_name,
            r"tweaks\.amend_by_name declares amends = 'product', but AmendByName has no such "
            r"field",
        ),
        (
            amend_perhaps,
            r"tweaks\.amend_perhaps declares amends = 'product_id', but AmendPerhaps\.product_id "
            r"does not hold a single document's id",
        ),
        (
            amend_into_nothing,
            r"tweaks\.amend_into_nothing amends shop\.product, so it must return Product",
        ),
        (
            reprice_unversioned,
            r"tweaks\.reprice_unversioned amends shop\.product, so it needs version: int",
        ),
        (
            reprice_optionally,
            r"tweaks\.reprice_optionally amends shop\.product, so it needs version: int",
        ),
        (
            restock_product,
            r"tweaks\.restock_product amends shop\.product, which has no field 'stock'",
        ),
        (
            reprice_loosely,
            r"tweaks\.reprice_loosely declares price unlike shop\.product does; a screen copies "
            r"the document's value into it",
        ),
    ],
)
def test_a_command_that_amends_a_document_is_shaped_like_it(
    handler: Callable[[Any, Context], Any], said: str
) -> None:
    tweaks = Module("tweaks", requires=(shop,))
    tweaks.command(handler)

    with pytest.raises(RegistrationError, match=said):
        Registry([tweaks])
