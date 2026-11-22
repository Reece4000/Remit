"""Following what changes: the audit trail by position, the changes feed, a document's
history, and being told when anything commits.

Exercised through a toy depot, where loading a crate writes the crate and its manifest
together, and porters may read crates but not manifests.
"""

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from uuid import UUID, uuid7

import pytest
from conftest import ROOT, kernel_on

from remit.kernel import (
    Actor,
    ActorKind,
    AuditEntry,
    Change,
    Command,
    Context,
    Document,
    Event,
    Kernel,
    Lifecycle,
    Module,
    PostgresStore,
    Problem,
    Rejected,
    Store,
    Transition,
    Unauthorised,
)
from remit.kernel.store import Commit

depot = Module("depot")


@depot.document
class Crate(Document):
    """A crate in the depot."""

    lifecycle = Lifecycle(initial="stored", transitions=(Transition("ship", "stored", "shipped"),))
    label: str


@depot.document
class Manifest(Document):
    """What a crate holds, which porters are not shown."""

    lifecycle = Lifecycle(initial="kept")
    crate_id: UUID
    contents: str


@depot.event
class CrateLoaded(Event):
    """A crate was stored."""

    crate_id: UUID


class Load(Command[Crate]):
    label: str
    contents: str


class Relabel(Command[Crate]):
    crate_id: UUID
    label: str


class Note(Command[Manifest]):
    crate_id: UUID
    contents: str


@depot.command
def load(command: Load, context: Context) -> Crate:
    """Store a crate with its manifest."""
    crate = context.create(Crate(label=command.label))
    context.create(Manifest(crate_id=crate.id, contents=command.contents))
    context.emit(CrateLoaded(crate_id=crate.id))
    return crate


@depot.command
def relabel(command: Relabel, context: Context) -> Crate:
    """Change a crate's label."""
    return context.update(context.get(Crate, command.crate_id), label=command.label)


@depot.command
def note(command: Note, context: Context) -> Manifest:
    """Record a further manifest for a crate."""
    return context.create(Manifest(crate_id=command.crate_id, contents=command.contents))


CLERK = Actor(id="clerk", kind=ActorKind.HUMAN, permissions=frozenset({"depot.*"}))
PORTER = Actor(id="porter", kind=ActorKind.HUMAN, permissions=frozenset({"depot.read.crate"}))
NOBODY = Actor(id="nobody", kind=ActorKind.HUMAN)


@pytest.fixture
def kernel(store: Store) -> Kernel:
    return kernel_on(store, [depot])


def stock(kernel: Kernel) -> tuple[Crate, Crate]:
    """Two crates, one relabelled, and a manifest noted on its own: four units of work."""
    apples = kernel.execute(Load(label="A1", contents="apples"), actor=CLERK).result
    pears = kernel.execute(Load(label="P1", contents="pears"), actor=CLERK).result
    kernel.execute(Note(crate_id=apples.id, contents="more apples"), actor=CLERK)
    kernel.execute(Relabel(crate_id=apples.id, label="A2"), actor=CLERK)
    return apples, pears


def test_the_audit_trail_is_numbered_and_read_from_a_position(kernel: Kernel) -> None:
    stock(kernel)

    trail = kernel.audit(actor=ROOT)
    assert [entry.position for entry in trail] == sorted(entry.position for entry in trail)
    assert len({entry.position for entry in trail}) == 4
    first, second, *_ = trail
    assert kernel.audit(actor=ROOT, after=first.position, limit=1) == [second]
    assert kernel.audit(actor=ROOT, after=trail[-1].position) == []


def test_the_audit_trail_of_one_document(kernel: Kernel) -> None:
    apples, pears = stock(kernel)

    actions = [e.action for e in kernel.audit(actor=ROOT, document=(Crate, apples.id))]
    assert actions == ["depot.load", "depot.relabel"]
    assert [e.action for e in kernel.audit(actor=ROOT, document=(Crate, pears.id))] == [
        "depot.load"
    ]
    assert kernel.audit(actor=ROOT, document=(Crate, uuid7())) == []


def test_the_changes_feed_shows_each_reader_only_what_they_may_read(kernel: Kernel) -> None:
    apples, pears = stock(kernel)

    everything = kernel.changes(actor=CLERK)
    assert [change.action for change in everything.items] == [
        "depot.load",
        "depot.load",
        "depot.note",
        "depot.relabel",
    ]
    assert {kind for change in everything.items for kind, _, _ in change.documents} == {
        "depot.crate",
        "depot.manifest",
    }

    seen = kernel.changes(actor=PORTER)
    assert [(c.action, c.documents) for c in seen.items] == [
        ("depot.load", (("depot.crate", apples.id, 1),)),
        ("depot.load", (("depot.crate", pears.id, 1),)),
        ("depot.relabel", (("depot.crate", apples.id, 2),)),
    ]
    assert all((c.actor, c.actor_kind) == ("clerk", ActorKind.HUMAN) for c in seen.items)
    assert seen.position == everything.position == kernel.audit(actor=ROOT)[-1].position


def test_following_from_the_feeds_position_sees_only_what_is_new(kernel: Kernel) -> None:
    assert kernel.position(actor=NOBODY) == 0
    stock(kernel)
    now = kernel.position(actor=NOBODY)
    assert now == kernel.audit(actor=ROOT)[-1].position
    assert kernel.changes(actor=CLERK, after=now).items == ()

    kernel.execute(Load(label="F1", contents="figs"), actor=CLERK)
    assert [c.action for c in kernel.changes(actor=CLERK, after=now).items] == ["depot.load"]


def test_reading_on_through_the_feed_always_advances(kernel: Kernel) -> None:
    stock(kernel)

    nothing = kernel.changes(actor=NOBODY, limit=2)
    assert nothing.items == ()
    assert nothing.position == kernel.audit(actor=ROOT)[1].position

    read: list[Change] = []
    position = 0
    while (stretch := kernel.changes(actor=PORTER, after=position, limit=1)).position != position:
        read += stretch.items
        position = stretch.position
    assert read == list(kernel.changes(actor=PORTER).items)


def test_a_documents_history_is_for_those_who_may_read_it(kernel: Kernel) -> None:
    apples, _ = stock(kernel)

    history = kernel.history(Crate, apples.id, actor=PORTER)
    assert [(c.action, c.documents) for c in history.items] == [
        ("depot.load", (("depot.crate", apples.id, 1),)),
        ("depot.relabel", (("depot.crate", apples.id, 2),)),
    ]
    later = kernel.history(Crate, apples.id, actor=PORTER, after=history.items[0].position)
    assert [c.action for c in later.items] == ["depot.relabel"]

    with pytest.raises(Unauthorised, match=r"porter does not hold the permission depot\.read"):
        kernel.history(Manifest, uuid7(), actor=PORTER)


@pytest.mark.parametrize("limit", [0, 201])
def test_a_stretch_of_changes_is_bounded(kernel: Kernel, limit: int) -> None:
    with pytest.raises(Rejected, match="limit must be between 1 and 200") as refused:
        kernel.changes(actor=CLERK, limit=limit)
    assert refused.value.problems == (Problem(("limit",), "must be between 1 and 200"),)
    with pytest.raises(Rejected, match="limit must be between 1 and 200"):
        kernel.history(Crate, uuid7(), actor=CLERK, limit=limit)


def test_the_event_log_is_read_a_stretch_at_a_time(kernel: Kernel) -> None:
    for number in range(3):
        kernel.execute(Load(label=f"C{number}", contents="x"), actor=CLERK)

    first, second, third = kernel.events(actor=ROOT)
    assert kernel.events(actor=ROOT, after=first.position, limit=1) == [second]
    assert kernel.events(actor=ROOT, after=second.position) == [third]


def test_a_listener_is_told_of_commits_and_of_nothing_else(kernel: Kernel) -> None:
    with kernel.store.listen() as listener:
        assert not listener.wait(0.05)

        kernel.execute(Load(label="A1", contents="apples"), actor=CLERK, dry_run=True)
        kernel.page(Crate, actor=CLERK)
        assert not listener.wait(0.05)

        kernel.execute(Load(label="A1", contents="apples"), actor=CLERK)
        assert listener.wait(5)
        assert not listener.wait(0.05)


def test_a_waiting_listener_wakes_when_another_thread_commits(kernel: Kernel) -> None:
    with kernel.store.listen() as listener:
        woke = threading.Event()

        def wait() -> None:
            if listener.wait(10):
                woke.set()

        waiting = threading.Thread(target=wait)
        waiting.start()
        time.sleep(0.05)
        kernel.execute(Load(label="A1", contents="apples"), actor=CLERK)
        waiting.join(10)
        assert woke.is_set()


def test_a_reader_following_the_feed_never_misses_a_change(postgres: PostgresStore) -> None:
    """Units commit in a different order from the one they began in. A reader that
    follows the feed by position must still see every one of them."""
    kernel = kernel_on(postgres, [depot])
    writers, each = 6, 15
    done = threading.Event()
    followed: list[int] = []

    def follow() -> None:
        position = 0
        while not done.is_set() or position < kernel.changes(actor=CLERK, after=position).position:
            stretch = kernel.changes(actor=CLERK, after=position, limit=200)
            followed.extend(change.position for change in stretch.items)
            position = stretch.position

    def write(writer: int) -> None:
        # Notes emit no events: the trail must keep its order without them.
        for number in range(each):
            kernel.execute(Note(crate_id=uuid7(), contents=f"{writer}-{number}"), actor=CLERK)

    follower = threading.Thread(target=follow)
    follower.start()
    with ThreadPoolExecutor(writers) as threads:
        list(threads.map(write, range(writers)))
    done.set()
    follower.join(30)

    assert followed == [change.position for change in kernel.changes(actor=CLERK, limit=200).items]
    assert len(followed) == writers * each


def test_a_unit_that_has_written_its_entry_holds_back_every_later_commit(
    postgres: PostgresStore,
) -> None:
    """Entries are numbered as they are written, so a later number committed first could
    be read past before the earlier one appears. Commits wait their turn instead."""

    def nothing_but(action: str) -> Commit:
        entry = AuditEntry(datetime.now(UTC), CLERK, action, {}, documents=(), events=())
        return Commit(documents=(), events=(), audit=entry)

    def commit_second() -> None:
        with postgres.unit() as second:
            second.commit(nothing_but("second"))

    with postgres.unit() as first:
        first.commit(nothing_but("first"))
        later = threading.Thread(target=commit_second)
        later.start()
        later.join(0.5)
        assert later.is_alive(), "a later unit committed while an earlier entry was unseen"
    later.join(10)

    with postgres.unit(read_only=True) as reader:
        trail = reader.audit()
    assert [entry.action for entry in trail] == ["first", "second"]
    assert trail[0].position < trail[1].position
