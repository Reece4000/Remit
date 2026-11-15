"""Reading: who may read what, listings, search and registered queries.

Exercised through a toy laboratory whose samples carry one field of every kind a
listing can compare.
"""

import base64
import json
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any
from uuid import UUID, uuid7

import psycopg
import pytest
from conftest import DSN, ROOT, kernel_on
from hypothesis import given, settings
from hypothesis import strategies as st
from pydantic import AwareDatetime

from remit.kernel import (
    Actor,
    ActorKind,
    Code,
    Command,
    Context,
    Document,
    Kernel,
    Lifecycle,
    MemoryStore,
    Module,
    Name,
    NotFound,
    Page,
    PostgresStore,
    Problem,
    Query,
    Reader,
    RegistrationError,
    Rejected,
    RemitError,
    Store,
    Transition,
    Unauthorised,
)
from remit.kernel.search import fold
from remit.kernel.selection import columns

lab = Module("lab")


class Grade(StrEnum):
    A = "a"
    B = "b"


@lab.document
class Sample(Document):
    """Something sent to the laboratory for testing."""

    lifecycle = Lifecycle(
        initial="received", transitions=(Transition("discard", "received", "discarded"),)
    )
    unique = ("code",)
    searchable = ("code", "name", "notes")

    code: Code
    name: Name
    notes: tuple[str, ...] = ()
    grade: Grade | None = None
    received: date
    weight: Decimal | None = None
    count: int = 0
    taken_at: AwareDatetime | None = None
    urgent: bool = False
    logged_at: datetime | None = None
    """Written without a time zone, so it can be matched but not ordered."""


@lab.document
class Reading(Document):
    """A measurement taken from a sample. Nothing about it can be searched."""

    lifecycle = Lifecycle(initial="taken")

    sample_id: UUID
    value: Decimal


class Receive(Command[Sample]):
    code: Code
    name: Name
    notes: tuple[str, ...] = ()
    grade: Grade | None = None
    received: date = date(2026, 1, 1)
    weight: Decimal | None = None
    count: int = 0
    taken_at: AwareDatetime | None = None
    urgent: bool = False
    logged_at: datetime | None = None


class Discard(Command[Sample]):
    sample_id: UUID


class ReceiveAndReport(Command[tuple[tuple[str, ...], str | None, int]]):
    code: Code
    name: Name
    weight: Decimal
    discard: UUID


class Heaviest(Query[Sample | None]):
    grade: Grade | None = None
    """Consider only samples of this grade."""


class Tally(Query[int]):
    pass


class WhoIsAsking(Query[tuple[str, str]]):
    pass


@lab.command
def receive(command: Receive, context: Context) -> Sample:
    """Record a sample as received."""
    return context.create(Sample.model_validate(command.model_dump()))


@lab.command
def discard(command: Discard, context: Context) -> Sample:
    """Throw a sample away."""
    return context.transition(context.get(Sample, command.sample_id), "discard")


@lab.query
def heaviest(query: Heaviest, reader: Reader) -> Sample | None:
    """The heaviest sample still held that has been weighed."""
    where: dict[str, object] = {"weight": {"ne": None}, "state": "received"}
    if query.grade:
        where["grade"] = query.grade
    found = reader.page(Sample, where=where, order="-weight", limit=1).items
    return found[0] if found else None


@lab.query
def tally(query: Tally, reader: Reader) -> int:
    """How many samples are still held, with the heaviest counted through another query."""
    held = len(reader.find(Sample, state="received"))
    return held if reader.ask(Heaviest()) else -held


@lab.query
def who_is_asking(query: WhoIsAsking, reader: Reader) -> tuple[str, str]:
    """The actor a query is answered for, and the kind of thing it is given to read with."""
    return reader.actor.id, type(reader).__name__


@lab.command
def receive_and_report(
    command: ReceiveAndReport, context: Context
) -> tuple[tuple[str, ...], str | None, int]:
    """Receive one sample and discard another, then read what the laboratory now holds."""
    context.create(Sample(code=command.code, name=command.name, received=date(2026, 3, 1)))
    context.transition(context.get(Sample, command.discard), "discard")
    held = context.page(Sample, where={"state": "received"}, order="code", limit=3)
    heaviest = context.ask(Heaviest())
    return tuple(s.code for s in held.items), heaviest and heaviest.code, context.ask(Tally())


TECHNICIAN = Actor(id="tess", kind=ActorKind.HUMAN, permissions=frozenset({"lab.*"}))
AUDITOR = Actor(id="agent:audit", kind=ActorKind.AGENT, permissions=frozenset({"*.read.*"}))
OBSERVER = Actor(id="olu", kind=ActorKind.HUMAN, permissions=frozenset({"lab.read.*"}))
COURIER = Actor(id="cal", kind=ActorKind.HUMAN, permissions=frozenset({"lab.receive"}))
COUNTER = Actor(id="agent:count", kind=ActorKind.AGENT, permissions=frozenset({"lab.read.tally"}))

SAMPLES: tuple[dict[str, Any], ...] = (
    {
        "code": "S-001",
        "name": "Müller Feed",
        "notes": ("Fragile", "Keep cold"),
        "grade": "a",
        "received": "2026-01-05",
        "weight": "10.50",
        "count": 3,
        "taken_at": "2026-01-05T09:00:00+01:00",
        "logged_at": "2026-01-05T09:00:00",
    },
    {
        "code": "S-002",
        "name": "muller oats",
        "grade": "b",
        "received": "2026-01-20",
        "weight": "9",
        "count": 12,
        "taken_at": "2026-01-05T08:00:00Z",
        "urgent": True,
    },
    {"code": "S-003", "name": "100% Cotton", "received": "2026-02-01"},
    {
        "code": "S-004",
        "name": "1000 Cotton_Wool",
        "grade": "a",
        "received": "2026-02-01",
        "weight": "10.5",
        "count": 7,
        "taken_at": "2026-02-02T00:00:00Z",
    },
    {
        "code": "S-010",
        "name": "Zebra hair",
        "notes": ("compare with s-001",),
        "grade": "b",
        "received": "2025-12-31",
        "weight": "100",
        "count": 1,
        "taken_at": "2026-02-01T23:59:59.5-01:00",
    },
    {"code": "S-10", "name": "S-001 retest", "received": "2026-01-05", "weight": "-0.25"},
    {"code": "ACME", "name": "acme", "notes": (r"Back\slash & Co",), "received": "2026-01-06"},
    {"code": "Z9", "name": "Straße Nord", "received": "2026-01-07", "count": 12},
)


def stock(kernel: Kernel) -> dict[str, Sample]:
    return {
        sample["code"]: kernel.execute(Receive.model_validate(sample), actor=TECHNICIAN).result
        for sample in SAMPLES
    }


def codes(kernel: Kernel, **listing: Any) -> list[str]:
    """The codes of every sample a listing gives, read a page at a time."""
    found: list[str] = []
    after = None
    for _ in range(len(SAMPLES) + 2):
        page = kernel.page(Sample, actor=ROOT, after=after, **listing)
        found += [sample.code for sample in page.items]
        after = page.next
        if after is None:
            return found
    raise AssertionError("the listing never came to an end")


@pytest.fixture
def kernel(store: Store) -> Kernel:
    return kernel_on(store, [lab])


@pytest.fixture
def stocked(kernel: Kernel) -> dict[str, Sample]:
    return stock(kernel)


# Who may read.


def test_reading_takes_the_permission_to_read(kernel: Kernel, stocked: dict[str, Sample]) -> None:
    sample = stocked["S-001"]

    with pytest.raises(Unauthorised, match=r"cal does not hold the permission lab\.read\.sample"):
        kernel.get(Sample, sample.id, actor=COURIER)
    with pytest.raises(Unauthorised, match=r"lab\.read\.sample"):
        kernel.page(Sample, actor=COURIER)
    with pytest.raises(Unauthorised, match=r"lab\.read\.heaviest"):
        kernel.ask(Heaviest(), actor=COURIER)
    with pytest.raises(Unauthorised, match=r"kernel\.read\.audit"):
        kernel.audit(actor=TECHNICIAN)
    with pytest.raises(Unauthorised, match=r"kernel\.read\.events"):
        kernel.events(actor=TECHNICIAN)


def test_a_module_can_be_read_without_being_changed(
    kernel: Kernel, stocked: dict[str, Sample]
) -> None:
    sample = stocked["S-001"]

    assert kernel.get(Sample, sample.id, actor=OBSERVER) == sample
    assert len(kernel.page(Sample, actor=OBSERVER).items) == len(SAMPLES)
    assert kernel.ask(Heaviest(), actor=OBSERVER) == stocked["S-010"]
    with pytest.raises(Unauthorised, match=r"olu does not hold the permission lab\.discard"):
        kernel.execute(Discard(sample_id=sample.id), actor=OBSERVER)


def test_everything_can_be_read_without_anything_being_changed(
    kernel: Kernel, stocked: dict[str, Sample]
) -> None:
    assert kernel.get(Sample, stocked["Z9"].id, actor=AUDITOR) == stocked["Z9"]
    assert len(kernel.audit(actor=AUDITOR)) == len(SAMPLES)
    assert kernel.events(actor=AUDITOR) == []
    with pytest.raises(Unauthorised):
        kernel.execute(Receive(code="S-999", name="Extra"), actor=AUDITOR)


def test_a_whole_module_grants_its_commands_and_its_reads(kernel: Kernel) -> None:
    sample = kernel.execute(Receive(code="S-1", name="One"), actor=TECHNICIAN).result

    assert kernel.get(Sample, sample.id, actor=TECHNICIAN) == sample
    assert kernel.ask(Tally(), actor=TECHNICIAN) == -1


def test_one_read_is_granted_without_the_rest(kernel: Kernel, stocked: dict[str, Sample]) -> None:
    assert kernel.ask(Tally(), actor=COUNTER) == len(SAMPLES), "it reads samples to answer"
    with pytest.raises(Unauthorised):
        kernel.page(Sample, actor=COUNTER)


def test_what_cannot_be_read_cannot_be_probed_for(kernel: Kernel) -> None:
    with pytest.raises(Unauthorised):
        kernel.get(Sample, uuid7(), actor=COURIER)
    with pytest.raises(NotFound, match=r"there is no lab\.sample with id"):
        kernel.get(Sample, uuid7(), actor=OBSERVER)


def test_the_kernels_own_permissions_cannot_be_declared_by_a_module() -> None:
    with pytest.raises(RegistrationError, match="a module cannot be named 'kernel'"):
        Module("kernel")


def test_a_document_and_a_query_cannot_share_the_permission_to_read_them() -> None:
    clash = Module("clash")

    @clash.document
    class Report(Document):
        """A report someone wrote."""

        lifecycle = Lifecycle(initial="written")

    class Report_(Query[int]):
        pass

    Report_.__name__ = "Report"

    def report(query: Report_, reader: Reader) -> int:
        """How many reports there are."""
        return 0

    with pytest.raises(RegistrationError, match=r"clash\.report is declared twice"):
        clash.query(report)


# Listings.


def test_a_listing_is_filtered_by_the_values_fields_equal(
    kernel: Kernel, stocked: dict[str, Sample]
) -> None:
    assert codes(kernel, where={"grade": "a"}) == ["S-001", "S-004"]
    assert codes(kernel, where={"grade": Grade.B, "urgent": True}) == ["S-002"]
    assert codes(kernel, where={"grade": None, "count": 12}) == ["Z9"]
    assert codes(kernel, where={"id": stocked["ACME"].id}) == ["ACME"]
    assert codes(kernel, where={"logged_at": "2026-01-05T09:00:00"}) == ["S-001"]


def test_numbers_and_instants_are_equal_by_value_however_they_were_written(
    kernel: Kernel, stocked: dict[str, Sample]
) -> None:
    assert codes(kernel, where={"weight": "10.5"}) == ["S-001", "S-004"]
    assert codes(kernel, where={"weight": {"ne": Decimal("10.500")}}) == [
        "S-002",
        "S-003",
        "S-010",
        "S-10",
        "ACME",
        "Z9",
    ]
    assert codes(kernel, where={"taken_at": "2026-01-05T08:00:00+00:00"}) == ["S-001", "S-002"]


def test_a_listing_is_filtered_by_ranges_and_choices(
    kernel: Kernel, stocked: dict[str, Sample]
) -> None:
    january = {"gte": "2026-01-01", "lt": date(2026, 2, 1)}
    assert codes(kernel, where={"received": january}) == ["S-001", "S-002", "S-10", "ACME", "Z9"]
    assert codes(kernel, where={"weight": {"gt": 9, "lte": "10.5"}}) == ["S-001", "S-004"]
    assert codes(kernel, where={"weight": {"lt": 0}}) == ["S-10"]
    assert codes(kernel, where={"count": {"gte": 7}, "code": {"gt": "S-004"}}) == ["Z9"]
    assert codes(kernel, where={"taken_at": {"gt": "2026-02-02T00:00:00Z"}}) == ["S-010"]
    assert codes(kernel, where={"grade": {"in": ["b", None]}, "count": {"in": [0, 1]}}) == [
        "S-003",
        "S-010",
        "S-10",
        "ACME",
    ]
    assert codes(kernel, where={"grade": {"in": []}}) == []
    assert codes(kernel, where={"grade": {"ne": None}, "state": {"ne": "received"}}) == []


def test_a_listing_is_ordered_by_a_field_then_by_age(
    kernel: Kernel, stocked: dict[str, Sample]
) -> None:
    assert codes(kernel) == [sample["code"] for sample in SAMPLES], "oldest first"
    assert codes(kernel, order="-id", limit=3) == [s["code"] for s in reversed(SAMPLES)]
    assert codes(kernel, order="code") == sorted(stocked)
    assert codes(kernel, order="name", limit=2) == [
        "S-003",
        "S-004",
        "S-001",
        "S-10",
        "Z9",
        "S-010",
        "ACME",
        "S-002",
    ], "text orders by code point"
    assert codes(kernel, order="received", limit=3) == [
        "S-010",
        "S-001",
        "S-10",
        "ACME",
        "Z9",
        "S-002",
        "S-003",
        "S-004",
    ]


def test_documents_without_a_value_come_after_those_with_one(
    kernel: Kernel, stocked: dict[str, Sample]
) -> None:
    weighed = ["S-10", "S-002", "S-001", "S-004", "S-010"]
    unweighed = ["S-003", "ACME", "Z9"]

    for limit in (1, 2, 3, 50):
        assert codes(kernel, order="weight", limit=limit) == weighed + unweighed
        assert codes(kernel, order="-weight", limit=limit) == [
            *reversed(unweighed),
            "S-010",
            "S-004",
            "S-001",
            "S-002",
            "S-10",
        ]
    assert codes(kernel, order="taken_at", limit=1)[:4] == ["S-001", "S-002", "S-004", "S-010"]


def test_a_page_says_whether_another_follows(kernel: Kernel, stocked: dict[str, Sample]) -> None:
    first = kernel.page(Sample, actor=ROOT, order="code", limit=5)
    assert [s.code for s in first.items] == ["ACME", "S-001", "S-002", "S-003", "S-004"]
    assert first.next is not None

    last = kernel.page(Sample, actor=ROOT, order="code", limit=5, after=first.next)
    assert last == Page((stocked["S-010"], stocked["S-10"], stocked["Z9"]), None)

    exactly = kernel.page(Sample, actor=ROOT, limit=len(SAMPLES))
    assert exactly.next is None, "a full page with nothing after it is the last"


def test_documents_added_between_pages_are_neither_skipped_nor_repeated(kernel: Kernel) -> None:
    for number in (2, 4, 6):
        kernel.execute(Receive(code=f"S-{number}", name="Sample"), actor=TECHNICIAN)
    first = kernel.page(Sample, actor=ROOT, order="code", limit=2)

    kernel.execute(Receive(code="S-1", name="Before the page"), actor=TECHNICIAN)
    kernel.execute(Receive(code="S-5", name="After the page"), actor=TECHNICIAN)

    rest = kernel.page(Sample, actor=ROOT, order="code", limit=2, after=first.next)
    assert [s.code for s in (*first.items, *rest.items)] == ["S-2", "S-4", "S-5", "S-6"]


def test_a_cursor_continues_only_the_listing_that_gave_it(
    kernel: Kernel, stocked: dict[str, Sample]
) -> None:
    cursor = kernel.page(Sample, actor=ROOT, order="code", limit=2).next
    assert cursor is not None

    def forged(*position: object) -> str:
        return base64.urlsafe_b64encode(json.dumps(position).encode()).decode()

    digest = json.loads(base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)))[0]
    others: list[dict[str, Any]] = [
        {"order": "name"},
        {"order": "-code"},
        {"order": "code", "where": {"grade": "a"}},
        {"order": "code", "search": "cotton"},
    ]
    for listing in others:
        with pytest.raises(Rejected, match="is not a cursor from this listing") as refusal:
            kernel.page(Sample, actor=ROOT, after=cursor, **listing)
        assert [problem.path for problem in refusal.value.problems] == [("after",)]
    for nonsense in (
        "nonsense",
        "",
        forged(digest, "S-001"),
        forged(digest, 7, str(uuid7())),
        forged(digest, True, str(uuid7())),
        forged(digest, "S-001", "not an id"),
        forged(digest, "S-001", 7),
    ):
        with pytest.raises(Rejected, match="is not a cursor from this listing"):
            kernel.page(Sample, actor=ROOT, order="code", after=nonsense)

    weights = kernel.page(Sample, actor=ROOT, order="weight", limit=1).next
    assert weights is not None
    digest = json.loads(base64.urlsafe_b64decode(weights + "=" * (-len(weights) % 4)))[0]
    for weight in ("heavy", "NaN", "Infinity"):
        with pytest.raises(Rejected, match="is not a cursor from this listing"):
            kernel.page(
                Sample, actor=ROOT, order="weight", after=forged(digest, weight, str(uuid7()))
            )

    oldest_first = kernel.page(Sample, actor=ROOT, limit=1).next
    assert oldest_first is not None
    digest = json.loads(base64.urlsafe_b64decode(oldest_first + "=" * (-len(oldest_first) % 4)))[0]
    with pytest.raises(Rejected, match="is not a cursor from this listing"):
        kernel.page(Sample, actor=ROOT, after=forged(digest, "S-001", str(uuid7())))


def test_a_listing_that_cannot_mean_anything_is_refused_with_every_fault(
    kernel: Kernel,
) -> None:
    with pytest.raises(Rejected, match=r"cannot list lab\.sample: ") as refusal:
        kernel.page(
            Sample,
            actor=ROOT,
            where={
                "colour": "red",
                "notes": "Fragile",
                "received": {"gte": "last Tuesday"},
                "urgent": {"gt": False},
                "grade": {"in": "a"},
                "count": {"lt": None, "eq": "many"},
                "code": None,
                "weight": {"between": [1, 2]},
            },
            order="urgent",
            limit=500,
        )

    assert set(refusal.value.problems) == {
        Problem(("where", "colour"), "it has no such field"),
        Problem(("where", "notes"), "holds more than one value, so it cannot be filtered on"),
        Problem(
            ("where", "received", "gte"),
            "Input should be a valid date or datetime, invalid character in year",
        ),
        Problem(("where", "urgent", "gt"), "cannot be compared with 'gt'; use one of eq, ne, in"),
        Problem(("where", "grade", "in"), "`in` takes a list of values"),
        Problem(("where", "count", "lt"), "cannot be compared with null using 'lt'"),
        Problem(
            ("where", "count", "eq"),
            "Input should be a valid integer, unable to parse string as an integer",
        ),
        Problem(("where", "code", "eq"), "is never null"),
        Problem(
            ("where", "weight", "between"),
            "cannot be compared with 'between'; use one of eq, ne, in, gt, gte, lt, lte",
        ),
        Problem(
            ("order",),
            "cannot order by 'urgent'; it can be ordered by id, state, version, code, name, "
            "grade, received, weight, count, taken_at",
        ),
        Problem(("limit",), "must be between 1 and 200"),
    }
    with pytest.raises(Rejected, match="limit: must be between 1 and 200"):
        kernel.page(Sample, actor=ROOT, limit=0)


# Search.


@pytest.mark.parametrize(
    ("search", "found"),
    [
        ("muller", ["S-001", "S-002"]),
        ("MÜLLER feed", ["S-001"]),
        ("feed müller", ["S-001"]),
        ("strasse", ["Z9"]),
        ("STRAẞE", ["Z9"]),
        ("cold fragile", ["S-001"]),
        ("cotton", ["S-003", "S-004"]),
        ("100%", ["S-003"]),
        ("cotton_", ["S-004"]),
        (r"back\slash", ["ACME"]),
        ("s-0", ["S-001", "S-002", "S-003", "S-004", "S-010", "S-10"]),
        ("z", ["S-010", "Z9"]),
        ("muller cotton", []),
        ("oats muller extra", []),
        ("   ", [sample["code"] for sample in SAMPLES]),
    ],
)
def test_search_finds_documents_holding_every_word_whatever_its_case_or_accents(
    kernel: Kernel, stocked: dict[str, Sample], search: str, found: list[str]
) -> None:
    assert codes(kernel, search=search, order="code") == sorted(found)
    assert sorted(codes(kernel, search=search, limit=2)) == sorted(found)


def test_search_puts_the_best_match_first_unless_an_order_is_asked_for(
    kernel: Kernel, stocked: dict[str, Sample]
) -> None:
    for limit in (1, 2, 50):
        assert codes(kernel, search="S-001", limit=limit) == ["S-001", "S-10", "S-010"], (
            "the code itself, then a name that starts with it, then a mention"
        )
        assert codes(kernel, search="acme", limit=limit) == ["ACME"]
        assert codes(kernel, search="s-001", order="-code", limit=limit) == [
            "S-10",
            "S-010",
            "S-001",
        ]


def test_search_works_with_filters(kernel: Kernel, stocked: dict[str, Sample]) -> None:
    assert codes(kernel, search="s-00", where={"grade": "a"}, order="-received") == [
        "S-004",
        "S-001",
    ]


def test_what_is_searched_follows_the_document(kernel: Kernel, stocked: dict[str, Sample]) -> None:
    kernel.execute(Discard(sample_id=stocked["S-003"].id), actor=TECHNICIAN)

    assert codes(kernel, search="cotton", where={"state": "received"}) == ["S-004"]
    assert codes(kernel, search="cotton", where={"state": "discarded"}) == ["S-003"]


def test_searching_what_declares_nothing_searchable_is_refused(kernel: Kernel) -> None:
    with pytest.raises(Rejected, match=r"cannot list lab\.reading: search: cannot be searched"):
        kernel.page(Reading, actor=ROOT, search="anything")
    with pytest.raises(Rejected, match="search: is longer than 200 characters"):
        kernel.page(Sample, actor=ROOT, search="x" * 201)


def test_searchable_fields_must_exist_and_hold_text() -> None:
    def declare(*searchable: str) -> Module:
        module = Module("museum")

        class Exhibit(Document):
            """Something on show."""

            lifecycle = Lifecycle(initial="shown")

            title: str
            labels: list[Name]
            year: int
            rooms: tuple[int, ...]
            anything: list[Any]

        Exhibit.searchable = searchable
        module.document(Exhibit)
        return module

    assert Kernel([declare("title", "labels")])
    with pytest.raises(RegistrationError, match="unknown field 'tilte' searchable"):
        Kernel([declare("tilte")])
    for field in ("year", "rooms", "anything"):
        with pytest.raises(RegistrationError, match=rf"exhibit\.{field} is declared searchable"):
            Kernel([declare(field)])


def test_folding_removes_case_accents_and_width() -> None:
    assert fold("  \uff2düller\tSTRAẞE  Ångström ") == "muller strasse angstrom"


# Reads inside a unit of work.


def test_a_handler_reads_what_its_unit_of_work_has_written(
    kernel: Kernel, stocked: dict[str, Sample]
) -> None:
    command = ReceiveAndReport(
        code="A-1", name="New arrival", weight=Decimal(1), discard=stocked["S-010"].id
    )

    listed, heaviest, held = kernel.execute(command, actor=TECHNICIAN).result

    assert listed == ("A-1", "ACME", "S-001"), "the sample it received is on the page"
    assert heaviest == "S-004", "the one it discarded is no longer the heaviest held"
    assert held == len(SAMPLES), "one received and one discarded"


def test_a_dry_run_reads_its_own_writes_and_leaves_none(
    kernel: Kernel, stocked: dict[str, Sample]
) -> None:
    command = ReceiveAndReport(
        code="A-1", name="New arrival", weight=Decimal(1), discard=stocked["S-001"].id
    )

    listed, _, _ = kernel.execute(command, actor=TECHNICIAN, dry_run=True).result

    assert listed == ("A-1", "ACME", "S-002")
    assert codes(kernel, order="code")[:3] == ["ACME", "S-001", "S-002"]


# Registered queries.


def test_a_query_is_answered_for_the_actor_who_asks(
    kernel: Kernel, stocked: dict[str, Sample]
) -> None:
    assert kernel.ask(Heaviest(grade=Grade.A), actor=OBSERVER) == stocked["S-004"], (
        "of two that weigh the same, the newer"
    )
    assert kernel.ask(WhoIsAsking(), actor=OBSERVER) == ("olu", "Reader")


def test_a_query_is_built_by_name_from_json(kernel: Kernel, stocked: dict[str, Sample]) -> None:
    query = kernel.query("lab.heaviest", {"grade": "b"})

    assert query == Heaviest(grade=Grade.B)
    assert kernel.ask(query, actor=OBSERVER) == stocked["S-010"]
    with pytest.raises(NotFound, match=r"there is no query 'lab\.lightest'"):
        kernel.query("lab.lightest", {})
    with pytest.raises(Rejected, match=r"lab\.heaviest: grade: Input should be 'a' or 'b'") as no:
        kernel.query("lab.heaviest", {"grade": "c"})
    assert [problem.path for problem in no.value.problems] == [("grade",)]


def test_a_query_asked_by_a_command_can_read_and_cannot_write() -> None:
    probe = Module("probe")
    given: list[Reader] = []

    @probe.document
    class Mark(Document):
        """A mark left behind."""

        lifecycle = Lifecycle(initial="left")

    class Look(Query[int]):
        pass

    class LeaveMark(Command[int]):
        pass

    @probe.query
    def look(query: Look, reader: Reader) -> int:
        """How many marks have been left."""
        given.append(reader)
        return len(reader.find(Mark))

    @probe.command
    def leave_mark(command: LeaveMark, context: Context) -> int:
        """Leave a mark, then count the marks."""
        context.create(Mark())
        return context.ask(Look())

    kernel = Kernel([probe])
    actor = Actor(id="p", kind=ActorKind.HUMAN, permissions=frozenset({"probe.*"}))

    assert kernel.execute(LeaveMark(), actor=actor).result == 1, "it sees the command's write"
    assert kernel.ask(Look(), actor=actor) == 1
    for reader in given:
        assert type(reader) is Reader
        assert not hasattr(reader, "create")
        assert not hasattr(reader, "emit")


def test_a_module_asks_only_its_own_queries_and_those_of_modules_it_requires() -> None:
    nosy = Module("nosy")
    polite = Module("polite", requires=(lab,))

    class Peek(Command[int]):
        pass

    class Enquire(Command[int]):
        pass

    @nosy.command
    def peek(command: Peek, context: Context) -> int:
        """Ask the laboratory's query without requiring the laboratory."""
        return context.ask(Tally())

    @polite.command
    def enquire(command: Enquire, context: Context) -> int:
        """Ask the laboratory's query, having required the laboratory."""
        return context.ask(Tally())

    kernel = Kernel([lab, nosy, polite])
    anyone = Actor(id="a", kind=ActorKind.HUMAN, permissions=frozenset({"nosy.*", "polite.*"}))

    assert kernel.execute(Enquire(), actor=anyone).result == 0
    with pytest.raises(RemitError, match=r"nosy cannot ask lab\.tally: it does not require lab"):
        kernel.execute(Peek(), actor=anyone)


def test_a_query_must_say_what_it_is_and_what_it_returns() -> None:
    module = Module("vague")

    class Guess(Query[int]):
        pass

    def undocumented(query: Guess, reader: Reader) -> int:
        return 0

    def untyped(query: Guess, reader: Reader):
        """Guess."""

    def misdirected(query: Receive, reader: Reader) -> int:
        """Take a command where a query belongs."""
        return 0

    with pytest.raises(RegistrationError, match="needs a docstring"):
        module.query(undocumented)
    with pytest.raises(RegistrationError, match="must declare what it returns"):
        module.query(untyped)
    with pytest.raises(RegistrationError, match="must be annotated with a Query"):
        module.query(misdirected)  # pyright: ignore[reportArgumentType]


def test_one_query_class_answers_for_one_module() -> None:
    def declared(name: str) -> Module:
        module = Module(name)

        def count(query: Tally, reader: Reader) -> int:
            """Count something."""
            return 0

        module.query(count)
        return module

    with pytest.raises(RegistrationError, match=r"second\.tally is the class already registered"):
        Kernel([declared("first"), declared("second")])


def test_the_registry_describes_how_everything_is_read(kernel: Kernel) -> None:
    described = kernel.registry.describe()["lab"]

    sample = described["documents"]["lab.sample"]
    assert sample["permission"] == "lab.read.sample"
    assert sample["listing"]["searchable"] == ["code", "name", "notes"]
    assert sample["listing"]["limit"] == {"default": 50, "maximum": 200}
    assert sample["listing"]["filters"]["weight"] == ["eq", "ne", "in", "gt", "gte", "lt", "lte"]
    assert sample["listing"]["filters"]["urgent"] == ["eq", "ne", "in"]
    assert sample["listing"]["filters"]["logged_at"] == ["eq", "ne", "in"]
    assert "notes" not in sample["listing"]["filters"]
    assert "urgent" not in sample["listing"]["orders"]
    assert described["documents"]["lab.reading"]["listing"]["searchable"] == []

    heaviest = described["queries"]["lab.heaviest"]
    assert heaviest["summary"] == "The heaviest sample still held that has been weighed."
    assert heaviest["permission"] == "lab.read.heaviest"
    assert heaviest["input"]["properties"]["grade"]["description"] == (
        "Consider only samples of this grade."
    )
    assert heaviest["returns"]["anyOf"][1] == {"type": "null"}
    assert kernel.registry.find_document("lab.sample") is Sample
    assert kernel.registry.find_document("lab.specimen") is None


# Both stores must mean the same thing by a listing.


def listings(values: dict[str, list[Any]]) -> st.SearchStrategy[dict[str, Any]]:
    found = columns(Sample)

    def condition(field: str) -> st.SearchStrategy[Any]:
        value = st.sampled_from(values[field])
        present = st.sampled_from([v for v in values[field] if v is not None])

        def operand(name: str) -> st.SearchStrategy[Any]:
            if name == "in":
                return st.lists(value, max_size=3)
            return value if name in ("eq", "ne") else present

        bare = value.filter(lambda v: v is not None or found[field].nullable)
        tests = st.dictionaries(st.sampled_from(found[field].operators), st.none(), max_size=2)
        return bare | tests.flatmap(
            lambda chosen: st.fixed_dictionaries({name: operand(name) for name in chosen})
        )

    orders = [name for name, column in found.items() if column.comparison]
    searches = ["muller", "S-001", "cotton", "100%", "_", "o", "é", "keep cold", "zzz", "\\", "s"]
    return st.fixed_dictionaries(
        {"limit": st.integers(1, 5)},
        optional={
            "where": st.dictionaries(st.sampled_from(sorted(found)), st.none(), max_size=3).flatmap(
                lambda chosen: st.fixed_dictionaries({field: condition(field) for field in chosen})
            ),
            "order": st.sampled_from([*orders, *(f"-{name}" for name in orders)]),
            "search": st.sampled_from(searches),
        },
    )


def test_both_stores_give_the_same_pages_for_any_listing(postgres: PostgresStore) -> None:
    memory, database = kernel_on(MemoryStore(), [lab]), kernel_on(postgres, [lab])
    stocked = stock(memory)
    stock(database)
    for kernel in (memory, database):
        kernel.execute(Discard(sample_id=codes_to_ids(kernel)["S-003"]), actor=TECHNICIAN)
    values = {
        field: [getattr(sample, field) for sample in stocked.values()] + ["discarded"]
        for field in columns(Sample)
        if field != "id"
    }
    values["id"] = [uuid7()]
    values["state"] = ["received", "discarded"]

    @settings(max_examples=400, deadline=None)
    @given(listings(values))
    def agree(listing: dict[str, Any]) -> None:
        try:
            expected = codes(memory, **listing)
        except Rejected as refusal:
            with pytest.raises(Rejected, match=str(refusal).split(":")[0]):
                codes(database, **listing)
        else:
            assert codes(database, **listing) == expected

    agree()


def codes_to_ids(kernel: Kernel) -> dict[str, UUID]:
    return {sample.code: sample.id for sample in kernel.page(Sample, actor=ROOT).items}


# What only PostgreSQL keeps: the text that search reads, beside the documents.


def searched_text(kind: str) -> list[str]:
    with psycopg.connect(DSN) as connection:
        found = connection.execute(
            "SELECT text FROM document_search WHERE kind = %s ORDER BY text", (kind,)
        ).fetchall()
    return [text for (text,) in found]


def test_installing_rebuilds_what_is_searched_when_a_module_declares_it_differently(
    postgres: PostgresStore,
) -> None:
    def declare(*searchable: str) -> tuple[Module, type[Document], type[Command[Any]]]:
        module = Module("archive")

        class Folder(Document):
            """A folder of papers."""

            lifecycle = Lifecycle(initial="filed")

            title: str
            shelf: str

        class File(Command[Folder]):
            title: str
            shelf: str

        def file(command: File, context: Context) -> Folder:
            """File a folder."""
            return context.create(Folder(title=command.title, shelf=command.shelf))

        Folder.searchable = searchable
        module.document(Folder)
        module.command(file)
        return module, Folder, File

    archivist = Actor(id="a", kind=ActorKind.HUMAN, permissions=frozenset({"archive.*"}))
    module, folder, file = declare("title")
    kernel = kernel_on(postgres, [module])
    filing = file.model_validate({"title": "Deeds", "shelf": "North wall"})
    kernel.execute(filing, actor=archivist)
    assert searched_text("archive.folder") == ["\ndeeds\n"]

    by_shelf, folder, file = declare("shelf")
    with pytest.raises(RegistrationError, match="installed from a different definition"):
        Kernel([by_shelf], store=postgres)
    kernel = kernel_on(postgres, [by_shelf])

    assert searched_text("archive.folder") == ["\nnorth wall\n"]
    [found] = kernel.page(folder, actor=ROOT, search="north").items
    assert found.model_dump()["title"] == "Deeds"
    assert kernel.page(folder, actor=ROOT, search="deeds").items == ()

    unsearched, folder, file = declare()
    kernel_on(postgres, [unsearched])
    assert searched_text("archive.folder") == []
