"""Stores to test against. Tests that take `store` run once in memory and once on PostgreSQL.

PostgreSQL is found at `REMIT_TEST_DSN` (default: a local database named `remit_test`).
If nothing answers there, the PostgreSQL runs are skipped and say so, unless
`REMIT_REQUIRE_POSTGRES` is set, as `./check` does, in which case they fail.
"""

import os
from collections.abc import Iterator, Sequence
from typing import Any

import psycopg
import pytest

from remit import items, ledger, parties
from remit.kernel import Kernel, MemoryStore, Module, PostgresStore, Store

DSN = os.environ.get("REMIT_TEST_DSN", "postgresql:///remit_test")


def kernel_on(store: Store, modules: Sequence[Module], **options: Any) -> Kernel:
    """A kernel on the store, installing the modules first when it is PostgreSQL.

    The order a deployment follows: install, then run.
    """
    if isinstance(store, PostgresStore):
        store.install(modules)
    return Kernel(modules, store=store, **options)


RECORDS = (
    "documents, events, audit, cursors, idempotency, data_keys, data_key_subjects, "
    "ledger_books, pgledger_idempotency_keys, pgledger_entries, pgledger_transfers, "
    "pgledger_accounts"
)


def empty_database() -> None:
    """Empty every table of record between tests.

    The database refuses TRUNCATE on these tables, as it must in production. A test
    database is the one place they may be emptied, so its owner switches those triggers
    off for the length of one transaction, which turns them back on when it commits.
    """
    with psycopg.connect(DSN) as connection:
        tables = [table.strip() for table in RECORDS.split(",")]
        for table in tables:
            connection.execute(f"ALTER TABLE {table} DISABLE TRIGGER never_truncated".encode())
        connection.execute(f"TRUNCATE {RECORDS} RESTART IDENTITY".encode())
        for table in tables:
            connection.execute(f"ALTER TABLE {table} ENABLE TRIGGER never_truncated".encode())


@pytest.fixture(scope="session")
def installed_postgres() -> Iterator[PostgresStore]:
    try:
        store = PostgresStore(DSN, timeout=3)
    except Exception as error:
        if os.environ.get("REMIT_REQUIRE_POSTGRES"):
            pytest.fail(f"PostgreSQL is required but nothing answers at {DSN}: {error}")
        pytest.skip(f"no PostgreSQL at {DSN}: {error}")
    store.install([ledger.module, parties.module, items.module])
    yield store
    store.close()


@pytest.fixture
def postgres(installed_postgres: PostgresStore) -> PostgresStore:
    empty_database()
    return installed_postgres


@pytest.fixture(params=["memory", "postgres"])
def store(request: pytest.FixtureRequest) -> Store:
    if request.param == "memory":
        return MemoryStore()
    return request.getfixturevalue("postgres")
