"""A store on PostgreSQL.

Each unit is one SERIALIZABLE transaction on a pooled connection. PostgreSQL aborts a
transaction it cannot fit into a serial order; that surfaces here as `ConcurrencyError`
and the kernel runs the unit again.
"""

import hashlib
import json
from collections.abc import Generator, Iterable, Mapping, Sequence
from contextlib import contextmanager
from importlib.resources import files
from typing import Any, cast
from uuid import UUID

from psycopg import Connection, IsolationLevel, errors, sql
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

from .actor import Actor
from .errors import ConcurrencyError, RegistrationError
from .module import Module, installation_order
from .store import AuditEntry, Commit, Json, StoredEvent, Unit

_SCHEMA = files(__package__).joinpath("schema.sql").read_bytes()

# Held from the moment a unit records events until it commits, so that events become
# visible in the same order as their positions and no subscriber can skip one.
_EVENT_ORDER_LOCK = 7_154_697_431

_MAX_IDENTIFIER = 63  # PostgreSQL's NAMEDATALEN - 1, in bytes
_UNIQUE_PREFIX = "unique "

# A unique violation is retried because two units can race to claim the same value (an
# idempotency key, a code): the retry then sees the winner and is refused or replayed.

_UNSERIALISABLE = (errors.SerializationFailure, errors.DeadlockDetected, errors.UniqueViolation)


class PostgresStore:
    runs_sql = True

    def __init__(self, dsn: str, *, connections: int = 10, timeout: float = 10.0) -> None:
        self._pool = ConnectionPool(
            dsn, min_size=1, max_size=connections, configure=_serialisable, open=False
        )
        self._pool.open(wait=True, timeout=timeout)

    def install(self, modules: Iterable[Module] = ()) -> None:
        """Create the kernel's tables, then each module's migrations, indexes and rules.

        Modules are installed with everything they require, dependencies first. Each is
        recorded with a fingerprint of its storage declarations, so that a kernel can
        refuse to run a module the store was not installed with.
        """
        with self._pool.connection() as connection:
            connection.execute(_SCHEMA)
            for module in installation_order(modules):
                _install_unique_indexes(connection, module)
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
                connection.execute(
                    "INSERT INTO installed_modules (module, fingerprint) VALUES (%s, %s) "
                    "ON CONFLICT (module) DO UPDATE "
                    "SET fingerprint = EXCLUDED.fingerprint, installed_at = now()",
                    (module.name, fingerprint(module)),
                )

    def missing(self, modules: Sequence[Module]) -> tuple[str, ...]:
        """Why this store cannot yet run these modules. Empty when it can."""
        with self._pool.connection() as connection:
            (ready,) = connection.execute(
                "SELECT to_regclass('installed_modules') IS NOT NULL"
            ).fetchone() or (False,)
            if not ready:
                return ("nothing has been installed in this database yet",)
            installed = dict(
                connection.execute("SELECT module, fingerprint FROM installed_modules").fetchall()
            )
        problems: list[str] = []
        for module in modules:
            if module.name not in installed:
                problems.append(f"module {module.name!r} is not installed in this database")
            elif installed[module.name] != fingerprint(module):
                problems.append(f"module {module.name!r} was installed from a different definition")
        return tuple(problems)

    def close(self) -> None:
        self._pool.close()

    @contextmanager
    def unit(self) -> Generator[Unit]:
        try:
            with self._pool.connection() as connection:
                yield _PostgresUnit(connection)
        except _UNSERIALISABLE as conflict:
            constraint = conflict.diag.constraint_name
            detail = f" (conflict on {constraint})" if constraint else ""
            raise ConcurrencyError(
                f"the database could not serialise this unit of work{detail}"
            ) from conflict


def fingerprint(module: Module) -> str:
    """A digest of everything about a module that installing it puts in the database."""
    declared = {
        "migrations": [(migration.name, migration.sql) for migration in module.migrations],
        "sql": module.sql,
        "unique": sorted(_unique_indexes(module)),
    }
    return hashlib.sha256(json.dumps(declared, sort_keys=True).encode()).hexdigest()


def _unique_indexes(module: Module) -> dict[str, tuple[str, str]]:
    """Index name to (document kind, field), for every unique field the module declares."""
    indexes: dict[str, tuple[str, str]] = {}
    for kind, document in module.documents.items():
        for field in document.unique:
            name = f"{_UNIQUE_PREFIX}{kind}.{field}"
            if len(name.encode()) > _MAX_IDENTIFIER:
                raise RegistrationError(f"{kind}.{field} is too long a name to index; shorten it")
            indexes[name] = (kind, field)
    return indexes


def _install_unique_indexes(connection: Connection[Any], module: Module) -> None:
    """Create the module's unique indexes, and drop any for fields no longer unique.

    A stale index would go on refusing values the module now allows, and its violations
    would look like contention rather than a rule.
    """
    declared = _unique_indexes(module)
    ours = f"{_UNIQUE_PREFIX}{module.name}."
    existing = {
        name
        for (name,) in connection.execute(
            "SELECT indexname FROM pg_indexes WHERE tablename = 'documents'"
        ).fetchall()
        if name.startswith(ours)
    }
    for name in sorted(existing - declared.keys()):
        connection.execute(sql.SQL("DROP INDEX {name}").format(name=sql.Identifier(name)))
    for name, (kind, field) in declared.items():
        connection.execute(
            sql.SQL(
                "CREATE UNIQUE INDEX IF NOT EXISTS {name} ON documents ((body ->> {field})) "
                "WHERE kind = {kind}"
            ).format(name=sql.Identifier(name), field=sql.Literal(field), kind=sql.Literal(kind))
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

    def secret(self, key: UUID) -> bytes | None:
        row = self._connection.execute(
            "SELECT secret FROM data_keys WHERE id = %s", (key,)
        ).fetchone()
        return bytes(row[0]) if row else None

    def audit(self) -> list[AuditEntry]:
        rows = self._connection.execute(
            "SELECT at, actor, action, input, documents, events, cause, key FROM audit ORDER BY id"
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
                key=key,
            )
            for at, actor, action, input, documents, events, cause, key in rows
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
        for data_key in commit.keys:
            execute(
                "INSERT INTO data_keys (id, secret) VALUES (%s, %s)", (data_key.id, data_key.secret)
            )
            for subject in data_key.subjects:
                execute(
                    "INSERT INTO data_key_subjects (key_id, subject) VALUES (%s, %s)",
                    (data_key.id, subject),
                )
        if commit.erasures:
            execute(
                "DELETE FROM data_keys WHERE id IN "
                "(SELECT key_id FROM data_key_subjects WHERE subject = ANY(%s))",
                (list(commit.erasures),),
            )
        execute(
            "INSERT INTO audit (at, actor, action, input, documents, events, cause, key) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
            (
                audit.at,
                Jsonb(audit.actor.model_dump(mode="json")),
                audit.action,
                Jsonb(audit.input),
                Jsonb([[kind, str(id), version] for kind, id, version in audit.documents]),
                Jsonb(list(audit.events)),
                audit.cause,
                audit.key,
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
