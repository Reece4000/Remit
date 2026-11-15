"""Modules built on other modules: dependencies, the order they install in, and what a
module may react to."""

from collections.abc import Iterator
from uuid import UUID

import psycopg
import pytest
from conftest import DSN, kernel_on
from psycopg.conninfo import make_conninfo
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
    kernel = kernel_on(store, [shop])
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


# A kernel runs only modules its store was installed with, as they are now declared.


def registry_module(*, unique: tuple[str, ...]) -> Module:
    """A fresh definition of one module, with or without a unique field."""
    registry = Module("test_registry")

    @registry.document
    class Entry(Document):
        """An entry in a register."""

        lifecycle = Lifecycle(initial="entered")

        number: str

    Entry.unique = unique
    return registry


@pytest.fixture
def fresh_registry(installed_postgres: PostgresStore) -> Iterator[PostgresStore]:
    def forget() -> None:
        with psycopg.connect(DSN) as connection:
            connection.execute("DELETE FROM installed_modules WHERE module = 'test_registry'")
            connection.execute('DROP INDEX IF EXISTS "unique test_registry.entry.number"')

    forget()
    yield installed_postgres
    forget()


def indexes_on_documents() -> set[str]:
    with psycopg.connect(DSN) as connection:
        rows = connection.execute(
            "SELECT indexname FROM pg_indexes WHERE tablename = 'documents'"
        ).fetchall()
    return {name for (name,) in rows}


def test_a_kernel_refuses_modules_its_store_was_not_installed_with(
    fresh_registry: PostgresStore,
) -> None:
    with pytest.raises(RegistrationError, match="'test_registry' is not installed"):
        Kernel([registry_module(unique=("number",))], store=fresh_registry)


def test_a_kernel_refuses_a_module_whose_definition_changed_since_install(
    fresh_registry: PostgresStore,
) -> None:
    fresh_registry.install([registry_module(unique=())])
    changed = registry_module(unique=("number",))

    with pytest.raises(RegistrationError, match="installed from a different definition"):
        Kernel([changed], store=fresh_registry)

    fresh_registry.install([changed])
    Kernel([changed], store=fresh_registry)


def test_installing_drops_the_index_of_a_field_that_is_no_longer_unique(
    fresh_registry: PostgresStore,
) -> None:
    fresh_registry.install([registry_module(unique=("number",))])
    assert "unique test_registry.entry.number" in indexes_on_documents()

    fresh_registry.install([registry_module(unique=())])
    assert "unique test_registry.entry.number" not in indexes_on_documents()


def test_a_kernel_on_a_database_never_installed_says_so(installed_postgres: PostgresStore) -> None:
    fresh = "remit_test_fresh"
    with psycopg.connect(DSN, autocommit=True) as connection:
        connection.execute(f"DROP DATABASE IF EXISTS {fresh}")
        connection.execute(f"CREATE DATABASE {fresh}")
    store = PostgresStore(make_conninfo(DSN, dbname=fresh), timeout=3)
    try:
        with pytest.raises(RegistrationError, match="nothing has been installed"):
            Kernel([catalogue], store=store)
    finally:
        store.close()
        with psycopg.connect(DSN, autocommit=True) as connection:
            connection.execute(f"DROP DATABASE {fresh}")


# Migrations are applied once and never change afterwards.


def archive_module(create: str) -> Module:
    return Module("test_archive", migrations=(Migration("create", create),))


@pytest.fixture
def fresh_archive(installed_postgres: PostgresStore) -> Iterator[PostgresStore]:
    def forget() -> None:
        with psycopg.connect(DSN) as connection:
            connection.execute("DROP TABLE IF EXISTS test_archive")
            connection.execute("DELETE FROM migrations WHERE module = 'test_archive'")
            connection.execute("DELETE FROM installed_modules WHERE module = 'test_archive'")

    forget()
    yield installed_postgres
    forget()


def columns_of_test_archive() -> list[str]:
    with psycopg.connect(DSN) as connection:
        rows = connection.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = 'test_archive' ORDER BY ordinal_position"
        ).fetchall()
    return [name for (name,) in rows]


def test_a_migration_edited_after_it_was_applied_is_refused(fresh_archive: PostgresStore) -> None:
    fresh_archive.install([archive_module("CREATE TABLE test_archive (id int)")])
    edited = archive_module("CREATE TABLE test_archive (id int, label text)")

    with pytest.raises(RegistrationError, match="'create' of test_archive has changed since"):
        fresh_archive.install([edited])
    with pytest.raises(RegistrationError, match="installed from a different definition"):
        Kernel([edited], store=fresh_archive)
    assert columns_of_test_archive() == ["id"]


def test_a_migration_applied_before_digests_were_kept_is_adopted(
    fresh_archive: PostgresStore,
) -> None:
    module = archive_module("CREATE TABLE test_archive (id int)")
    fresh_archive.install([module])
    with psycopg.connect(DSN) as connection:
        connection.execute("UPDATE migrations SET digest = NULL WHERE module = 'test_archive'")

    fresh_archive.install([module])
    with pytest.raises(RegistrationError, match="has changed since"):
        fresh_archive.install([archive_module("CREATE TABLE test_archive (id bigint)")])


def test_a_kernel_refuses_a_database_installed_by_another_version_of_remit(
    installed_postgres: PostgresStore,
) -> None:
    with psycopg.connect(DSN) as connection:
        connection.execute(
            "UPDATE installed_modules SET fingerprint = 'older' WHERE module = 'remit.kernel'"
        )
    try:
        with pytest.raises(RegistrationError, match="installed by a different version of Remit"):
            Kernel([], store=installed_postgres)
    finally:
        installed_postgres.install()
    Kernel([], store=installed_postgres)
