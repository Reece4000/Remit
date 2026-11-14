"""Modules built on other modules: dependencies, the order they install in, and what a
module may react to."""

from collections.abc import Iterator
from uuid import UUID

import psycopg
import pytest
from conftest import DSN
from pydantic import BaseModel

from remit.kernel import (
    Actor,
    ActorKind,
    Command,
    Context,
    Document,
    Event,
    Kernel,
    Lifecycle,
    Migration,
    Module,
    PostgresStore,
    RegistrationError,
    Store,
)

catalogue = Module("catalogue")


@catalogue.document
class Product(Document):
    """Something for sale."""

    lifecycle = Lifecycle(initial="listed")

    name: str


@catalogue.event
class ProductListed(Event):
    """A product was listed."""

    product_id: UUID


class ListProduct(Command[Product]):
    name: str


@catalogue.command
def list_product(command: ListProduct, context: Context) -> Product:
    """List a product."""
    product = context.create(Product(name=command.name))
    context.emit(ProductListed(product_id=product.id))
    return product


shop = Module("shop", requires=(catalogue,))


@shop.document
class ShelfLabel(Document):
    """A label printed for a newly listed product."""

    lifecycle = Lifecycle(initial="printed")

    product_id: UUID


@shop.subscribe(ProductListed)
def print_label(event: ProductListed, context: Context) -> None:
    """Print a shelf label for every product listed in the catalogue."""
    context.create(ShelfLabel(product_id=event.product_id))


ADMIN = Actor(id="admin", kind=ActorKind.HUMAN, permissions=frozenset({"*"}))


def test_a_module_brings_its_dependencies_with_it() -> None:
    kernel = Kernel([shop])
    assert list(kernel.registry.modules) == ["catalogue", "shop"]


def test_dependencies_install_first_and_once_whatever_order_they_are_given_in() -> None:
    kernel = Kernel([shop, catalogue])
    assert list(kernel.registry.modules) == ["catalogue", "shop"]


def test_unrelated_modules_keep_the_order_they_were_given_in() -> None:
    first, second = Module("first"), Module("second")
    assert list(Kernel([second, first]).registry.modules) == ["second", "first"]


def test_a_dependency_must_be_the_same_module_everywhere() -> None:
    impostor = Module("catalogue")
    with pytest.raises(RegistrationError, match="installed twice"):
        Kernel([impostor, shop])


def test_requirements_cannot_be_changed_after_declaration() -> None:
    with pytest.raises(AttributeError):
        shop.requires = ()  # pyright: ignore[reportAttributeAccessIssue]


def test_a_dependency_needs_its_settings_even_when_only_its_dependent_is_named() -> None:
    class Settings(BaseModel):
        region: str

    configured = Module("configured", settings=Settings)
    with pytest.raises(RegistrationError, match="needs Settings settings"):
        Kernel([Module("dependent", requires=(configured,))])


def test_a_module_reacts_to_events_of_the_modules_it_requires(store: Store) -> None:
    kernel = Kernel([shop], store=store)
    product = kernel.execute(ListProduct(name="Kettle"), actor=ADMIN).result
    assert kernel.dispatch().delivered == 1
    assert [label.product_id for label in kernel.find(ShelfLabel)] == [product.id]


def test_a_module_cannot_react_to_events_of_a_module_it_does_not_require() -> None:
    eavesdropper = Module("eavesdropper")

    @eavesdropper.subscribe(ProductListed)
    def overhear(event: ProductListed, context: Context) -> None:
        """React to someone else's event without declaring the dependency."""

    with pytest.raises(
        RegistrationError,
        match=r"eavesdropper\.overhear reacts to catalogue\.product_listed, "
        r"but eavesdropper does not require catalogue",
    ):
        Kernel([catalogue, eavesdropper])


def test_the_registry_describes_what_each_module_requires() -> None:
    described = Kernel([shop]).registry.describe()
    assert described["shop"]["requires"] == ["catalogue"]
    assert described["catalogue"]["requires"] == []


# Installation on PostgreSQL: a dependent's migrations may rely on its dependency's.

foundation = Module(
    "test_foundation",
    migrations=(Migration("create", "CREATE TABLE test_foundation (id int PRIMARY KEY)"),),
)
extension = Module(
    "test_extension",
    requires=(foundation,),
    migrations=(
        Migration(
            "create",
            "CREATE TABLE test_extension (id int PRIMARY KEY REFERENCES test_foundation (id))",
        ),
    ),
)


@pytest.fixture
def clean_schema(installed_postgres: PostgresStore) -> Iterator[PostgresStore]:
    def drop() -> None:
        with psycopg.connect(DSN) as connection:
            connection.execute("DROP TABLE IF EXISTS test_extension, test_foundation")
            connection.execute(
                "DELETE FROM migrations WHERE module IN ('test_foundation', 'test_extension')"
            )

    drop()
    yield installed_postgres
    drop()


def test_installing_a_module_installs_its_dependencies_first(clean_schema: PostgresStore) -> None:
    # The extension's table references the foundation's, so this fails unless the
    # foundation's migration was applied first.
    clean_schema.install([extension])
    with psycopg.connect(DSN) as connection:
        applied = connection.execute(
            "SELECT module FROM migrations WHERE module LIKE 'test_%' ORDER BY module"
        ).fetchall()
    assert [module for (module,) in applied] == ["test_extension", "test_foundation"]


def test_a_unique_field_whose_index_name_postgres_would_truncate_is_refused(
    installed_postgres: PostgresStore,
) -> None:
    verbose = Module("a_module_with_a_name_long_enough_to_overflow_postgres")

    @verbose.document
    class Registration(Document):
        """A registration."""

        lifecycle = Lifecycle(initial="recorded")
        unique = ("registration_number",)

        registration_number: str

    with pytest.raises(RegistrationError, match="too long a name to index"):
        installed_postgres.install([verbose])
