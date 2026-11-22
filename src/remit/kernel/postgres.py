"""A store on PostgreSQL.

Each unit is one SERIALIZABLE transaction on a pooled connection. PostgreSQL aborts a
transaction it cannot fit into a serial order; that surfaces here as `ConcurrencyError`
and the kernel runs the unit again. A read-only unit is also DEFERRABLE: it waits for a
snapshot no writer can invalidate, so it is never aborted and never aborts a writer.
"""

import hashlib
import json
from collections.abc import Callable, Collection, Generator, Iterable, Mapping, Sequence
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
from .module import Migration, Module
from .registry import Registry
from .search import VERSION as SEARCH_VERSION
from .search import holding_pattern, ranked_patterns, searched
from .selection import Comparison, Condition, Selection, Tally, comparable, defaults
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
        the one last installed, so it always holds what the module now declares: for its
        own documents, and for the documents it extends.

        Modules are installed with everything they require, dependencies first. Each
        migration is applied once and recorded with a digest; one that has changed since
        it was applied is refused, since applying it again is not possible and skipping
        it would leave the database out of step with its definition. Each module, and
        the kernel, is recorded with a fingerprint of its storage declarations, so that
        a kernel can refuse to run what the store was not installed with.

        All of it happens in one transaction: an install that fails changes nothing.
        """
        registry = Registry(modules)
        with self._connection(read_only=False) as connection:
            connection.execute("SELECT pg_advisory_xact_lock(%s)", (_INSTALL_LOCK,))
            connection.execute(_SQL.joinpath("bootstrap.sql").read_bytes())
            _apply(connection, _KERNEL, _KERNEL_MIGRATIONS, _KERNEL_RULES)
            installed = dict(
                connection.execute("SELECT module, fingerprint FROM installed_modules").fetchall()
            )
            refolded = installed.get(_KERNEL) != _kernel_fingerprint()
            _record(connection, _KERNEL, _kernel_fingerprint())
            stale: dict[str, None] = {}
            for module in registry.modules.values():
                _install_unique_indexes(connection, module)
                _apply(connection, module.name, module.migrations, module.sql)
                if refolded or installed.get(module.name) != fingerprint(module):
                    hosts = (registry.host(e) for e in module.extensions.values())
                    stale |= dict.fromkeys([*module.documents, *map(registry.document_name, hosts)])
                _record(connection, module.name, fingerprint(module))
            _index_for_search(connection, registry, stale)

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
            # What documents written before an extension was installed are searched for.
            "extensions": {
                name: {field: defaults(extension)[field] for field in extension.searchable}
                for name, extension in module.extensions.items()
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


def _index_for_search(
    connection: Connection[Any], registry: Registry, kinds: Collection[str]
) -> None:
    """Rebuild what listings search for documents of these kinds, from the documents
    themselves: their own searchable fields and their extensions'."""
    connection.execute("DELETE FROM document_search WHERE kind = ANY(%s)", (list(kinds),))
    for kind in kinds:
        document = registry.extended(registry.document_type(kind))
        if not document.searchable:
            continue
        unset = defaults(document)
        # A named cursor reads from the server a batch at a time, however many there are.
        with connection.cursor(name="remit_search") as stored, connection.cursor() as index:
            stored.execute("SELECT id, body FROM documents WHERE kind = %s", (kind,))
            while batch := stored.fetchmany(1000):
                index.executemany(
                    "INSERT INTO document_search (kind, id, text) VALUES (%s, %s, %s)",
                    [
                        (kind, id, searched({**unset, **body}, document.searchable))
                        for id, body in batch
                    ],
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

    def count(self, selection: Selection) -> int:
        (counted,) = cast(tuple[int], self._execute(*_count_statement(selection)).fetchone())
        return counted

    def tally(self, tally: Tally) -> list[tuple[Any, int]]:
        rows = self._execute(*_tally_statement(tally)).fetchall()
        return [(value, count) for value, count in rows]

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

    def position(self) -> int:
        row = self._execute("SELECT coalesce(max(id), 0) FROM audit").fetchone()
        return int(row[0]) if row else 0

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
_MONEY = (("currency", Comparison.TEXT), ("amount", Comparison.NUMBER))
"""The parts money is compared by, most significant first, and how each compares."""


class _Statement:
    """SQL being put together, with the parameters it has used so far, in order."""

    def __init__(self, defaults: Mapping[str, Any]) -> None:
        self.parameters: list[object] = []
        self.defaults = defaults
        """What each field with a default holds, as stored, where a body holds nothing for it."""

    def bound(self, value: object) -> sql.Composable:
        """A placeholder for a value. Values are always bound, never written into the SQL."""
        self.parameters.append(value)
        return sql.Placeholder()

    def held(self, field: str) -> sql.Composable:
        """A document's field, as the JSON it holds: its default where the body holds
        nothing for it."""
        default = self.defaults.get(field)
        if default is None:
            return sql.SQL("(d.body -> {})").format(self.bound(field))
        return sql.SQL("coalesce(d.body -> {}, {})").format(
            self.bound(field), self.bound(Jsonb(default))
        )

    def value(self, field: str, comparison: Comparison) -> sql.Composable:
        """A document's field, as something that compares the way the field does: its
        default where the body holds nothing for it."""
        return sql.SQL("({} #>> '{{}}')").format(self.held(field)) + _CASTS[comparison]

    def part(self, field: str, name: str, comparison: Comparison) -> sql.Composable:
        """One part of a money field, as something that compares the way that part does."""
        held = sql.SQL("({} ->> {})").format(self.held(field), sql.Literal(name))
        return held + _CASTS[comparison]

    def given(self, value: Any, comparison: Comparison) -> sql.Composable:
        """A stored value to compare a field with, bound as the type the field is cast to."""
        return self.bound(comparable(value, comparison))

    def holding(self, field: str, value: Any) -> sql.Composable:
        """True for documents that hold exactly this JSON value in the field, or hold
        nothing for it when this is its default."""
        holds = sql.SQL("d.body @> {}").format(self.bound(Jsonb({field: value})))
        if field not in self.defaults or self.defaults[field] != value:
            return holds
        return sql.SQL("({} OR NOT d.body ? {})").format(holds, self.bound(field))

    def same(self, field: str, value: Any, comparison: Comparison | None) -> sql.Composable:
        if value is None or comparison is None or comparison is Comparison.TEXT:
            return self.holding(field, value)
        if comparison is Comparison.MONEY:
            # The same currency and the same amount. A field holding null holds neither.
            equal = [
                sql.SQL("{} IS NOT DISTINCT FROM {}").format(
                    self.part(field, name, compared), self.given(value[name], compared)
                )
                for name, compared in _MONEY
            ]
            return sql.SQL("({})").format(sql.SQL(" AND ").join(equal))
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
        if ordered is Comparison.MONEY:
            # An amount in one currency is neither more nor less than an amount in another.
            (currency, by_currency), (amount, by_amount) = _MONEY
            return sql.SQL("({} = {} AND {} {} {})").format(
                self.part(field, currency, by_currency),
                self.given(condition.value[currency], by_currency),
                self.part(field, amount, by_amount),
                _RANGES[condition.operator],
                self.given(condition.value[amount], by_amount),
            )
        return sql.SQL("{} {} {}").format(
            self.value(field, ordered),
            _RANGES[condition.operator],
            self.given(condition.value, ordered),
        )


def _direction(descending: bool) -> sql.Composable:
    """Nulls sort after every value, and a descending order is the ascending one reversed."""
    return sql.SQL("DESC NULLS FIRST" if descending else "ASC NULLS LAST")


def _matching(statement: _Statement, selection: Selection) -> sql.Composed:
    """`FROM … WHERE …` for the documents a selection matches, on every page together."""
    source = sql.SQL("documents AS d")
    if selection.terms:
        source += sql.SQL(" JOIN document_search AS s ON s.kind = d.kind AND s.id = d.id")
    conditions = [sql.SQL("d.kind = {}").format(statement.bound(selection.kind))]
    conditions += [statement.condition(condition) for condition in selection.where]
    conditions += [
        sql.SQL("s.text LIKE {}").format(statement.bound(holding_pattern(term)))
        for term in selection.terms
    ]
    return sql.SQL("FROM {} WHERE {}").format(source, sql.SQL(" AND ").join(conditions))


def _statement(selection: Selection) -> tuple[sql.Composed, list[object]]:
    """A selection as SQL that means what `Selection.arranged` means.

    The inner query finds the documents and works out what each is ordered by; the outer
    one starts after the place the last page ended, orders them and cuts the page.
    """
    statement = _Statement(selection.defaults)
    # What each document is ordered by before its id: an expression, how it compares,
    # and whether it is ordered from the highest down. Money is ordered by two: its
    # currency, then its amount.
    keys: list[tuple[sql.Composable, Comparison, bool]] = []
    if selection.ranked:
        exact, starting = ranked_patterns(selection.terms)
        rank = sql.SQL(
            "CASE WHEN s.text LIKE {} THEN 0 WHEN s.text LIKE {} THEN 1 ELSE 2 END"
        ).format(statement.bound(exact), statement.bound(starting))
        keys.append((rank, Comparison.NUMBER, False))
    for key in selection.order:
        if key.comparison is Comparison.MONEY:
            for name, compared in _MONEY:
                keys.append((statement.part(key.field, name, compared), compared, key.descending))
        else:
            expression = statement.value(key.field, key.comparison)
            keys.append((expression, key.comparison, key.descending))
    named = [sql.Identifier(f"ordered_by_{position}") for position in range(len(keys))]
    found = sql.SQL("SELECT d.body, d.id{} {}").format(
        sql.SQL("").join(
            sql.SQL(", {} AS {}").format(expression, name)
            for (expression, _, _), name in zip(keys, named, strict=True)
        ),
        _matching(statement, selection),
    )
    order = sql.SQL(", ").join(
        [
            *(
                sql.SQL("{} {}").format(name, _direction(descending))
                for name, (_, _, descending) in zip(named, keys, strict=True)
            ),
            sql.SQL("id {}").format(_direction(selection.newest_first)),
        ]
    )

    after: sql.Composable = sql.SQL("true")
    if selection.after is not None:
        ended, id = selection.after
        places = list(ended)
        if not selection.ranked:
            pairs = zip(ended, selection.order, strict=True)
            places = [place for value, key in pairs for place in _places(value, key.comparison)]

        def beyond(position: int) -> sql.Composable:
            """True for documents after the place the last page ended, of those that tie
            with it on every value before `position`. Binds values in the order the SQL
            reads them."""
            if position == len(keys):
                return sql.SQL("id {} {}").format(
                    sql.SQL("<" if selection.newest_first else ">"), statement.bound(UUID(id))
                )
            name, value = named[position], places[position]
            _, comparison, descending = keys[position]
            if value is None and descending:
                return sql.SQL("({} IS NOT NULL OR {})").format(name, beyond(position + 1))
            if value is None:
                return sql.SQL("({} IS NULL AND {})").format(name, beyond(position + 1))
            past, tied = statement.given(value, comparison), statement.given(value, comparison)
            return sql.SQL("({} {} {} OR ({} = {} AND {}){})").format(
                name,
                sql.SQL("<" if descending else ">"),
                past,
                name,
                tied,
                beyond(position + 1),
                sql.SQL("") if descending else sql.SQL(" OR {} IS NULL").format(name),
            )

        after = beyond(0)

    return (
        sql.SQL("SELECT body FROM ({}) AS found WHERE {} ORDER BY {} LIMIT {}").format(
            found, after, order, statement.bound(selection.limit)
        ),
        statement.parameters,
    )


def _places(value: Any, comparison: Comparison) -> list[Any]:
    """A stored value as the SQL orders it: money as its currency, then its amount."""
    if comparison is not Comparison.MONEY:
        return [value]
    return [None if value is None else value[name] for name, _ in _MONEY]


def _count_statement(selection: Selection) -> tuple[sql.Composed, list[object]]:
    """How many documents a selection matches, as SQL."""
    statement = _Statement(selection.defaults)
    return (
        sql.SQL("SELECT count(*) {}").format(_matching(statement, selection)),
        statement.parameters,
    )


def _tally_statement(tally: Tally) -> tuple[sql.Composed, list[object]]:
    """A tally as SQL that means what `Tally.counted` means.

    The inner query counts the documents holding each way of writing a value, null and
    absent alike; the outer one gathers the ways of writing one value, by the field's
    comparison, and gives the first of them in code point order.
    """
    statement = _Statement(tally.selection.defaults)
    held = sql.SQL("nullif({}, 'null')").format(statement.held(tally.field))
    written = sql.SQL("SELECT {} AS held, count(*) AS documents {} GROUP BY held").format(
        held, _matching(statement, tally.selection)
    )
    if tally.comparison is Comparison.MONEY:
        compared = [
            sql.SQL("(held ->> {})").format(sql.Literal(name)) + _CASTS[by] for name, by in _MONEY
        ]
    else:
        # Values that have no order, such as true and false, are told apart and ordered as
        # text.
        compared = [sql.SQL("(held #>> '{}')") + _CASTS[tally.comparison or Comparison.TEXT]]
    return (
        sql.SQL(
            "SELECT (array_agg(held ORDER BY held #>> '{{}}' COLLATE \"C\"))[1], "
            "sum(documents)::bigint "
            "FROM ({}) AS written GROUP BY {} ORDER BY {} LIMIT {}"
        ).format(
            written,
            sql.SQL(", ").join(compared),
            sql.SQL(", ").join(each + sql.SQL(" ASC NULLS FIRST") for each in compared),
            statement.bound(tally.limit),
        ),
        statement.parameters,
    )
