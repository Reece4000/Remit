"""Commands that declare the transition they take the document they act on through.

The registry describes the move, so that a screen offers a command only where it
applies, and the kernel holds each handler to it as it runs.
"""

from typing import Annotated
from uuid import UUID, uuid7

import pytest
from conftest import ROOT, kernel_on
from pydantic import ValidationError

from remit.kernel import (
    Actor,
    ActorKind,
    Command,
    Context,
    Document,
    Kernel,
    Lifecycle,
    Module,
    References,
    RegistrationError,
    Registry,
    Rejected,
    RemitError,
    Store,
    Transition,
)

rental = Module("rental")

BikeId = Annotated[UUID, References("rental.bike")]


@rental.document
class Bike(Document):
    """A bike for hire."""

    lifecycle = Lifecycle(
        initial="available",
        transitions=(
            Transition("hire", "available", "hired"),
            Transition("return", "hired", "available"),
            Transition("retire", "available", "retired"),
        ),
    )

    inspections: int = 0


@rental.document
class Hire(Document):
    """One hire of a bike, from when it goes out until it comes back."""

    lifecycle = Lifecycle(initial="out", transitions=(Transition("end", "out", "ended"),))

    bike_id: BikeId


class AddBike(Command[Bike]):
    pass


class HireBike(Command[Hire]):
    moves = "hire"
    bike_id: BikeId


class EndHire(Command[Hire]):
    moves = "end"
    hire_id: Annotated[UUID, References("rental.hire")]


class InspectBike(Command[Bike]):
    moves = "retire"
    bike_id: BikeId
    fit: bool


class HireUnannounced(Command[Hire]):
    bike_id: BikeId


class HireMislabelled(Command[Hire]):
    moves = "retire"
    bike_id: BikeId


class HireGroup(Command[tuple[Hire, ...]]):
    bike_ids: Annotated[tuple[UUID, ...], References("rental.bike")]


class HireOnBehalf(Command[Hire]):
    bike_id: BikeId
    announced: bool
    """Whether the command it executes declares the move."""


@rental.command
def add_bike(command: AddBike, context: Context) -> Bike:
    """Add a bike to the fleet."""
    return context.create(Bike())


@rental.command
def hire_bike(command: HireBike, context: Context) -> Hire:
    """Hire a bike out."""
    return _hire(context, command.bike_id)


@rental.command
def end_hire(command: EndHire, context: Context) -> Hire:
    """End a hire, which makes its bike available again."""
    hire = context.get(Hire, command.hire_id)
    context.transition(context.get(Bike, hire.bike_id), "return")
    return context.transition(hire, "end")


@rental.command
def inspect_bike(command: InspectBike, context: Context) -> Bike:
    """Inspect a bike, retiring it if it is no longer fit to hire."""
    bike = context.get(Bike, command.bike_id)
    inspected = context.update(bike, inspections=bike.inspections + 1)
    return inspected if command.fit else context.transition(inspected, "retire")


@rental.command
def hire_unannounced(command: HireUnannounced, context: Context) -> Hire:
    """Hire a bike out without declaring the move."""
    return _hire(context, command.bike_id)


@rental.command
def hire_mislabelled(command: HireMislabelled, context: Context) -> Hire:
    """Hire a bike out while declaring another move."""
    return _hire(context, command.bike_id)


@rental.command
def hire_group(command: HireGroup, context: Context) -> tuple[Hire, ...]:
    """Hire several bikes out at once, without declaring the move."""
    return tuple(_hire(context, bike_id) for bike_id in command.bike_ids)


@rental.command
def hire_on_behalf(command: HireOnBehalf, context: Context) -> Hire:
    """Hire a bike out through another command."""
    if command.announced:
        return context.execute(HireBike(bike_id=command.bike_id))
    return context.execute(HireUnannounced(bike_id=command.bike_id))


def _hire(context: Context, bike_id: UUID) -> Hire:
    hired = context.transition(context.get(Bike, bike_id), "hire")
    return context.create(Hire(bike_id=hired.id))


CLERK = Actor(id="clerk", kind=ActorKind.HUMAN, permissions=frozenset({"rental.*"}))


@pytest.fixture
def kernel(store: Store) -> Kernel:
    return kernel_on(store, [rental])


@pytest.fixture
def bike(kernel: Kernel) -> Bike:
    return kernel.execute(AddBike(), actor=CLERK).result


def test_the_registry_describes_the_document_a_command_moves_and_how() -> None:
    commands = Registry([rental]).describe()["rental"]["commands"]
    hiring = commands["rental.hire_bike"]

    assert hiring["moves"] == {"document": "rental.bike", "transition": "hire"}
    assert commands["rental.end_hire"]["moves"] == {"document": "rental.hire", "transition": "end"}
    assert commands["rental.add_bike"]["moves"] is None
    assert "moves" not in hiring["input"]["properties"]


def test_a_move_is_declared_by_the_command_not_sent_with_it() -> None:
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        HireBike.model_validate({"bike_id": uuid7(), "moves": "retire"})


def test_documents_the_input_does_not_refer_to_are_not_held_to_the_move(
    kernel: Kernel, bike: Bike
) -> None:
    hire = kernel.execute(HireBike(bike_id=bike.id), actor=CLERK).result
    ended = kernel.execute(EndHire(hire_id=hire.id), actor=CLERK).result

    assert ended.state == "ended"
    assert kernel.get(Bike, bike.id, actor=ROOT).state == "available"


def test_a_move_may_depend_on_what_the_handler_finds(kernel: Kernel, bike: Bike) -> None:
    kept = kernel.execute(InspectBike(bike_id=bike.id, fit=True), actor=CLERK).result
    retired = kernel.execute(InspectBike(bike_id=bike.id, fit=False), actor=CLERK).result

    assert (kept.state, retired.state) == ("available", "retired")


@pytest.mark.parametrize(
    ("command", "said"),
    [
        (
            HireUnannounced,
            r"rental\.hire_unannounced took rental\.bike, which its input refers to, through "
            r"'hire'; declare moves = 'hire' on HireUnannounced",
        ),
        (
            HireMislabelled,
            r"rental\.hire_mislabelled took rental\.bike, which its input refers to, through "
            r"'hire', but HireMislabelled declares moves = 'retire', a transition of rental\.bike",
        ),
    ],
)
def test_a_move_the_command_does_not_declare_is_an_error_in_the_module(
    kernel: Kernel,
    bike: Bike,
    command: type[HireUnannounced] | type[HireMislabelled],
    said: str,
) -> None:
    with pytest.raises(RemitError, match=said) as raised:
        kernel.execute(command(bike_id=bike.id), actor=CLERK)

    assert not isinstance(raised.value, Rejected)
    assert kernel.get(Bike, bike.id, actor=ROOT).state == "available"


def test_documents_referred_to_from_a_list_are_held_to_the_move_too(
    kernel: Kernel, bike: Bike
) -> None:
    with pytest.raises(RemitError, match=r"rental\.hire_group took rental\.bike"):
        kernel.execute(HireGroup(bike_ids=(bike.id,)), actor=CLERK)


def test_a_command_run_by_another_is_held_to_its_own_declaration(
    kernel: Kernel, bike: Bike
) -> None:
    # HireOnBehalf refers to the bike and declares no move; HireBike, which it runs,
    # moves the bike as HireBike declares, and that is not counted against HireOnBehalf.
    hired = kernel.execute(HireOnBehalf(bike_id=bike.id, announced=True), actor=CLERK).result
    assert hired.bike_id == bike.id

    other = kernel.execute(AddBike(), actor=CLERK).result
    with pytest.raises(RemitError, match=r"rental\.hire_unannounced took rental\.bike"):
        kernel.execute(HireOnBehalf(bike_id=other.id, announced=False), actor=CLERK)


def test_a_move_no_document_the_input_refers_to_can_take_is_refused() -> None:
    workshop = Module("workshop", requires=(rental,))

    class Repaint(Command[None]):
        moves = "repaint"
        bike_id: BikeId

    @workshop.command
    def repaint(command: Repaint, context: Context) -> None:
        """Repaint a bike."""

    with pytest.raises(
        RegistrationError,
        match=r"workshop\.repaint declares moves = 'repaint', but no document its input refers "
        r"to has that transition; it refers to rental\.bike",
    ):
        Registry([workshop])


def test_a_move_more_than_one_document_the_input_refers_to_could_take_is_refused() -> None:
    depot = Module("depot", requires=(rental,))

    @depot.document
    class Rack(Document):
        """Where bikes are kept."""

        lifecycle = Lifecycle(
            initial="in_use", transitions=(Transition("retire", "in_use", "retired"),)
        )

    class RetireWithRack(Command[None]):
        moves = "retire"
        bike_id: BikeId
        rack_id: Annotated[UUID, References("depot.rack")]

    @depot.command
    def retire_with_rack(command: RetireWithRack, context: Context) -> None:
        """Retire a bike together with the rack it hangs on."""

    with pytest.raises(
        RegistrationError,
        match=r"depot\.retire_with_rack declares moves = 'retire', but its input refers to "
        r"rental\.bike and depot\.rack, each with that transition, so which it moves is ambiguous",
    ):
        Registry([depot])
