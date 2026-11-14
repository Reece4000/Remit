"""Stores to test against. Tests that take `store` run once in memory and once on PostgreSQL.

PostgreSQL is found at `REMIT_TEST_DSN` (default: a local database named `remit_test`).
If nothing answers there, the PostgreSQL runs are skipped and say so, unless
`REMIT_REQUIRE_POSTGRES` is set, as `./check` does, in which case they fail.
"""

import os
from collections.abc import Iterator

import psycopg
import pytest

from remit import ledger
from remit.kernel import MemoryStore, PostgresStore, Store

DSN = os.environ.get("REMIT_TEST_DSN", "postgresql:///remit_test")


def empty_database() -> None:
    with psycopg.connect(DSN) as connection:
        connection.execute(
            "TRUNCATE documents, events, audit, cursors, idempotency, ledger_books, "
            "pgledger_idempotency_keys, pgledger_entries, pgledger_transfers, pgledger_accounts "
            "RESTART IDENTITY"
        )


@pytest.fixture(scope="session")
def installed_postgres() -> Iterator[PostgresStore]:
    try:
        store = PostgresStore(DSN, timeout=3)
    except Exception as error:
        if os.environ.get("REMIT_REQUIRE_POSTGRES"):
            pytest.fail(f"PostgreSQL is required but nothing answers at {DSN}: {error}")
        pytest.skip(f"no PostgreSQL at {DSN}: {error}")
    store.install([ledger.module])
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
