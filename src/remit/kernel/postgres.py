"""A store on PostgreSQL.

Each unit is one SERIALIZABLE transaction on a pooled connection. PostgreSQL aborts a
transaction it cannot fit into a serial order; that surfaces here as `ConcurrencyError`
and the kernel runs the unit again. A read-only unit is also DEFERRABLE: it waits for a
snapshot no writer can invalidate, so it is never aborted and never aborts a writer.
"""

import hashlib
import json
from collections.abc import Callable, Generator, Iterable, Sequence
from contextlib import contextmanager
from importlib.resources import files
from typing import Any, cast
from uuid import UUID

from psycopg import Connection, Cursor, IsolationLevel, errors, sql
from psycopg.abc import QueryNoTemplate
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

from .actor import Actor
from .errors import ConcurrencyError, RegistrationError, Rejected, RemitError
from .module import Migration, Module, installation_order
from .search import VERSION as SEARCH_VERSION
from .search import holding_pattern, ranked_patterns, searched
from .selection import Comparison, Condition, Selection, comparable
from .store import AuditEntry, Commit, Json, Listener, StoredEvent, Unit

_SQL = files(__package__).joinpath("sql")

_KERNEL = "remit.kernel"
"""The kernel's name where it records its own migrations. No module can take it: module
names cannot contain a dot."""

_KERNEL_MIGRATIONS = (
    Migration("0001-tables", _SQL.joinpath("0001-tables.sql").read_text()),
    Migration(
        "0002-idempotency-per-actor", _SQL.joinpath("0002-idempotency-per-actor.sql").read_text()
    ),
    Migration("0003-search", _SQL.joinpath("0003-search.sql").read_text()),
    Migration("0004-commit-order", _SQL.joinpath("0004-commit-order.sql").read_text()),
)
_KERNEL_RULES = _SQL.joinpath("rules.sql").read_text()

# Held from the moment a unit records its events and audit entry until it commits, so
# that both become visible in the order of their positions and no reader following
# either can skip one. Units therefore commit one at a time from that moment on, which
# is a few statements and the commit itself.
_COMMIT_ORDER_LOCK = 7_154_697_431

_COMMITTED = "remit_committed"
"""The channel every commit notifies, for whoever is listening."""

# Held while installing, so that two installs cannot interleave their migrations.
_INSTALL_LOCK = 7_154_697_432

_MAX_IDENTIFIER = 63  # PostgreSQL's NAMEDATALEN - 1, in bytes
_UNIQUE_PREFIX = "unique "

# A unique violation is retried because two units can race to claim the same value (an
# idempotency key, a code): the retry then sees the winner and is refused or replayed.
_UNSERIALISABLE = (errors.SerializationFailure, errors.DeadlockDetected, errors.UniqueViolation)

# The codes the database's own rules raise with (see sql/rules.sql), and modules' too.
_REFUSED = (errors.CheckViolation, errors.IntegrityConstraintViolation)


class PostgresStore:
    runs_sql = True

    def __init__(self, dsn: str, *, connections: int = 10, timeout: float = 10.0) -> None:
        self._dsn = dsn
        self._pool = ConnectionPool(
            dsn, min_size=1, max_size=connections, configure=_serialisable, open=False
        )
        self._pool.open(wait=True, timeout=timeout)

    def install(self, modules: Iterable[Module] = ()) -> None:
        """Install the kernel's tables, then each module's migrations, indexes and rules.

        What listings search is rebuilt for every module whose definition differs from
        the one last installed, so it always holds what the module now declares.

        Modules are installed with everything they require, dependencies first. Each
        migration is applied once and recorded with a digest; one that has changed since
        it was applied is refused, since applying it again is not possible and skipping
        it would leave the database out of step with its definition. Each module, and
        the kernel, is recorded with a fingerprint of its storage declarations, so that
        a kernel can refuse to run what the store was not installed with.

        All of it happens in one transaction: an install that fails changes nothing.
        """
        with self._connection(read_only=False) as connection:
            connection.execute("SELECT pg_advisory_xact_lock(%s)", (_INSTALL_LOCK,))
            connection.execute(_SQL.joinpath("bootstrap.sql").read_bytes())
            _apply(connection, _KERNEL, _KERNEL_MIGRATIONS, _KERNEL_RULES)
            installed = dict(
                connection.execute("SELECT module, fingerprint FROM installed_modules").fetchall()
            )
            refolded = installed.get(_KERNEL) != _kernel_fingerprint()
            _record(connection, _KERNEL, _kernel_fingerprint())
            for module in installation_order(modules):
                _install_unique_indexes(connection, module)
                _apply(connection, module.name, module.migrations, module.sql)
                if refolded or installed.get(module.name) != fingerprint(module):
                    _index_for_search(connection, module)
                _record(connection, module.name, fingerprint(module))

    def missing(self, modules: Sequence[Module]) -> tuple[str, ...]:
        """Why this store cannot yet run these modules. Empty when it can."""
        with self._connection(read_only=True) as connection:
            (ready,) = connection.execute(
                "SELECT to_regclass('installed_modules') IS NOT NULL"
            ).fetchone() or (False,)
            if not ready:
                return ("nothing has been installed in this database yet",)
            installed = dict(
                connection.execute("SELECT module, fingerprint FROM installed_modules").fetchall()
            )
        problems: list[str] = []
        if installed.get(_KERNEL) != _kernel_fingerprint():
            problems.append("the kernel's tables were installed by a different version of Remit")
        for module in modules:
            if module.name not in installed:
                problems.append(f"module {module.name!r} is not installed in this database")
            elif installed[module.name] != fingerprint(module):
                problems.append(f"module {module.name!r} was installed from a different definition")
        return tuple(problems)

    def close(self) -> None:
        self._pool.close()

    @contextmanager
    def listen(self) -> Generator[Listener]:
        """A connection of its own, outside the pool, listening for commits."""
        with Connection.connect(self._dsn, autocommit=True) as connection:
            connection.execute(sql.SQL("LISTEN {}").format(sql.Identifier(_COMMITTED)))
            yield _PostgresListener(connection)

    @contextmanager
    def unit(self, *, read_only: bool = False) -> Generator[Unit]:
        with _translated(), self._connection(read_only=read_only) as connection:
            yield _PostgresUnit(connection)

    @contextmanager
    def _connection(self, *, read_only: bool) -> Generator[Connection[Any]]:
        with self._pool.connection() as connection:
            # Set on every use: a pooled connection keeps what its last user set.
            connection.read_only = read_only
            connection.deferrable = read_only
            yield connection


def fingerprint(module: Module) -> str:
    """A digest of everything about a module that installing it puts in the database."""
    return _digest(
        {
            "migrations": [(migration.name, migration.sql) for migration in module.migrations],
            "sql": module.sql,
            "unique": sorted(_unique_indexes(module)),
            "searchable": {
                kind: document.searchable for kind, document in module.documents.items()
            },
        }
    )


def _kernel_fingerprint() -> str:
    return _digest(
        {
            "migrations": [(m.name, m.sql) for m in _KERNEL_MIGRATIONS],
            "sql": _KERNEL_RULES,
            "search": SEARCH_VERSION,
        }
    )


def _digest(declared: object) -> str:
    return hashlib.sha256(json.dumps(declared, sort_keys=True).encode()).hexdigest()


def _apply(
    connection: Connection[Any], owner: str, migrations: Sequence[Migration], rules: str
) -> None:
    """Apply what has not been applied of `owner`'s migrations, in order, then its rules."""
    for migration in migrations:
        digest = hashlib.sha256(migration.sql.encode()).hexdigest()
        row = connection.execute(
            "SELECT digest FROM migrations WHERE module = %s AND name = %s",
            (owner, migration.name),
        ).fetchone()
        if row is None:
            connection.execute(migration.sql.encode())
            connection.execute(
                "INSERT INTO migrations (module, name, digest) VALUES (%s, %s, %s)",
                (owner, migration.name, digest),
            )
        elif row[0] is None:
            connection.execute(
                "UPDATE migrations SET digest = %s WHERE module = %s AND name = %s",
                (digest, owner, migration.name),
            )
        elif row[0] != digest:
            raise RegistrationError(
                f"migration {migration.name!r} of {owner} has changed since it was applied; "
                "a migration that has been applied must never change, so restore it and "
                "put the change in a new migration"
            )
    if rules:
        connection.execute(rules.encode())


def _record(connection: Connection[Any], owner: str, fingerprint: str) -> None:
    connection.execute(
        "INSERT INTO installed_modules (module, fingerprint) VALUES (%s, %s) "
        "ON CONFLICT (module) DO UPDATE "
        "SET fingerprint = EXCLUDED.fingerprint, installed_at = now()",
        (owner, fingerprint),
    )


def _unique_indexes(module: Module) -> dict[str, tuple[str, tuple[str, ...]]]:
    """Index name to (document kind, fields), for everything the module declares unique."""
    indexes: dict[str, tuple[str, tuple[str, ...]]] = {}
    for kind, document in module.documents.items():
        for fields in document.unique_together():
            declared = f"{kind}.{'+'.join(fields)}"
            name = f"{_UNIQUE_PREFIX}{declared}"
            if len(name.encode()) > _MAX_IDENTIFIER:
                raise RegistrationError(f"{declared} is too long a name to index; shorten it")
            indexes[name] = (kind, fields)
    return indexes


def _install_unique_indexes(connection: Connection[Any], module: Module) -> None:
    """Create the module's unique indexes, and drop any for fields no longer unique.

    A stale index would go on refusing values the module now allows, and its violations
    would look like contention rather than a rule.
    """
    declared = _unique_indexes(module)
    existing = {
        name
        for (name,) in connection.execute(
            "SELECT indexname FROM pg_indexes WHERE tablename = 'documents'"
        ).fetchall()
        if name.startswith(_UNIQUE_PREFIX)
        and name.removeprefix(_UNIQUE_PREFIX).split(".")[0] == module.name
    }
    for name in sorted(existing - declared.keys()):
        connection.execute(sql.SQL("DROP INDEX {name}").format(name=sql.Identifier(name)))
    for name, (kind, fields) in declared.items():
        # A value left unset reads as NULL, and NULLs never collide: as in the kernel.
        values = sql.SQL(", ").join(
            sql.SQL("(body ->> {field})").format(field=sql.Literal(field)) for field in fields
        )
        connection.execute(
            sql.SQL(
                "CREATE UNIQUE INDEX IF NOT EXISTS {name} "
                "ON documents ({values}) WHERE kind = {kind}"
            ).format(name=sql.Identifier(name), values=values, kind=sql.Literal(kind))
        )


def _index_for_search(connection: Connection[Any], module: Module) -> None:
    """Rebuild what listings search for a module's documents, from the documents themselves."""
    connection.execute(
        "DELETE FROM document_search WHERE kind = ANY(%s)", (list(module.documents),)
    )
    for kind, document in module.documents.items():
        if not document.searchable:
            continue
        # A named cursor reads from the server a batch at a time, however many there are.
        with connection.cursor(name="remit_search") as stored, connection.cursor() as index:
            stored.execute("SELECT id, body FROM documents WHERE kind = %s", (kind,))
            while batch := stored.fetchmany(1000):
                index.executemany(
                    "INSERT INTO document_search (kind, id, text) VALUES (%s, %s, %s)",
                    [(kind, id, searched(body, document.searchable)) for id, body in batch],
                )


def _serialisable(connection: Connection[Any]) -> None:
    connection.isolation_level = IsolationLevel.SERIALIZABLE


@contextmanager
def _translated() -> Generator[None]:
    """The database's errors as the kernel's: what to retry, and what was refused."""
    try:
        yield
    except _UNSERIALISABLE as conflict:
        constraint = conflict.diag.constraint_name
        detail = f" (conflict on {constraint})" if constraint else ""
        raise ConcurrencyError(
            f"the database could not serialise this unit of work{detail}"
        ) from conflict
    except errors.ReadOnlySqlTransaction as refusal:
        raise RemitError(
            "nothing can be changed through a read; execute a command to change things"
        ) from refusal
    except _REFUSED as refusal:
        raise Rejected(f"the database refused this: {refusal.diag.message_primary}") from refusal


def _wait_for_turn(execute: Callable[..., object]) -> None:
    """Take the commit order lock, held until this unit's transaction ends."""
    execute("SELECT pg_advisory_xact_lock(%s)", (_COMMIT_ORDER_LOCK,))


class _PostgresListener:
    def __init__(self, connection: Connection[Any]) -> None:
        self._connection = connection

    def wait(self, timeout: float) -> bool:
        # Notifications that arrived since the last wait are delivered first, at once.
        notified = self._connection.notifies(timeout=timeout, stop_after=1)
        woke = next(notified, None) is not None
        notified.close()
        return woke


class _PostgresUnit:
    def __init__(self, connection: Connection[Any]) -> None:
        self._connection = connection

    def _execute(self, query: QueryNoTemplate, parameters: Sequence[object] = ()) -> Cursor[Any]:
        # Translated here as well as when the unit ends, so that a handler running SQL
        # sees a conflict as the `ConcurrencyError` it is.
        with _translated():
            return self._connection.execute(query, parameters)

    def get(self, kind: str, id: UUID) -> Json | None:
        row = self._execute(
            "SELECT body FROM documents WHERE kind = %s AND id = %s", (kind, id)
        ).fetchone()
        return cast(Json, row[0]) if row else None

    def select(self, selection: Selection) -> list[Json]:
        rows = self._execute(*_statement(selection)).fetchall()
        return [cast(Json, row[0]) for row in rows]

    def events(
        self, *, after: int = 0, name: str | None = None, limit: int | None = None
    ) -> list[StoredEvent]:
        rows = self._execute(
            """
            SELECT position, name, body, at, actor FROM events
            WHERE position > %s AND (%s::text IS NULL OR name = %s)
            ORDER BY position LIMIT %s
            """,
            (after, name, name, limit),
        ).fetchall()
        return [StoredEvent(*row) for row in rows]

    def cursor(self, subscription: str) -> int:
        row = self._execute(
            "SELECT position FROM cursors WHERE subscription = %s", (subscription,)
        ).fetchone()
        return cast(int, row[0]) if row else 0

    def idempotency(self, actor: str, key: str) -> Json | None:
        row = self._execute(
            "SELECT record FROM idempotency WHERE actor = %s AND key = %s", (actor, key)
        ).fetchone()
        return cast(Json, row[0]) if row else None

    def secret(self, key: UUID) -> bytes | None:
        row = self._execute("SELECT secret FROM data_keys WHERE id = %s", (key,)).fetchone()
        return bytes(row[0]) if row else None

    def audit(
        self, *, after: int = 0, document: tuple[str, UUID] | None = None, limit: int | None = None
    ) -> list[AuditEntry]:
        if document is None:
            rows = self._execute(
                "SELECT at, actor, action, input, documents, events, cause, key, id FROM audit "
                "WHERE id > %s ORDER BY id LIMIT %s",
                (after, limit),
            ).fetchall()
        else:
            # Containment reaches into each [kind, id, version]: [kind, id] is held by it.
            rows = self._execute(
                "SELECT at, actor, action, input, documents, events, cause, key, id FROM audit "
                "WHERE id > %s AND documents @> %s ORDER BY id LIMIT %s",
                (after, Jsonb([[document[0], str(document[1])]]), limit),
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
                position=position,
            )
            for at, actor, action, input, documents, events, cause, key, position in rows
        ]

    def sql(self, query: str, parameters: Sequence[object] = ()) -> list[tuple[Any, ...]]:
        cursor = self._execute(query.encode(), parameters)
        return cursor.fetchall() if cursor.description else []

    def rollback(self) -> None:
        self._connection.rollback()

    def rehearse(self, commit: Commit) -> None:
        self.commit(commit)
        self.rollback()

    def commit(self, commit: Commit) -> None:
        execute = self._execute
        for document in commit.documents:
            values = (document.version, document.final, Jsonb(document.body))
            key = (document.kind, document.id)
            if document.version == 1:
                execute(
                    "INSERT INTO documents (version, final, body, kind, id) "
                    "VALUES (%s, %s, %s, %s, %s)",
                    (*values, *key),
                )
            else:
                updated = execute(
                    "UPDATE documents SET version = %s, final = %s, body = %s "
                    "WHERE kind = %s AND id = %s AND version = %s",
                    (*values, *key, document.version - 1),
                )
                if updated.rowcount != 1:
                    raise ConcurrencyError(
                        f"{document.kind} {document.id} changed since it was read"
                    )
            if document.search is not None:
                execute(
                    "INSERT INTO document_search (kind, id, text) VALUES (%s, %s, %s) "
                    "ON CONFLICT (kind, id) DO UPDATE SET text = EXCLUDED.text",
                    (*key, document.search),
                )

        audit = commit.audit
        _wait_for_turn(execute)
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
            actor, key, record = commit.idempotency
            execute(
                "INSERT INTO idempotency (actor, key, record) VALUES (%s, %s, %s)",
                (actor, key, Jsonb(record)),
            )
        # Delivered when the transaction commits, and not at all if it does not.
        execute("SELECT pg_notify(%s, '')", (_COMMITTED,))


_CASTS = {
    Comparison.TEXT: sql.SQL(' COLLATE "C"'),
    Comparison.NUMBER: sql.SQL("::numeric"),
    Comparison.INSTANT: sql.SQL("::timestamptz"),
}
_RANGES = {"gt": sql.SQL(">"), "gte": sql.SQL(">="), "lt": sql.SQL("<"), "lte": sql.SQL("<=")}


class _Statement:
    """SQL being put together, with the parameters it has used so far, in order."""

    def __init__(self) -> None:
        self.parameters: list[object] = []

    def bound(self, value: object) -> sql.Composable:
        """A placeholder for a value. Values are always bound, never written into the SQL."""
        self.parameters.append(value)
        return sql.Placeholder()

    def value(self, field: str, comparison: Comparison) -> sql.Composable:
        """A document's field, as something that compares the way the field does."""
        return sql.SQL("(d.body ->> {})").format(self.bound(field)) + _CASTS[comparison]

    def given(self, value: Any, comparison: Comparison) -> sql.Composable:
        """A stored value to compare a field with, bound as the type the field is cast to."""
        return self.bound(comparable(value, comparison))

    def holding(self, field: str, value: Any) -> sql.Composable:
        """True for documents that hold exactly this JSON value in the field."""
        return sql.SQL("d.body @> {}").format(self.bound(Jsonb({field: value})))

    def same(self, field: str, value: Any, comparison: Comparison | None) -> sql.Composable:
        if value is None or comparison is None or comparison is Comparison.TEXT:
            return self.holding(field, value)
        # Numbers and instants are equal by value, however each was written down. A field
        # holding null is unequal to any value, not unknown, so that `ne` finds it.
        return sql.SQL("{} IS NOT DISTINCT FROM {}").format(
            self.value(field, comparison), self.given(value, comparison)
        )

    def condition(self, condition: Condition) -> sql.Composable:
        field, comparison = condition.field, condition.comparison
        if condition.operator == "in":
            if not condition.value:
                return sql.SQL("false")
            options = [self.same(field, value, comparison) for value in condition.value]
            return sql.SQL("({})").format(sql.SQL(" OR ").join(options))
        if condition.operator == "eq":
            return self.same(field, condition.value, comparison)
        if condition.operator == "ne":
            return sql.SQL("NOT ({})").format(self.same(field, condition.value, comparison))
        # `select` offers the range operators only on fields whose values have an order.
        ordered = cast(Comparison, comparison)
        return sql.SQL("{} {} {}").format(
            self.value(field, ordered),
            _RANGES[condition.operator],
            self.given(condition.value, ordered),
        )


def _statement(selection: Selection) -> tuple[sql.Composed, list[object]]:
    """A selection as SQL that means what `Selection.arranged` means.

    The inner query finds the documents and works out what each is ordered by; the outer
    one starts after the place the last page ended, orders them and cuts the page.
    """
    statement = _Statement()
    source = sql.SQL("documents AS d")
    conditions: list[sql.Composable] = []
    searching = bool(selection.terms)
    if searching:
        source += sql.SQL(" JOIN document_search AS s ON s.kind = d.kind AND s.id = d.id")

    if selection.ranked:
        exact, starting = ranked_patterns(selection.terms)
        leading: sql.Composable | None = sql.SQL(
            "CASE WHEN s.text LIKE {} THEN 0 WHEN s.text LIKE {} THEN 1 ELSE 2 END"
        ).format(statement.bound(exact), statement.bound(starting))
    elif selection.order == "id":
        leading = None
    else:
        leading = statement.value(selection.order, selection.comparison)

    conditions.append(sql.SQL("d.kind = {}").format(statement.bound(selection.kind)))
    conditions += [statement.condition(condition) for condition in selection.where]
    conditions += [
        sql.SQL("s.text LIKE {}").format(statement.bound(holding_pattern(term)))
        for term in selection.terms
    ]
    found = sql.SQL("SELECT d.body, d.id, {} AS ordered_by FROM {} WHERE {}").format(
        leading or sql.SQL("NULL"), source, sql.SQL(" AND ").join(conditions)
    )

    # Nulls sort after every value, and a descending order is the ascending one reversed.
    direction = sql.SQL("DESC NULLS FIRST" if selection.descending else "ASC NULLS LAST")
    beyond = sql.SQL("<" if selection.descending else ">")
    order = sql.SQL("id {}").format(direction)
    if leading is not None:
        order = sql.SQL("ordered_by {}, ").format(direction) + order

    after = sql.SQL("true")
    if selection.after is not None:
        ended, id = selection.after
        comparison = Comparison.NUMBER if selection.ranked else selection.comparison

        def further() -> sql.Composable:
            return sql.SQL("id {} {}").format(beyond, statement.bound(UUID(id)))

        if leading is None:
            after = further()
        elif ended is None and selection.descending:
            after = sql.SQL("(ordered_by IS NOT NULL OR {})").format(further())
        elif ended is None:
            after = sql.SQL("(ordered_by IS NULL AND {})").format(further())
        else:
            after = sql.SQL("(ordered_by {} {} OR (ordered_by = {} AND {}){})").format(
                beyond,
                statement.given(ended, comparison),
                statement.given(ended, comparison),
                further(),
                sql.SQL("" if selection.descending else " OR ordered_by IS NULL"),
            )

    return (
        sql.SQL("SELECT body FROM ({}) AS found WHERE {} ORDER BY {} LIMIT {}").format(
            found, after, order, statement.bound(selection.limit)
        ),
        statement.parameters,
    )
