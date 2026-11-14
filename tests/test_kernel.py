"""Kernel behaviour, exercised through a toy module that has nothing to do with accounting."""

from uuid import UUID

import pytest

from remit.kernel import (
    Actor,
    ActorKind,
    Command,
    Context,
    Document,
    Event,
    Kernel,
    Lifecycle,
    Module,
    RegistrationError,
    Rejected,
    RemitError,
    Store,
    Transition,
    Unauthorised,
)

desk = Module("desk")


@desk.document
class Ticket(Document):
    """A request someone has raised."""

    lifecycle = Lifecycle(
        initial="open",
        transitions=(
            Transition("resolve", "open", "resolved", requires=("resolution",)),
            Transition("reopen", "resolved", "open"),
            Transition("close", "resolved", "closed"),
        ),
    )

    subject: str
    resolution: str | None = None


@desk.document
class Notice(Document):
    """A message telling the requester their ticket was resolved."""

    lifecycle = Lifecycle(initial="sent")

    ticket_id: UUID


@desk.event
class TicketResolved(Event):
    """A ticket was resolved."""

    ticket_id: UUID


class OpenTicket(Command[Ticket]):
    subject: str


class RenameTicket(Command[Ticket]):
    ticket_id: UUID
    subject: str


class MoveTicket(Command[Ticket]):
    ticket_id: UUID
    transition: str
    resolution: str | None = None


class OpenThenFail(Command[None]):
    subject: str


class WriteFromStaleCopy(Command[None]):
    ticket_id: UUID


@desk.command
def open_ticket(command: OpenTicket, context: Context) -> Ticket:
    """Raise a ticket."""
    return context.create(Ticket(subject=command.subject))


@desk.command
def rename_ticket(command: RenameTicket, context: Context) -> Ticket:
    """Change a ticket's subject."""
    return context.update(context.get(Ticket, command.ticket_id), subject=command.subject)


@desk.command
def move_ticket(command: MoveTicket, context: Context) -> Ticket:
    """Move a ticket along its lifecycle."""
    ticket = context.get(Ticket, command.ticket_id)
    changes = {"resolution": command.resolution} if command.resolution else {}
    moved = context.transition(ticket, command.transition, **changes)
    if command.transition == "resolve":
        context.emit(TicketResolved(ticket_id=ticket.id))
    return moved


@desk.command
def open_then_fail(command: OpenThenFail, context: Context) -> None:
    """Raise a ticket through a nested command, then be refused."""
    context.execute(OpenTicket(subject=command.subject))
    raise Rejected("refused after the nested command ran")


@desk.command
def write_from_stale_copy(command: WriteFromStaleCopy, context: Context) -> None:
    """Update a ticket twice from the same copy."""
    ticket = context.get(Ticket, command.ticket_id)
    context.update(ticket, subject="first")
    context.update(ticket, subject="second")


@desk.subscribe(TicketResolved)
def notify_requester(event: TicketResolved, context: Context) -> None:
    """Send the requester a notice."""
    context.create(Notice(ticket_id=event.ticket_id))


AGENT = Actor(id="agent:desk", kind=ActorKind.AGENT, permissions=frozenset({"desk.*"}))
VISITOR = Actor(id="visitor", kind=ActorKind.HUMAN, permissions=frozenset({"desk.open_ticket"}))


@pytest.fixture
def kernel(store: Store) -> Kernel:
    return Kernel([desk], store=store)


@pytest.fixture
def ticket(kernel: Kernel) -> Ticket:
    return kernel.execute(OpenTicket(subject="Printer"), actor=AGENT).result


def test_a_command_commits_its_documents_and_audit_entry(kernel: Kernel) -> None:
    outcome = kernel.execute(OpenTicket(subject="Printer"), actor=AGENT)

    assert outcome.committed
    assert outcome.result.state == "open"
    assert kernel.get(Ticket, outcome.result.id) == outcome.result
    [entry] = kernel.audit()
    assert (entry.actor, entry.action, entry.input) == (
        AGENT,
        "desk.open_ticket",
        {"subject": "Printer"},
    )
    assert entry.documents == (("desk.ticket", outcome.result.id, 1),)


def test_an_actor_without_the_permission_is_refused(kernel: Kernel, ticket: Ticket) -> None:
    with pytest.raises(Unauthorised):
        kernel.execute(RenameTicket(ticket_id=ticket.id, subject="Mine now"), actor=VISITOR)
    assert kernel.get(Ticket, ticket.id) == ticket


def test_a_transition_waits_for_its_required_fields(kernel: Kernel, ticket: Ticket) -> None:
    with pytest.raises(Rejected, match="fill in resolution first"):
        kernel.execute(MoveTicket(ticket_id=ticket.id, transition="resolve"), actor=AGENT)

    resolved = kernel.execute(
        MoveTicket(ticket_id=ticket.id, transition="resolve", resolution="Replaced toner"),
        actor=AGENT,
    ).result
    assert (resolved.state, resolved.version) == ("resolved", 2)


def test_only_declared_transitions_are_allowed(kernel: Kernel, ticket: Ticket) -> None:
    with pytest.raises(Rejected, match="allowed here: resolve"):
        kernel.execute(MoveTicket(ticket_id=ticket.id, transition="close"), actor=AGENT)


def test_a_document_in_a_final_state_never_changes(kernel: Kernel, ticket: Ticket) -> None:
    for transition in ("resolve", "close"):
        kernel.execute(
            MoveTicket(ticket_id=ticket.id, transition=transition, resolution="Done"), actor=AGENT
        )
    closed = kernel.get(Ticket, ticket.id)

    with pytest.raises(Rejected, match="can no longer change"):
        kernel.execute(RenameTicket(ticket_id=ticket.id, subject="Again"), actor=AGENT)
    with pytest.raises(Rejected, match="this state is final"):
        kernel.execute(MoveTicket(ticket_id=ticket.id, transition="reopen"), actor=AGENT)
    assert kernel.get(Ticket, ticket.id) == closed


def test_a_refused_command_leaves_nothing_behind(kernel: Kernel) -> None:
    with pytest.raises(Rejected):
        kernel.execute(OpenThenFail(subject="Ghost"), actor=AGENT)

    assert kernel.find(Ticket) == []
    assert kernel.audit() == []


def test_a_dry_run_reports_what_would_happen_and_changes_nothing(
    kernel: Kernel, ticket: Ticket
) -> None:
    command = MoveTicket(ticket_id=ticket.id, transition="resolve", resolution="Replaced toner")
    outcome = kernel.execute(command, actor=AGENT, dry_run=True)

    assert not outcome.committed
    assert outcome.result.state == "resolved"
    assert outcome.events == (TicketResolved(ticket_id=ticket.id),)
    assert kernel.get(Ticket, ticket.id) == ticket
    assert kernel.events() == []


def test_an_idempotency_key_makes_a_retry_safe(kernel: Kernel) -> None:
    first = kernel.execute(OpenTicket(subject="Printer"), actor=AGENT, idempotency_key="k1")
    retry = kernel.execute(OpenTicket(subject="Printer"), actor=AGENT, idempotency_key="k1")

    assert retry.replayed
    assert (retry.result, retry.documents, retry.events) == (
        first.result,
        first.documents,
        first.events,
    )
    assert len(kernel.find(Ticket)) == 1
    with pytest.raises(Rejected, match="already used"):
        kernel.execute(OpenTicket(subject="Scanner"), actor=AGENT, idempotency_key="k1")


def test_a_subscription_receives_each_event_once(kernel: Kernel, ticket: Ticket) -> None:
    kernel.execute(
        MoveTicket(ticket_id=ticket.id, transition="resolve", resolution="Done"), actor=AGENT
    )

    assert kernel.dispatch().delivered == 1
    assert kernel.dispatch().delivered == 0
    [notice] = kernel.find(Notice)
    assert notice.ticket_id == ticket.id
    delivery = kernel.audit()[-1]
    assert (delivery.actor.id, delivery.action, delivery.cause) == (
        "system:desk",
        "desk.notify_requester",
        1,
    )


def test_writing_from_a_stale_copy_is_refused(kernel: Kernel, ticket: Ticket) -> None:
    with pytest.raises(RemitError, match="use the copy that change returned"):
        kernel.execute(WriteFromStaleCopy(ticket_id=ticket.id), actor=AGENT)
    assert kernel.get(Ticket, ticket.id) == ticket


def test_the_registry_describes_everything_a_module_declares(kernel: Kernel) -> None:
    described = kernel.registry.describe()["desk"]

    assert described["documents"]["desk.ticket"]["summary"] == "A request someone has raised."
    assert described["documents"]["desk.ticket"]["lifecycle"]["final"] == ["closed"]
    assert described["commands"]["desk.open_ticket"]["summary"] == "Raise a ticket."
    assert described["commands"]["desk.open_ticket"]["returns"]["title"] == "Ticket"
    assert "subject" in described["commands"]["desk.open_ticket"]["input"]["properties"]
    assert described["subscriptions"] == [
        {
            "name": "desk.notify_requester",
            "event": "desk.ticket_resolved",
            "summary": "Send the requester a notice.",
        }
    ]


def test_declarations_without_a_docstring_do_not_register() -> None:
    with pytest.raises(RegistrationError, match="needs a docstring"):

        @Module("bare").document
        class Undocumented(Document):
            lifecycle = Lifecycle(initial="new")


def test_a_transition_cannot_require_a_field_that_does_not_exist() -> None:
    broken = Module("broken")

    @broken.document
    class Parcel(Document):
        """A parcel."""

        lifecycle = Lifecycle(
            initial="packed",
            transitions=(Transition("ship", "packed", "shipped", requires=("carrier",)),),
        )

    with pytest.raises(RegistrationError, match="unknown fields"):
        Kernel([broken])
