"""A store on PostgreSQL.

Each unit is one SERIALIZABLE transaction on a pooled connection. PostgreSQL aborts a
transaction it cannot fit into a serial order; that surfaces here as `ConcurrencyError`
and the kernel runs the unit again.
"""

from collections.abc import Generator, Iterable, Mapping, Sequence
from contextlib import contextmanager
from importlib.resources import files
from typing import Any, cast
from uuid import UUID

from psycopg import Connection, IsolationLevel, errors
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

from .actor import Actor
from .errors import ConcurrencyError
from .module import Module
from .store import AuditEntry, Commit, Json, StoredEvent, Unit

_SCHEMA = files(__package__).joinpath("schema.sql").read_bytes()

# Held from the moment a unit records events until it commits, so that events become
# visible in the same order as their positions and no subscriber can skip one.
_EVENT_ORDER_LOCK = 7_154_697_431

_UNSERIALISABLE = (errors.SerializationFailure, errors.DeadlockDetected, errors.UniqueViolation)


class PostgresStore:
    runs_sql = True

    def __init__(self, dsn: str, *, connections: int = 10, timeout: float = 10.0) -> None:
        self._pool = ConnectionPool(
            dsn, min_size=1, max_size=connections, configure=_serialisable, open=False
        )
        self._pool.open(wait=True, timeout=timeout)

    def install(self, modules: Iterable[Module] = ()) -> None:
        """Create the kernel's tables, then each module's migrations and database rules."""
        with self._pool.connection() as connection:
            connection.execute(_SCHEMA)
            for module in modules:
                for migration in module.migrations:
                    claimed = connection.execute(
                        "INSERT INTO migrations (module, name) VALUES (%s, %s) "
                        "ON CONFLICT DO NOTHING",
                        (module.name, migration.name),
                    )
                    if claimed.rowcount:
                        connection.execute(migration.sql.encode())
                if module.sql:
                    connection.execute(module.sql.encode())

    def close(self) -> None:
        self._pool.close()

    @contextmanager
    def unit(self) -> Generator[Unit]:
        try:
            with self._pool.connection() as connection:
                yield _PostgresUnit(connection)
        except _UNSERIALISABLE as conflict:
            raise ConcurrencyError("the database could not serialise this unit of work") from (
                conflict
            )


def _serialisable(connection: Connection[Any]) -> None:
    connection.isolation_level = IsolationLevel.SERIALIZABLE


class _PostgresUnit:
    def __init__(self, connection: Connection[Any]) -> None:
        self._connection = connection

    def get(self, kind: str, id: UUID) -> Json | None:
        row = self._connection.execute(
            "SELECT body FROM documents WHERE kind = %s AND id = %s", (kind, id)
        ).fetchone()
        return cast(Json, row[0]) if row else None

    def find(self, kind: str, equals: Mapping[str, object]) -> list[Json]:
        rows = self._connection.execute(
            "SELECT body FROM documents WHERE kind = %s AND body @> %s ORDER BY id",
            (kind, Jsonb(dict(equals))),
        ).fetchall()
        return [cast(Json, row[0]) for row in rows]

    def events(
        self, *, after: int = 0, name: str | None = None, limit: int | None = None
    ) -> list[StoredEvent]:
        rows = self._connection.execute(
            """
            SELECT position, name, body, at, actor FROM events
            WHERE position > %s AND (%s::text IS NULL OR name = %s)
            ORDER BY position LIMIT %s
            """,
            (after, name, name, limit),
        ).fetchall()
        return [StoredEvent(*row) for row in rows]

    def cursor(self, subscription: str) -> int:
        row = self._connection.execute(
            "SELECT position FROM cursors WHERE subscription = %s", (subscription,)
        ).fetchone()
        return cast(int, row[0]) if row else 0

    def idempotency(self, key: str) -> Json | None:
        row = self._connection.execute(
            "SELECT record FROM idempotency WHERE key = %s", (key,)
        ).fetchone()
        return cast(Json, row[0]) if row else None

    def audit(self) -> list[AuditEntry]:
        rows = self._connection.execute(
            "SELECT at, actor, action, input, documents, events, cause FROM audit ORDER BY id"
        ).fetchall()
        return [
            AuditEntry(
                at=at,
                actor=Actor.model_validate(actor),
                action=action,
                input=input,
                documents=tuple((kind, UUID(id), version) for kind, id, version in documents),
                events=tuple(events),
                cause=cause,
            )
            for at, actor, action, input, documents, events, cause in rows
        ]

    def sql(self, query: str, parameters: Sequence[object] = ()) -> list[tuple[Any, ...]]:
        cursor = self._connection.execute(query.encode(), parameters)
        return cursor.fetchall() if cursor.description else []

    def rollback(self) -> None:
        self._connection.rollback()

    def commit(self, commit: Commit) -> None:
        execute = self._connection.execute
        for document in commit.documents:
            values = (document.version, document.final, Jsonb(document.body))
            key = (document.kind, document.id)
            if document.version == 1:
                execute(
                    "INSERT INTO documents (version, final, body, kind, id) "
                    "VALUES (%s, %s, %s, %s, %s)",
                    (*values, *key),
                )
                continue
            updated = execute(
                "UPDATE documents SET version = %s, final = %s, body = %s "
                "WHERE kind = %s AND id = %s AND version = %s",
                (*values, *key, document.version - 1),
            )
            if updated.rowcount != 1:
                raise ConcurrencyError(f"{document.kind} {document.id} changed since it was read")

        audit = commit.audit
        if commit.events:
            execute("SELECT pg_advisory_xact_lock(%s)", (_EVENT_ORDER_LOCK,))
            for name, body in commit.events:
                execute(
                    "INSERT INTO events (name, body, at, actor) VALUES (%s, %s, %s, %s)",
                    (name, Jsonb(body), audit.at, audit.actor.id),
                )
        execute(
            "INSERT INTO audit (at, actor, action, input, documents, events, cause) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s)",
            (
                audit.at,
                Jsonb(audit.actor.model_dump(mode="json")),
                audit.action,
                Jsonb(audit.input),
                Jsonb([[kind, str(id), version] for kind, id, version in audit.documents]),
                Jsonb(list(audit.events)),
                audit.cause,
            ),
        )
        if commit.cursor:
            execute(
                "INSERT INTO cursors (subscription, position) VALUES (%s, %s) "
                "ON CONFLICT (subscription) DO UPDATE SET position = EXCLUDED.position",
                commit.cursor,
            )
        if commit.idempotency:
            key, record = commit.idempotency
            execute("INSERT INTO idempotency (key, record) VALUES (%s, %s)", (key, Jsonb(record)))
