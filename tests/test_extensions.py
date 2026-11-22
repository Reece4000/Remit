"""Extensions: fields one module adds to a document another module owns.

A standard module keeps its documents lean, recording what a thing is. Buying adds who a
product is bought from and how long that takes; a business's own module adds how it must
be kept. The fields live in the product's own stored body, so it keeps one version and
one history, and is listed by them as by its own fields.
"""

import json
from typing import Annotated, Any
from uuid import UUID, uuid7

import httpx2
import pytest
from conftest import ROOT, kernel_on, rows
from fastapi.testclient import TestClient
from openapi_spec_validator import OpenAPIV32SpecValidator, validate
from pydantic import Field
from test_api import AUDIENCE, ISSUER, IdentityProvider, enrol

from remit import access
from remit.kernel import (
    ERASED,
    Actor,
    ActorKind,
    Command,
    Context,
    Document,
    Extension,
    Kernel,
    Lifecycle,
    Module,
    Personal,
    PostgresStore,
    Problem,
    Query,
    Reader,
    References,
    RegistrationError,
    Registry,
    Rejected,
    RemitError,
    Store,
    Transition,
)
from remit.server import Provider, Verifier, create_app

# The owning module: what a product is.

stores = Module("stores")

ProductId = Annotated[UUID, References("stores.product")]


@stores.document
class Product(Document):
    """Something the shop stocks."""

    lifecycle = Lifecycle(
        initial="listed", transitions=(Transition("withdraw", "listed", "withdrawn"),)
    )
    unique = ("code",)
    searchable = ("code", "name")

    code: str
    name: str


class ListProduct(Command[Product]):
    code: str
    name: str


class RenameProduct(Command[Product]):
    amends = "product_id"
    product_id: ProductId
    version: int
    name: str


class WithdrawProduct(Command[Product]):
    moves = "withdraw"
    product_id: ProductId


@stores.command
def list_product(command: ListProduct, context: Context) -> Product:
    """Start stocking a product."""
    return context.create(Product(code=command.code, name=command.name))


@stores.command
def rename_product(command: RenameProduct, context: Context) -> Product:
    """Give a product another name."""
    product = context.get(Product, command.product_id, version=command.version)
    return context.revise(product, name=command.name)[0]


@stores.command
def withdraw_product(command: WithdrawProduct, context: Context) -> Product:
    """Stop stocking a product, for good."""
    return context.transition(context.get(Product, command.product_id), "withdraw")


# A standard module that extends it: who a product is bought from.

buying = Module("buying", requires=(stores,))

SupplierId = Annotated[UUID | None, References("buying.supplier")]
Days = Annotated[int, Field(ge=0)]


@buying.document
class Supplier(Document):
    """Someone the shop buys from."""

    lifecycle = Lifecycle(initial="active")

    name: str


@buying.extension
class ProductBuying(Extension[Product]):
    """What buying keeps about a product: who it is bought from, and how long that takes."""

    searchable = ("supplier_code",)

    supplier_id: SupplierId = None
    """Who it is bought from unless an order says otherwise."""
    lead_time_days: Days = 0
    supplier_code: str | None = None
    """What the supplier calls it."""


class AddSupplier(Command[Supplier]):
    name: str


class SetBuying(Command[ProductBuying]):
    amends = "product_id"
    product_id: ProductId
    version: int
    supplier_id: SupplierId
    lead_time_days: Days
    supplier_code: str | None


class BuyingOf(Query[ProductBuying]):
    product_id: ProductId


@buying.command
def add_supplier(command: AddSupplier, context: Context) -> Supplier:
    """Start buying from someone."""
    return context.create(Supplier(name=command.name))


@buying.command
def set_buying(command: SetBuying, context: Context) -> ProductBuying:
    """Say who a product is bought from, and how long that takes."""
    held = context.extension(ProductBuying, command.product_id, version=command.version)
    changes = command.model_dump(include={"supplier_id", "lead_time_days", "supplier_code"})
    return context.revise(held, **changes)[0]


@buying.query
def buying_of(query: BuyingOf, reader: Reader) -> ProductBuying:
    """What buying keeps about a product."""
    return reader.extension(ProductBuying, query.product_id)


# A business's own module that extends it too: how a product must be kept.

cellar = Module("cellar", requires=(stores,))


@cellar.extension
class ProductStorage(Extension[Product]):
    """How a product must be kept."""

    searchable = ("keep",)

    keep: str = "ambient"
    """Ambient, chilled or frozen."""


class Keep(Command[ProductStorage]):
    product_id: ProductId
    keep: str


@cellar.command
def keep(command: Keep, context: Context) -> ProductStorage:
    """Say how a product must be kept."""
    held = context.extension(ProductStorage, command.product_id)
    return context.update(held, keep=command.keep)


# A module whose handlers make the mistakes the kernel must catch.

tally = Module("tally", requires=(stores,))


@tally.extension
class Counted(Extension[Product]):
    """How many of a product the last stocktake counted."""

    counted: Annotated[int, Field(ge=0)] = 0


class Count(Command[tuple[str, ...]]):
    product_id: ProductId
    counted: int


class CountTwice(Command[Counted]):
    product_id: ProductId


class Recount(Command[Counted]):
    product_id: ProductId
    changes: dict[str, int]


@tally.command
def count(command: Count, context: Context) -> tuple[str, ...]:
    """Record a count, and say which products have now been counted that many."""
    context.update(context.extension(Counted, command.product_id), counted=command.counted)
    return tuple(product.code for product in context.find(Product, counted=command.counted))


@tally.command
def count_twice(command: CountTwice, context: Context) -> Counted:
    """Count a product twice, changing the same copy each time."""
    held = context.extension(Counted, context.get(Product, command.product_id))
    context.update(held, counted=held.counted + 1)
    return context.update(held, counted=held.counted + 2)


@tally.command
def recount(command: Recount, context: Context) -> Counted:
    """Change whatever fields are named, to whatever values are given."""
    return context.update(context.extension(Counted, command.product_id), **command.changes)


CLERK = Actor(
    id="clerk",
    kind=ActorKind.HUMAN,
    permissions=frozenset({"stores.*", "buying.*", "cellar.*", "tally.*"}),
)


@pytest.fixture
def kernel(store: Store) -> Kernel:
    return kernel_on(store, [buying, cellar, tally])


def list_now(kernel: Kernel, code: str, name: str = "Lavender") -> Product:
    return kernel.execute(ListProduct(code=code, name=name), actor=CLERK).result


def buy(
    kernel: Kernel,
    product: Product,
    *,
    version: int,
    days: int,
    supplier: UUID | None = None,
    code: str | None = None,
) -> ProductBuying:
    command = SetBuying(
        product_id=product.id,
        version=version,
        supplier_id=supplier,
        lead_time_days=days,
        supplier_code=code,
    )
    return kernel.execute(command, actor=CLERK).result


def whole(kernel: Kernel, id: UUID) -> dict[str, Any]:
    """A product with every installed extension's fields, as anyone outside reads it."""
    return kernel.get(kernel.registry.extended(Product), id, actor=ROOT).model_dump()


def codes(kernel: Kernel, **listing: Any) -> list[str]:
    return [product.code for product in kernel.page(Product, actor=ROOT, **listing).items]


# Reading and writing.


def test_a_document_reads_as_holding_its_extensions_defaults_until_given_values(
    kernel: Kernel,
) -> None:
    product = list_now(kernel, "P-1")

    assert kernel.ask(BuyingOf(product_id=product.id), actor=CLERK) == ProductBuying(
        id=product.id, version=1, supplier_id=None, lead_time_days=0, supplier_code=None
    )
    assert whole(kernel, product.id) == {
        **product.model_dump(),
        "supplier_id": None,
        "lead_time_days": 0,
        "supplier_code": None,
        "keep": "ambient",
        "counted": 0,
    }


def test_each_module_keeps_the_fields_the_others_wrote(kernel: Kernel) -> None:
    supplier = kernel.execute(AddSupplier(name="Hollander Farms"), actor=CLERK).result
    product = list_now(kernel, "P-1")

    bought = buy(kernel, product, version=1, days=5, supplier=supplier.id, code="LAV-9")
    assert (bought.id, bought.version, bought.lead_time_days) == (product.id, 2, 5)
    renamed = RenameProduct(product_id=product.id, version=2, name="English lavender")
    kernel.execute(renamed, actor=CLERK)
    kernel.execute(Keep(product_id=product.id, keep="chilled"), actor=CLERK)
    buy(kernel, product, version=4, days=7, supplier=supplier.id, code="LAV-9")

    held = whole(kernel, product.id)
    assert held["name"] == "English lavender"
    assert (held["supplier_id"], held["lead_time_days"], held["supplier_code"]) == (
        supplier.id,
        7,
        "LAV-9",
    )
    assert (held["keep"], held["version"]) == ("chilled", 5)
    history = kernel.history(Product, product.id, actor=ROOT).items
    assert [change.action for change in history] == [
        "stores.list_product",
        "buying.set_buying",
        "stores.rename_product",
        "cellar.keep",
        "buying.set_buying",
    ]


def test_only_the_module_that_declares_an_extension_writes_it(store: Store) -> None:
    receiving = Module("receiving", requires=(stores, buying))

    class Expedite(Command[ProductBuying]):
        product_id: ProductId

    @receiving.command
    def expedite(command: Expedite, context: Context) -> ProductBuying:
        """Cut a product's lead time to nothing, though buying keeps it."""
        held = context.extension(ProductBuying, command.product_id)
        return context.update(held, lead_time_days=0)

    kernel = kernel_on(store, [receiving])
    product = list_now(kernel, "P-1")
    clerk = Actor(id="r", kind=ActorKind.HUMAN, permissions=frozenset({"receiving.*"}))

    with pytest.raises(
        RemitError,
        match=r"receiving cannot write buying\.product_buying: it belongs to buying, so only "
        r"buying's own code can do that",
    ):
        kernel.execute(Expedite(product_id=product.id), actor=clerk)
    assert kernel.get(Product, product.id, actor=ROOT).version == 1


def test_a_final_document_refuses_changes_to_its_extensions(kernel: Kernel) -> None:
    product = list_now(kernel, "P-1")
    kernel.execute(WithdrawProduct(product_id=product.id), actor=CLERK)

    with pytest.raises(
        Rejected, match=rf"stores\.product {product.id} is withdrawn and can no longer change"
    ):
        buy(kernel, product, version=2, days=5)


def test_an_extension_read_at_an_older_version_of_its_document_is_refused(
    kernel: Kernel,
) -> None:
    product = list_now(kernel, "P-1")
    kernel.execute(RenameProduct(product_id=product.id, version=1, name="Rose"), actor=CLERK)

    with pytest.raises(Rejected, match=r"has changed since version 1 \(it is now at version 2\)"):
        buy(kernel, product, version=1, days=5)
    assert buy(kernel, product, version=2, days=5).version == 3


def test_a_document_read_before_its_extension_changed_is_out_of_date(kernel: Kernel) -> None:
    product = list_now(kernel, "P-1")
    buy(kernel, product, version=1, days=5)

    renaming = RenameProduct(product_id=product.id, version=1, name="Rose")
    with pytest.raises(Rejected, match=r"has changed since version 1 \(it is now at version 2\)"):
        kernel.execute(renaming, actor=CLERK)


def test_a_copy_of_an_extension_from_before_its_unit_changed_it_is_refused(
    kernel: Kernel,
) -> None:
    product = list_now(kernel, "P-1")

    with pytest.raises(
        RemitError, match=rf"Counted {product.id} was changed earlier in this unit of work"
    ):
        kernel.execute(CountTwice(product_id=product.id), actor=CLERK)


def test_a_change_to_an_extension_that_changes_nothing_is_refused(kernel: Kernel) -> None:
    product = list_now(kernel, "P-1")

    with pytest.raises(
        Rejected, match=rf"nothing would change: stores\.product {product.id} already has these"
    ):
        buy(kernel, product, version=1, days=0)


def test_an_extension_refers_only_to_documents_that_exist(kernel: Kernel) -> None:
    product, nowhere = list_now(kernel, "P-1"), uuid7()
    missing = f"there is no buying.supplier with id {nowhere}"

    with pytest.raises(Rejected, match=r"stores\.product supplier_id refers to nothing") as refused:
        buy(kernel, product, version=1, days=5, supplier=nowhere)
    assert refused.value.problems == (Problem(("supplier_id",), missing, "stores.product"),)


def test_an_extension_takes_only_values_its_fields_accept(kernel: Kernel) -> None:
    product = list_now(kernel, "P-1")

    with pytest.raises(Rejected, match=rf"stores\.product {product.id}: counted: ") as refused:
        kernel.execute(Recount(product_id=product.id, changes={"counted": -1}), actor=CLERK)
    [problem] = refused.value.problems
    assert (problem.path, problem.document) == (("counted",), "stores.product")


@pytest.mark.parametrize(
    ("changes", "said"),
    [
        ({"weight": 3}, r"tally\.counted has no fields weight"),
        ({"version": 3}, r"version are managed by the kernel"),
    ],
)
def test_an_extension_is_changed_only_in_the_fields_it_adds(
    kernel: Kernel, changes: dict[str, int], said: str
) -> None:
    product = list_now(kernel, "P-1")

    with pytest.raises(RemitError, match=said):
        kernel.execute(Recount(product_id=product.id, changes=changes), actor=CLERK)


# Listing.


def test_a_listing_filters_orders_and_searches_extension_fields(kernel: Kernel) -> None:
    lavender, rose = list_now(kernel, "P-1", "Lavender"), list_now(kernel, "P-2", "Rose")
    list_now(kernel, "P-3", "Thyme")
    buy(kernel, lavender, version=1, days=5, code="LAV-9")
    buy(kernel, rose, version=1, days=2, code="ROS-1")
    kernel.execute(Keep(product_id=rose.id, keep="chilled"), actor=CLERK)

    assert codes(kernel, where={"lead_time_days": {"gte": 2}}, order="-lead_time_days") == [
        "P-1",
        "P-2",
    ]
    # P-3 has never been given these fields, and is listed as holding their defaults.
    assert codes(kernel, where={"lead_time_days": 0}) == ["P-3"]
    assert codes(kernel, where={"supplier_code": None}) == ["P-3"]
    assert codes(kernel, where={"keep": "ambient"}) == ["P-1", "P-3"]
    assert codes(kernel, order="lead_time_days") == ["P-3", "P-2", "P-1"]
    assert codes(kernel, search="ros-1") == ["P-2"]
    assert codes(kernel, search="ambient") == ["P-1", "P-3"]


def test_a_listing_counts_and_pages_documents_by_their_extensions_defaults(
    kernel: Kernel,
) -> None:
    lavender, rose = list_now(kernel, "P-1", "Lavender"), list_now(kernel, "P-2", "Rose")
    list_now(kernel, "P-3", "Thyme")
    buy(kernel, lavender, version=1, days=5)
    kernel.execute(Keep(product_id=rose.id, keep="chilled"), actor=CLERK)

    # P-2 has never been given buying's fields, and P-1 and P-3 never cellar's.
    assert kernel.page(Product, actor=ROOT, where={"keep": "ambient"}).total == 2
    assert kernel.page(Product, actor=ROOT, where={"lead_time_days": 0}).total == 2
    keeps = kernel.values(Product, "keep", actor=ROOT).items
    assert [(c.value, c.count) for c in keeps] == [("ambient", 2), ("chilled", 1)]
    days = kernel.values(Product, "lead_time_days", actor=ROOT).items
    assert [(c.value, c.count) for c in days] == [(0, 2), (5, 1)]

    paged: list[str] = []
    after = None
    while True:
        page = kernel.page(Product, actor=ROOT, order="keep,-lead_time_days", after=after, limit=1)
        paged += [product.code for product in page.items]
        if (after := page.next) is None:
            break
    assert paged == ["P-1", "P-3", "P-2"]


def test_a_handler_lists_what_its_unit_has_written_to_an_extension(kernel: Kernel) -> None:
    first, second = list_now(kernel, "P-1"), list_now(kernel, "P-2")

    counting = Count(product_id=first.id, counted=3)
    assert kernel.execute(counting, actor=CLERK).result == ("P-1",)
    counting = Count(product_id=first.id, counted=0)
    assert kernel.execute(counting, actor=CLERK).result == ("P-1", "P-2")
    counting = Count(product_id=second.id, counted=3)
    assert kernel.execute(counting, actor=CLERK).result == ("P-2",)


# Describing.


def test_the_registry_describes_a_document_with_every_extensions_fields() -> None:
    described = Registry([buying, cellar]).describe()
    product = described["stores"]["documents"]["stores.product"]
    properties = product["schema"]["properties"]

    assert properties["lead_time_days"] == {
        "default": 0,
        "minimum": 0,
        "title": "Lead Time Days",
        "type": "integer",
        "x-module": "buying",
    }
    assert properties["supplier_id"]["description"] == (
        "Who it is bought from unless an order says otherwise."
    )
    assert properties["supplier_id"]["x-references"] == "buying.supplier"
    assert properties["keep"]["x-module"] == "cellar"
    assert "x-module" not in properties["name"]
    assert product["schema"]["required"] == ["code", "name"]
    # Still the product, as what a command returns calls it.
    assert product["schema"]["title"] == "Product"
    assert product["listing"]["filters"]["lead_time_days"] == [
        "eq",
        "ne",
        "in",
        "gt",
        "gte",
        "lt",
        "lte",
    ]
    assert "keep" in product["listing"]["orders"]
    assert product["listing"]["searchable"] == ["code", "name", "supplier_code", "keep"]
    assert described["buying"]["extensions"] == {
        "buying.product_buying": {
            "summary": "What buying keeps about a product: who it is bought from, and how "
            "long that takes.",
            "extends": "stores.product",
            "fields": ["supplier_id", "lead_time_days", "supplier_code"],
        }
    }
    assert described["stores"]["extensions"] == {}


def test_a_document_is_described_with_the_extensions_installed_and_no_others() -> None:
    alone = Registry([stores]).describe()["stores"]["documents"]["stores.product"]

    assert set(alone["schema"]["properties"]) == {"id", "state", "version", "code", "name"}
    assert alone["listing"]["searchable"] == ["code", "name"]


def test_a_command_amends_an_extension_as_it_would_the_document() -> None:
    commands = Registry([buying]).describe()["buying"]["commands"]

    assert commands["buying.set_buying"]["amends"] == {
        "document": "stores.product",
        "by": "product_id",
        "fields": ["supplier_id", "lead_time_days", "supplier_code"],
    }


class RenameThroughBuying(Command[ProductBuying]):
    amends = "product_id"
    product_id: ProductId
    version: int
    name: str


def rename_through_buying(command: RenameThroughBuying, context: Context) -> ProductBuying:
    """Rename a product through what buying keeps about it."""
    return context.extension(ProductBuying, command.product_id)


class SetLeadTimeLoosely(Command[ProductBuying]):
    amends = "product_id"
    product_id: ProductId
    version: int
    lead_time_days: int


def set_lead_time_loosely(command: SetLeadTimeLoosely, context: Context) -> ProductBuying:
    """Set a lead time, accepting any number of days at all."""
    return context.extension(ProductBuying, command.product_id)


class SetBuyingBySupplier(Command[ProductBuying]):
    amends = "supplier_id"
    supplier_id: Annotated[UUID, References("buying.supplier")]
    version: int


def set_buying_by_supplier(command: SetBuyingBySupplier, context: Context) -> ProductBuying:
    """Amend a supplier, returning what buying keeps about a product."""
    raise AssertionError("never registered")


@pytest.mark.parametrize(
    ("handler", "said"),
    [
        (
            rename_through_buying,
            r"tweaks\.rename_through_buying amends buying\.product_buying, which has no field "
            r"'name'; besides product_id and version, the command holds only "
            r"buying\.product_buying's own fields",
        ),
        (
            set_lead_time_loosely,
            r"tweaks\.set_lead_time_loosely declares lead_time_days unlike buying\.product_buying "
            r"does",
        ),
        (
            set_buying_by_supplier,
            r"tweaks\.set_buying_by_supplier amends buying\.supplier, so it must return "
            r"Supplier, or an extension of it",
        ),
    ],
)
def test_a_command_that_amends_an_extension_is_shaped_like_it(handler: Any, said: str) -> None:
    tweaks = Module("tweaks", requires=(stores, buying))
    tweaks.command(handler)

    with pytest.raises(RegistrationError, match=said):
        Registry([tweaks])


# Declaring.


class Unlisted(Document):
    """A document no module declares."""

    lifecycle = Lifecycle(initial="kept")


class Loose(Extension):
    """Fields for no document in particular."""


class Vague(Extension[Any]):
    """Fields for any document at all."""


class Stray(Extension[Unlisted]):
    """Fields for a document no module declares."""


class Versioned(Extension[Product]):
    """A version of its own, beside the product's."""

    version: int = 0


class Stated(Extension[Product]):
    """A state of its own, beside the product's."""

    state: str = ""


class Renamed(Extension[Product]):
    """A name, though the product has one."""

    name: str = ""


class Hurried(Extension[Product]):
    """A lead time, though buying adds one."""

    lead_time_days: int = 1


class Weighed(Extension[Product]):
    """A weight, which products written before it have none of."""

    grams: int


class Tagged(Extension[Product]):
    """Tags, made afresh for each product."""

    tags: tuple[str, ...] = Field(default_factory=tuple)


class Graded(Extension[Product]):
    """A grade, said to be searchable though it is a number."""

    searchable = ("grade",)

    grade: int = 0


class Signed(Extension[Product]):
    """Who signed for a product, though products are about no one."""

    signed_by: Annotated[str | None, Personal] = None


class Pointing(Extension[Product]):
    """A reference to a document of a module tinker does not require."""

    member_id: Annotated[UUID | None, References("club.member")] = None


@pytest.mark.parametrize(
    ("extension", "said"),
    [
        (
            Loose,
            r"tinker\.loose names no document to extend; declare it as "
            r"Loose\(Extension\[TheDocument\]\)",
        ),
        (Vague, r"tinker\.vague names no document to extend"),
        (Stray, r"tinker\.stray extends Unlisted, which is not a document of an installed module"),
        (Versioned, r"tinker\.versioned declares version, which every document has already"),
        (Stated, r"tinker\.stated declares state, which every document has already"),
        (Renamed, r"tinker\.renamed declares 'name', which stores\.product has already"),
        (
            Hurried,
            r"tinker\.hurried declares 'lead_time_days', which buying\.product_buying already "
            r"adds to stores\.product",
        ),
        (
            Weighed,
            r"tinker\.weighed needs a default value for grams: documents written before it was "
            r"installed hold nothing for its fields",
        ),
        (Tagged, r"tinker\.tagged needs a default value for tags"),
        (Graded, r"tinker\.graded\.grade is declared searchable but does not hold text"),
        (
            Signed,
            r"tinker\.signed holds personal data, but stores\.product holds none and is erased "
            r"by no one",
        ),
        (
            Pointing,
            r"Pointing\.member_id refers to club\.member, which is not a document of tinker or "
            r"of a module it requires",
        ),
        (
            ProductBuying,
            r"tinker\.product_buying is the class already registered as buying\.product_buying",
        ),
    ],
)
def test_an_extension_adds_fields_of_its_own_with_defaults_to_a_document_it_may_extend(
    extension: type[Extension[Any]], said: str
) -> None:
    tinker = Module("tinker", requires=(stores, buying))
    tinker.extension(extension)

    with pytest.raises(RegistrationError, match=said):
        Registry([tinker])


def test_a_module_extends_only_the_documents_of_modules_it_requires() -> None:
    loner = Module("loner")

    @loner.extension
    class Shelved(Extension[Product]):
        """Which shelf a product is on."""

        shelf: str = ""

    with pytest.raises(
        RegistrationError,
        match=r"loner\.shelved extends stores\.product, but loner does not require stores",
    ):
        Registry([stores, loner])


def test_a_module_declares_fields_on_its_own_documents_rather_than_extending_them() -> None:
    workshop = Module("workshop")

    @workshop.document
    class Bench(Document):
        """Where work is done."""

        lifecycle = Lifecycle(initial="free")

    @workshop.extension
    class BenchLighting(Extension[Bench]):
        """How a bench is lit."""

        lamp: str = ""

    with pytest.raises(
        RegistrationError,
        match=r"workshop\.bench_lighting extends workshop\.bench, a document of workshop "
        r"itself; declare its fields there instead",
    ):
        Registry([workshop])


# Personal data.

club = Module("club")

MemberId = Annotated[UUID, References("club.member")]


@club.document
class Member(Document):
    """A member of the club. Their record is kept after what it says about them is erased."""

    lifecycle = Lifecycle(initial="current", transitions=(Transition("leave", "current", "left"),))

    name: Annotated[str, Personal]


class Join(Command[Member]):
    name: Annotated[str, Personal]


class EraseMember(Command[Member]):
    member_id: MemberId


@club.command
def join(command: Join, context: Context) -> Member:
    """Welcome a member."""
    return context.create(Member(name=command.name))


@club.command
def erase_member(command: EraseMember, context: Context) -> Member:
    """Erase what the club holds about a member."""
    erased = context.update(context.get(Member, command.member_id), name=ERASED)
    context.erase(erased)
    return erased


phones = Module("phones", requires=(club,))


@phones.extension
class MemberPhone(Extension[Member]):
    """How to reach a member by phone."""

    mobile: Annotated[str | None, Personal] = None
    texts: bool = False
    """Whether they have agreed to be sent texts."""


class SetPhone(Command[MemberPhone]):
    amends = "member_id"
    member_id: MemberId
    version: int
    mobile: Annotated[str | None, Personal]
    texts: bool


class PhoneAfterErasing(Command[MemberPhone]):
    member_id: MemberId


@phones.command
def set_phone(command: SetPhone, context: Context) -> MemberPhone:
    """Say how to reach a member by phone."""
    held = context.extension(MemberPhone, command.member_id, version=command.version)
    return context.update(held, mobile=command.mobile, texts=command.texts)


@phones.command
def phone_after_erasing(command: PhoneAfterErasing, context: Context) -> MemberPhone:
    """Erase a member, then give them a mobile number again."""
    context.execute(EraseMember(member_id=command.member_id))
    held = context.extension(MemberPhone, command.member_id)
    return context.update(held, mobile="07700 900123")


SECRETARY = Actor(id="s", kind=ActorKind.HUMAN, permissions=frozenset({"club.*", "phones.*"}))


def test_erasing_a_document_erases_what_its_extensions_hold_about_the_person(
    store: Store,
) -> None:
    kernel = kernel_on(store, [phones])
    member = kernel.execute(Join(name="Ada Lovelace"), actor=SECRETARY).result
    phoning = SetPhone(member_id=member.id, version=1, mobile="07700 900123", texts=True)
    kernel.execute(phoning, actor=SECRETARY)

    kernel.execute(EraseMember(member_id=member.id), actor=SECRETARY)

    held = kernel.get(kernel.registry.extended(Member), member.id, actor=ROOT).model_dump()
    assert (held["name"], held["mobile"], held["texts"]) == (ERASED, None, True)
    _, phoned, _ = kernel.audit(actor=ROOT, document=(Member, member.id))
    assert phoned.input["mobile"] == ERASED


def test_an_erased_document_is_given_no_personal_data_again_in_the_same_unit(
    store: Store,
) -> None:
    kernel = kernel_on(store, [phones])
    member = kernel.execute(Join(name="Ada Lovelace"), actor=SECRETARY).result

    with pytest.raises(
        RemitError, match=r"replace club\.member's personal fields before erasing it: mobile"
    ):
        kernel.execute(PhoneAfterErasing(member_id=member.id), actor=SECRETARY)


def test_a_personal_field_an_extension_adds_defaults_to_holding_nothing() -> None:
    loud = Module("loud", requires=(club,))

    @loud.extension
    class Nickname(Extension[Member]):
        """What a member is called at the bar."""

        nickname: Annotated[str, Personal] = "mate"

    with pytest.raises(
        RegistrationError,
        match=r"loud\.nickname\.nickname holds personal data, so its default must hold none",
    ):
        Registry([loud])


# Serving.


def test_the_api_serves_documents_with_their_extensions_fields(store: Store) -> None:
    kernel = kernel_on(store, [access.module, buying, cellar])
    provider = IdentityProvider()
    http = httpx2.Client(transport=httpx2.MockTransport(provider.handle))
    verifier = Verifier((Provider(issuer=ISSUER, audience=AUDIENCE),), http=http)
    enrol(kernel, "buyer", "stores.*", "buying.*")
    headers = {"Authorization": f"Bearer {provider.token('buyer')}"}

    with TestClient(create_app(kernel, verifier)) as client:
        document = client.get("/openapi.json").json()
        validate(document, cls=OpenAPIV32SpecValidator)
        schemas = document["components"]["schemas"]
        assert schemas["ExtendedProduct"]["properties"]["lead_time_days"]["x-module"] == "buying"
        assert schemas["ExtendedProduct"]["properties"]["keep"]["x-module"] == "cellar"
        paths = document["paths"]

        def answer(path: str) -> str:
            ok = paths[path]["get"]["responses"]["200"]["content"]["application/json"]
            return ok["schema"]["$ref"]

        assert answer("/documents/stores.product/{id}").endswith("/ExtendedProduct")
        assert answer("/documents/stores.product").endswith("/Listed_ExtendedProduct_")

        listing = {"code": "P-1", "name": "Lavender"}
        listed = client.post("/commands/stores.list_product", json=listing, headers=headers)
        id = listed.json()["result"]["id"]
        fields = {"supplier_id": None, "lead_time_days": 5, "supplier_code": None}
        setting = {"product_id": id, "version": 1, **fields}
        bought = client.post("/commands/buying.set_buying", json=setting, headers=headers)
        assert bought.json()["result"] == {"id": id, "version": 2, **fields}
        read = client.get(f"/documents/stores.product/{id}", headers=headers)
        assert read.headers["ETag"] == '"2"'
        assert (read.json()["lead_time_days"], read.json()["keep"]) == (5, "ambient")
        found = client.get(
            "/documents/stores.product",
            params={"where": json.dumps({"lead_time_days": {"gte": 5}})},
            headers=headers,
        )
        assert [item["code"] for item in found.json()["items"]] == ["P-1"]
        counted = client.get("/documents/stores.product/values/keep", headers=headers)
        assert counted.json() == {"values": [{"value": "ambient", "count": 1}], "more": False}


# Installing.


def test_installing_an_extension_rebuilds_what_is_searched_and_migrates_nothing(
    postgres: PostgresStore,
) -> None:
    def larder(keep: str) -> Module:
        module = Module("larder", requires=(stores,))

        class ProductShelf(Extension[Product]):
            """Where in the larder a product is kept."""

            searchable = ("shelf",)

            shelf: str = keep

        module.extension(ProductShelf)
        return module

    def searched() -> list[str]:
        found = rows("SELECT text FROM document_search WHERE kind = 'stores.product' ORDER BY text")
        return [text for (text,) in found]

    kernel = kernel_on(postgres, [stores])
    list_now(kernel, "P-1", "Lavender")
    withdrawn = list_now(kernel, "P-2", "Rose")
    kernel.execute(WithdrawProduct(product_id=withdrawn.id), actor=CLERK)
    assert searched() == ["\np-1\nlavender\n", "\np-2\nrose\n"]

    kernel = kernel_on(postgres, [larder("top shelf")])

    assert searched() == ["\np-1\nlavender\ntop shelf\n", "\np-2\nrose\ntop shelf\n"]
    assert codes(kernel, where={"shelf": "top shelf"}, search="top") == ["P-1", "P-2"]
    withdrawn_whole = kernel.get(kernel.registry.extended(Product), withdrawn.id, actor=ROOT)
    assert withdrawn_whole.model_dump()["shelf"] == "top shelf"

    lower = larder("bottom shelf")
    with pytest.raises(RegistrationError, match="installed from a different definition"):
        Kernel([lower], store=postgres)
    kernel = kernel_on(postgres, [lower])
    assert searched() == ["\np-1\nlavender\nbottom shelf\n", "\np-2\nrose\nbottom shelf\n"]
