"""Personal data: sealed in the audit trail and idempotency records, erasable by subject.

Exercised through a toy module; see docs/personal-data.md for the design.
"""

from typing import Annotated, Optional
from uuid import UUID

import pytest
from conftest import kernel_on
from pydantic import BaseModel

from remit.kernel import (
    ERASED,
    Actor,
    ActorKind,
    Command,
    Context,
    Document,
    Event,
    Kernel,
    Lifecycle,
    Module,
    Personal,
    RegistrationError,
    Rejected,
    RemitError,
    Store,
    Transition,
)

people = Module("people")


class Guardian(BaseModel):
    """Someone responsible for a junior member."""

    name: Annotated[str, Personal]
    relation: str


@people.document
class Member(Document):
    """A member of a club."""

    lifecycle = Lifecycle(
        initial="current", transitions=(Transition("erase", "current", "erased"),)
    )

    name: Annotated[str, Personal]
    email: Annotated[str | None, Personal] = None
    grade: str = "junior"
    guardians: tuple[Guardian, ...] = ()
    emergency: Guardian | None = None


@people.document
class Notice(Document):
    """A notice pinned to the board. Holds no personal data."""

    lifecycle = Lifecycle(initial="pinned")

    text: str


class Join(Command[Member]):
    name: Annotated[str, Personal]
    email: Annotated[str | None, Personal] = None
    guardians: tuple[Guardian, ...] = ()
    emergency: Guardian | None = None


class JoinWithFamilies(Command[Member]):
    name: Annotated[str, Personal]
    families: tuple[tuple[Guardian, ...], ...]


class ForgetNameOnly(Command[Member]):
    member_id: UUID


class Directory(Command[dict[str, Member]]):
    name: Annotated[str, Personal]


class Households(Command[dict[str, tuple[Member, ...]]]):
    name: Annotated[str, Personal]


class Count(Command[dict[str, int]]):
    name: Annotated[str, Personal]


class JoinTogether(Command[tuple[Member, Member]]):
    first: Annotated[str, Personal]
    second: Annotated[str, Personal]


class Forget(Command[Member]):
    member_id: UUID


class ForgetWithoutBlanking(Command[None]):
    member_id: UUID


class PinWithName(Command[Notice]):
    """Carries a personal value but writes no data subject to link it to."""

    name: Annotated[str, Personal]


class Pin(Command[Notice]):
    text: str


class EraseNotice(Command[None]):
    notice_id: UUID


@people.command
def join(command: Join, context: Context) -> Member:
    """Join the club."""
    return context.create(Member.model_validate(command.model_dump()))


@people.command
def join_with_families(command: JoinWithFamilies, context: Context) -> Member:
    """Join, listing each family the member belongs to."""
    return context.create(Member(name=command.name))


@people.command
def forget_name_only(command: ForgetNameOnly, context: Context) -> Member:
    """Erase a member's own details but not their guardians'."""
    member = context.get(Member, command.member_id)
    erased = context.transition(member, "erase", name=ERASED, email=None, emergency=None)
    context.erase(erased)
    return erased


@people.command
def households(command: Households, context: Context) -> dict[str, tuple[Member, ...]]:
    """Join, and return the member under their household."""
    return {"Shaw": (context.create(Member(name=command.name)),)}


@people.command
def count(command: Count, context: Context) -> dict[str, int]:
    """Join, and return how many members have joined under each grade."""
    context.create(Member(name=command.name))
    return {"junior": len(context.find(Member, grade="junior"))}


@people.command
def directory(command: Directory, context: Context) -> dict[str, Member]:
    """Join, and return the member keyed by name."""
    return {command.name: context.create(Member(name=command.name))}


@people.command
def join_together(command: JoinTogether, context: Context) -> tuple[Member, Member]:
    """Two people join at once."""
    return (
        context.create(Member(name=command.first)),
        context.create(Member(name=command.second)),
    )


@people.command
def forget(command: Forget, context: Context) -> Member:
    """Erase a member."""
    member = context.get(Member, command.member_id)
    erased = context.transition(
        member, "erase", name=ERASED, email=None, guardians=(), emergency=None
    )
    context.erase(erased)
    return erased


@people.command
def forget_without_blanking(command: ForgetWithoutBlanking, context: Context) -> None:
    """Try to erase a member while keeping their details on the live document."""
    context.erase(context.get(Member, command.member_id))


@people.command
def pin_with_name(command: PinWithName, context: Context) -> Notice:
    """Pin a notice, mentioning someone by name."""
    return context.create(Notice(text=f"Welcome, {command.name}"))


@people.command
def pin(command: Pin, context: Context) -> Notice:
    """Pin a notice."""
    return context.create(Notice(text=command.text))


@people.command
def erase_notice(command: EraseNotice, context: Context) -> None:
    """Try to erase a document that holds no personal data."""
    context.erase(context.get(Notice, command.notice_id))


SECRETARY = Actor(id="secretary", kind=ActorKind.HUMAN, permissions=frozenset({"people.*"}))


@pytest.fixture
def club(store: Store) -> Kernel:
    return kernel_on(store, [people])


def inputs(kernel: Kernel) -> list[dict[str, object]]:
    return [entry.input for entry in kernel.audit()]


def contact_details(kernel: Kernel) -> list[dict[str, object]]:
    """The name and email in each entry's input; the member id for an erasure."""
    keep = ("name", "email", "member_id")
    return [{k: v for k, v in entry.items() if k in keep} for entry in inputs(kernel)]


def test_personal_values_read_back_until_their_subject_is_erased(club: Kernel) -> None:
    amy = club.execute(Join(name="Amy", email="amy@example.com"), actor=SECRETARY).result
    club.execute(Join(name="Bea"), actor=SECRETARY)
    assert contact_details(club) == [
        {"name": "Amy", "email": "amy@example.com"},
        {"name": "Bea", "email": None},
    ]

    erased = club.execute(Forget(member_id=amy.id), actor=SECRETARY).result

    assert (erased.name, erased.email, erased.grade) == (ERASED, None, "junior")
    assert contact_details(club) == [
        {"name": ERASED, "email": ERASED},
        {"name": "Bea", "email": None},
        {"member_id": str(amy.id)},
    ]


def test_an_entry_about_two_people_is_erased_with_either(club: Kernel) -> None:
    amy, bea = club.execute(JoinTogether(first="Amy", second="Bea"), actor=SECRETARY).result
    club.execute(Forget(member_id=amy.id), actor=SECRETARY)
    assert inputs(club)[0] == {"first": ERASED, "second": ERASED}
    assert club.get(Member, bea.id).name == "Bea"


def test_a_replay_is_answered_from_its_sealed_record(club: Kernel) -> None:
    first = club.execute(Join(name="Amy"), actor=SECRETARY, idempotency_key="join-amy")
    again = club.execute(Join(name="Amy"), actor=SECRETARY, idempotency_key="join-amy")
    assert again.replayed
    assert again.result == first.result
    with pytest.raises(Rejected, match="already used for another command"):
        club.execute(Join(name="Ann"), actor=SECRETARY, idempotency_key="join-amy")


def test_a_replay_of_an_erased_subject_is_refused(club: Kernel) -> None:
    amy = club.execute(Join(name="Amy"), actor=SECRETARY, idempotency_key="join-amy").result
    club.execute(Forget(member_id=amy.id), actor=SECRETARY)
    with pytest.raises(Rejected, match="was erased with the personal data it held"):
        club.execute(Join(name="Amy"), actor=SECRETARY, idempotency_key="join-amy")


def test_personal_data_with_no_subject_to_erase_it_by_is_refused(club: Kernel) -> None:
    with pytest.raises(RemitError, match="writes no document it belongs to"):
        club.execute(PinWithName(name="Amy"), actor=SECRETARY)
    assert club.find(Notice) == []
    assert club.audit() == []


def test_a_dry_run_keeps_nothing(club: Kernel) -> None:
    club.execute(Join(name="Amy"), actor=SECRETARY, dry_run=True)
    assert club.audit() == []


def test_erasing_needs_the_live_document_blanked_first(club: Kernel) -> None:
    amy = club.execute(Join(name="Amy", email="amy@example.com"), actor=SECRETARY).result
    with pytest.raises(RemitError, match="personal fields before erasing it: name, email"):
        club.execute(ForgetWithoutBlanking(member_id=amy.id), actor=SECRETARY)
    assert contact_details(club)[0] == {"name": "Amy", "email": "amy@example.com"}


def test_only_documents_with_personal_data_can_be_erased(club: Kernel) -> None:
    notice = club.execute(Pin(text="AGM on Friday"), actor=SECRETARY).result
    assert club.audit()[0].key is None
    with pytest.raises(RemitError, match="holds no personal data to erase"):
        club.execute(EraseNotice(notice_id=notice.id), actor=SECRETARY)


def test_events_may_not_carry_personal_data() -> None:
    gossip = Module("gossip")

    @gossip.event
    class Overheard(Event):
        """Something someone said."""

        speaker: Annotated[str, Personal]

    with pytest.raises(RegistrationError, match="carries personal data; events"):
        Kernel([gossip])


def test_personal_data_where_it_could_not_be_sealed_is_refused() -> None:
    ledger = Module("roll")

    @ledger.document
    class Roll(Document):
        """Members by number."""

        lifecycle = Lifecycle(initial="kept")

        members: dict[str, Member]

    with pytest.raises(RegistrationError, match=r"Roll\.members holds personal data inside a dict"):
        Kernel([ledger])


def test_personal_values_nested_in_models_and_tuples_are_sealed_and_erased(club: Kernel) -> None:
    mum, gran = Guardian(name="Ruth", relation="mother"), Guardian(name="Iris", relation="gran")
    join = Join(name="Tom", guardians=(mum, gran), emergency=gran)
    tom = club.execute(join, actor=SECRETARY, idempotency_key="join-tom").result
    assert inputs(club)[0]["guardians"] == [
        {"name": "Ruth", "relation": "mother"},
        {"name": "Iris", "relation": "gran"},
    ]
    assert club.execute(join, actor=SECRETARY, idempotency_key="join-tom").result == tom

    club.execute(Forget(member_id=tom.id), actor=SECRETARY)

    remembered = inputs(club)[0]
    assert remembered["guardians"] == [
        {"name": ERASED, "relation": "mother"},
        {"name": ERASED, "relation": "gran"},
    ]
    assert remembered["emergency"] == {"name": ERASED, "relation": "gran"}


def test_a_tuple_of_results_is_sealed_for_replay(club: Kernel) -> None:
    together = JoinTogether(first="Amy", second="Bea")
    first = club.execute(together, actor=SECRETARY, idempotency_key="pair").result
    assert club.execute(together, actor=SECRETARY, idempotency_key="pair").result == first


@pytest.mark.parametrize("command", [Directory(name="Amy"), Households(name="Amy")])
def test_a_result_that_would_hide_personal_data_in_a_mapping_is_refused(
    club: Kernel, command: Command[object]
) -> None:
    with pytest.raises(TypeError, match="inside a mapping cannot be sealed"):
        club.execute(command, actor=SECRETARY, idempotency_key="mapping")
    assert club.audit() == []


def test_a_mapping_without_personal_data_is_kept_for_replay(club: Kernel) -> None:
    first = club.execute(Count(name="Amy"), actor=SECRETARY, idempotency_key="count")
    assert first.result == {"junior": 1}
    again = club.execute(Count(name="Amy"), actor=SECRETARY, idempotency_key="count")
    assert (again.replayed, again.result) == (True, {"junior": 1})


def test_unsealable_personal_data_is_found_however_deeply_nested() -> None:
    nested = Module("nested")

    class Register(BaseModel):
        by_name: dict[str, Guardian]

    @nested.document
    class Club(Document):
        """A club and its register."""

        lifecycle = Lifecycle(initial="open")

        roll: Register

    with pytest.raises(RegistrationError, match=r"Register\.by_name holds personal data inside"):
        Kernel([nested])
    assert repr(Personal) == "Personal"


def test_personal_values_in_nested_sequences_are_sealed(club: Kernel) -> None:
    families = (
        (Guardian(name="Ruth", relation="mother"),),
        (Guardian(name="Iris", relation="gran"),),
    )
    tom = club.execute(JoinWithFamilies(name="Tom", families=families), actor=SECRETARY).result
    club.execute(Forget(member_id=tom.id), actor=SECRETARY)
    assert inputs(club)[0]["families"] == [
        [{"name": ERASED, "relation": "mother"}],
        [{"name": ERASED, "relation": "gran"}],
    ]


def test_erasing_checks_personal_values_nested_in_the_document(club: Kernel) -> None:
    tom = club.execute(
        Join(name="Tom", guardians=(Guardian(name="Ruth", relation="mother"),)), actor=SECRETARY
    ).result
    with pytest.raises(RemitError, match=r"before erasing it: guardians\[0\]\.name"):
        club.execute(ForgetNameOnly(member_id=tom.id), actor=SECRETARY)


def test_an_erasure_can_be_retried(club: Kernel) -> None:
    amy = club.execute(Join(name="Amy"), actor=SECRETARY).result
    first = club.execute(Forget(member_id=amy.id), actor=SECRETARY, idempotency_key="forget-amy")
    again = club.execute(Forget(member_id=amy.id), actor=SECRETARY, idempotency_key="forget-amy")
    assert again.replayed
    assert again.result == first.result


def test_a_dry_run_is_refused_for_the_same_reasons_as_a_real_one(club: Kernel) -> None:
    with pytest.raises(RemitError, match="writes no document it belongs to"):
        club.execute(PinWithName(name="Amy"), actor=SECRETARY, dry_run=True)


@pytest.mark.parametrize(
    "annotation",
    [Optional[Annotated[str, Personal]], tuple[Annotated[str, Personal], ...]],  # noqa: UP045
    ids=["inside a union", "inside a tuple"],
)
def test_a_mark_on_part_of_a_type_is_refused_not_ignored(annotation: object) -> None:
    partial = Module("partial")

    @partial.event
    class Noted(Event):
        """Something was noted."""

        value: annotation  # pyright: ignore[reportInvalidTypeForm]

    with pytest.raises(RegistrationError, match="marks part of its type as personal"):
        Kernel([partial])
