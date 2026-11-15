"""Kernel behaviour, exercised through a toy module that has nothing to do with accounting."""

from collections.abc import Callable
from datetime import UTC, datetime
from uuid import UUID, uuid7

import pytest
from conftest import kernel_on
from pydantic import ValidationError

from remit.kernel import (
    Actor,
    ActorKind,
    AuditEntry,
    Command,
    ConcurrencyError,
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
from remit.kernel.store import Commit, StoredDocument

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
    version: int | None = None


class MoveTicket(Command[Ticket]):
    ticket_id: UUID
    transition: str
    resolution: str | None = None


class RenameAndCount(Command[tuple[int, int]]):
    ticket_id: UUID
    subject: str


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
    ticket = context.get(Ticket, command.ticket_id, version=command.version)
    return context.update(ticket, subject=command.subject)


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
def rename_and_count(command: RenameAndCount, context: Context) -> tuple[int, int]:
    """Rename a ticket, then count tickets under its old and new subjects."""
    ticket = context.get(Ticket, command.ticket_id)
    context.update(ticket, subject=command.subject)
    context.create(Ticket(subject=command.subject))
    old = context.find(Ticket, subject=ticket.subject)
    new = context.find(Ticket, subject=command.subject)
    return len(old), len(new)


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
    return kernel_on(store, [desk])


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


def test_find_sees_what_the_command_has_already_written(kernel: Kernel, ticket: Ticket) -> None:
    command = RenameAndCount(ticket_id=ticket.id, subject="Scanner")
    assert kernel.execute(command, actor=AGENT).result == (0, 2)


def test_a_command_refuses_fields_it_does_not_have() -> None:
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        RenameTicket.model_validate({"ticket_id": uuid7(), "subject": "Fax", "priority": "high"})


def test_a_change_made_from_an_out_of_date_copy_is_refused(kernel: Kernel, ticket: Ticket) -> None:
    renamed = kernel.execute(
        RenameTicket(ticket_id=ticket.id, subject="Scanner", version=ticket.version), actor=AGENT
    ).result
    assert renamed.version == ticket.version + 1

    with pytest.raises(Rejected, match=r"has changed since version 1 \(it is now at version 2\)"):
        kernel.execute(
            RenameTicket(ticket_id=ticket.id, subject="Fax", version=ticket.version), actor=AGENT
        )
    assert kernel.get(Ticket, ticket.id) == renamed


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


def test_a_unique_field_cannot_be_declared_on_a_field_that_does_not_exist() -> None:
    broken = Module("broken")

    @broken.document
    class Parcel(Document):
        """A parcel."""

        lifecycle = Lifecycle(initial="packed")
        unique = ("waybill",)

    with pytest.raises(RegistrationError, match=r"unknown fields \['waybill'\] unique"):
        Kernel([broken])


# Unique fields, through a module whose queues are known by a code.

routing = Module("routing")


@routing.document
class Queue(Document):
    """Where tickets of one kind wait."""

    lifecycle = Lifecycle(initial="open", transitions=(Transition("close", "open", "closed"),))
    unique = ("code", "email")

    code: str
    email: str | None = None


class CreateQueue(Command[Queue]):
    code: str
    email: str | None = None


class ChangeQueue(Command[Queue]):
    queue_id: UUID
    code: str
    email: str | None = None


@routing.command
def create_queue(command: CreateQueue, context: Context) -> Queue:
    """Create a queue."""
    return context.create(Queue(code=command.code, email=command.email))


@routing.command
def change_queue(command: ChangeQueue, context: Context) -> Queue:
    """Change a queue's code and email."""
    queue = context.get(Queue, command.queue_id)
    return context.update(queue, code=command.code, email=command.email)


@pytest.fixture
def routes(store: Store) -> Kernel:
    return kernel_on(store, [routing])


ROUTER = Actor(id="router", kind=ActorKind.HUMAN, permissions=frozenset({"routing.*"}))


def test_a_unique_value_cannot_be_used_twice(routes: Kernel) -> None:
    routes.execute(CreateQueue(code="IT", email="it@example.com"), actor=ROUTER)

    with pytest.raises(Rejected, match=r"routing\.queue code 'IT' is already in use"):
        routes.execute(CreateQueue(code="IT"), actor=ROUTER)
    with pytest.raises(Rejected, match=r"routing\.queue email 'it@example\.com' is already in use"):
        routes.execute(CreateQueue(code="HR", email="it@example.com"), actor=ROUTER)
    assert len(routes.find(Queue)) == 1


def test_a_document_may_keep_its_own_unique_value_but_not_take_another(routes: Kernel) -> None:
    it = routes.execute(CreateQueue(code="IT"), actor=ROUTER).result
    routes.execute(CreateQueue(code="HR"), actor=ROUTER)

    kept = routes.execute(
        ChangeQueue(queue_id=it.id, code="IT", email="it@example.com"), actor=ROUTER
    )
    assert kept.result.email == "it@example.com"
    with pytest.raises(Rejected, match="code 'HR' is already in use"):
        routes.execute(ChangeQueue(queue_id=it.id, code="HR"), actor=ROUTER)


def test_unset_unique_values_do_not_collide(routes: Kernel) -> None:
    routes.execute(CreateQueue(code="IT"), actor=ROUTER)
    routes.execute(CreateQueue(code="HR"), actor=ROUTER)
    assert len(routes.find(Queue, email=None)) == 2


# A module writes only its own documents and emits only its own events. Another module
# that wants a ticket changed must ask desk, through desk's commands.

helpdesk_bot = Module("helpdesk_bot", requires=(desk,))


class Hijack(Command[None]):
    ticket_id: UUID


class FakeResolution(Command[None]):
    ticket_id: UUID


class RenameThroughDesk(Command[Ticket]):
    ticket_id: UUID


class OpenElsewhere(Command[None]):
    pass


@helpdesk_bot.command
def hijack(command: Hijack, context: Context) -> None:
    """Rename a ticket that belongs to desk, directly."""
    context.update(context.get(Ticket, command.ticket_id), subject="Mine now")


@helpdesk_bot.command
def fake_resolution(command: FakeResolution, context: Context) -> None:
    """Announce that a ticket was resolved, without resolving it."""
    context.emit(TicketResolved(ticket_id=command.ticket_id))


@helpdesk_bot.command
def rename_through_desk(command: RenameThroughDesk, context: Context) -> Ticket:
    """Rename a ticket by asking desk to."""
    return context.execute(RenameTicket(ticket_id=command.ticket_id, subject="Via desk"))


@helpdesk_bot.subscribe(TicketResolved)
def close_resolved(event: TicketResolved, context: Context) -> None:
    """Close a resolved ticket directly, which only desk may do."""
    context.transition(context.get(Ticket, event.ticket_id), "close")


BOT = Actor(id="agent:bot", kind=ActorKind.AGENT, permissions=frozenset({"*"}))


@pytest.fixture
def bot(store: Store) -> Kernel:
    return kernel_on(store, [helpdesk_bot])


def test_a_module_cannot_write_another_modules_documents(bot: Kernel) -> None:
    ticket = bot.execute(OpenTicket(subject="Printer"), actor=BOT).result

    with pytest.raises(RemitError, match=r"helpdesk_bot cannot write desk\.ticket"):
        bot.execute(Hijack(ticket_id=ticket.id), actor=BOT)
    assert bot.get(Ticket, ticket.id) == ticket


def test_a_module_cannot_emit_another_modules_events(bot: Kernel) -> None:
    with pytest.raises(RemitError, match=r"helpdesk_bot cannot emit desk\.ticket_resolved"):
        bot.execute(FakeResolution(ticket_id=uuid7()), actor=BOT)
    assert bot.events() == []


def test_a_module_changes_another_modules_documents_through_its_commands(bot: Kernel) -> None:
    ticket = bot.execute(OpenTicket(subject="Printer"), actor=BOT).result

    renamed = bot.execute(RenameThroughDesk(ticket_id=ticket.id), actor=BOT).result

    assert bot.get(Ticket, ticket.id) == renamed
    assert renamed.subject == "Via desk"


def test_a_subscription_cannot_write_another_modules_documents(bot: Kernel) -> None:
    ticket = bot.execute(OpenTicket(subject="Printer"), actor=BOT).result
    bot.execute(MoveTicket(ticket_id=ticket.id, transition="resolve", resolution="Done"), actor=BOT)

    dispatched = bot.dispatch()

    [failure] = dispatched.failures
    assert failure.subscription == "helpdesk_bot.close_resolved"
    assert "cannot write desk.ticket" in failure.error
    assert bot.get(Ticket, ticket.id).state == "resolved"
    assert len(bot.find(Notice)) == 1, "desk's own subscription is still delivered"


def test_a_module_cannot_execute_commands_of_a_module_it_does_not_require(store: Store) -> None:
    outsider = Module("outsider")

    @outsider.command
    def open_elsewhere(command: OpenElsewhere, context: Context) -> None:
        """Open a ticket through a module this one does not require."""
        context.execute(OpenTicket(subject="Sneaky"))

    kernel = kernel_on(store, [desk, outsider])
    with pytest.raises(RemitError, match=r"outsider cannot execute desk\.open_ticket"):
        kernel.execute(OpenElsewhere(), actor=BOT)
    assert kernel.find(Ticket) == []


# One failing subscription must not hold up any other.

triage = Module("triage", requires=(desk,))
broken = {"triage.tally": True}


@triage.document
class Tally(Document):
    """A count kept for each resolved ticket."""

    lifecycle = Lifecycle(initial="counted")

    ticket_id: UUID


@triage.subscribe(TicketResolved)
def tally(event: TicketResolved, context: Context) -> None:
    """Fails with an ordinary Python error while it is broken."""
    if broken["triage.tally"]:
        _ = {}["missing"]
    context.create(Tally(ticket_id=event.ticket_id))


@triage.subscribe(TicketResolved)
def refuse(event: TicketResolved, context: Context) -> None:
    """Always refused by a rule."""
    raise Rejected("triage is closed")


@pytest.fixture
def triaged(store: Store) -> Kernel:
    broken["triage.tally"] = True
    return kernel_on(store, [triage])


def test_a_failing_subscription_does_not_stop_the_others(triaged: Kernel, ticket: Ticket) -> None:
    ticket = triaged.execute(OpenTicket(subject="Printer"), actor=AGENT).result
    resolve = MoveTicket(ticket_id=ticket.id, transition="resolve", resolution="Done")
    triaged.execute(resolve, actor=AGENT)

    for _ in range(2):  # a failure stays where it is, and does not block later passes
        dispatched = triaged.dispatch()
        assert [(f.subscription, f.position) for f in dispatched.failures] == [
            ("triage.tally", 1),
            ("triage.refuse", 1),
        ]
        assert dispatched.failures[0].error == "KeyError: 'missing'"
        assert dispatched.failures[1].error == "triage is closed"
    assert len(triaged.find(Notice)) == 1, "desk's subscription ran despite triage's"
    assert triaged.find(Tally) == []

    broken["triage.tally"] = False
    assert triaged.dispatch().delivered == 1
    assert [tally.ticket_id for tally in triaged.find(Tally)] == [ticket.id]


# Subscription names: each keeps its place in the log under its name.


def test_a_conflict_during_delivery_is_retried_not_reported_as_a_failure(store: Store) -> None:
    contended = Module("contended", requires=(desk,))
    conflicts = [ConcurrencyError("another worker got there first")]

    @contended.subscribe(TicketResolved)
    def count_once(event: TicketResolved, context: Context) -> None:
        """Conflicts once, as a delivery racing another worker would."""
        if conflicts:
            raise conflicts.pop()

    kernel = kernel_on(store, [contended])
    ticket = kernel.execute(OpenTicket(subject="Printer"), actor=AGENT).result
    kernel.execute(
        MoveTicket(ticket_id=ticket.id, transition="resolve", resolution="Done"), actor=AGENT
    )

    dispatched = kernel.dispatch()

    assert (dispatched.delivered, dispatched.failures) == (2, ())
    assert conflicts == []


def test_a_subscription_name_must_be_one_identifier() -> None:
    with pytest.raises(RegistrationError, match="subscription name 'Notify-Them' must be"):
        Module("strict").subscribe(TicketResolved, name="Notify-Them")(notify_requester)


def test_two_subscriptions_cannot_share_a_name() -> None:
    twins = Module("twins")

    def reaction(summary: str) -> Callable[[TicketResolved, Context], None]:
        def react(event: TicketResolved, context: Context) -> None:
            pass

        react.__doc__ = summary
        return react

    twins.subscribe(TicketResolved)(reaction("The first reaction."))
    with pytest.raises(RegistrationError, match=r"twins\.react is declared twice"):
        # Same name, so it would share the first one's place in the log.
        twins.subscribe(TicketResolved)(reaction("A second reaction."))


def test_a_subscription_cannot_take_a_commands_name() -> None:
    clash = Module("clash", requires=(desk,))

    @clash.command
    def open_elsewhere(command: OpenElsewhere, context: Context) -> None:
        """A command, named after its command class: clash.open_elsewhere."""

    with pytest.raises(RegistrationError, match=r"clash\.open_elsewhere is declared twice"):
        clash.subscribe(TicketResolved, name="open_elsewhere")(notify_requester)


def test_a_subscription_can_be_given_a_name_that_survives_renaming_its_handler() -> None:
    steady = Module("steady", requires=(desk,))

    @steady.subscribe(TicketResolved, name="notify")
    def notify_requester_by_email(event: TicketResolved, context: Context) -> None:
        """Tell the requester."""

    [subscription] = steady.subscriptions
    assert subscription.name == "steady.notify"


def test_module_names_cannot_contain_what_separates_names() -> None:
    for name in ("a.b", "Desk", "", "1desk", "desk-2"):
        with pytest.raises(RegistrationError, match="must be lower case letters"):
            Module(name)


def test_a_class_cannot_be_registered_by_two_modules() -> None:
    copycat = Module("copycat")
    copycat.document(Ticket)
    with pytest.raises(RegistrationError, match=r"copycat\.ticket is the class already registered"):
        Kernel([desk, copycat])


# Idempotency keys belong to the actor who sent them.


def test_idempotency_keys_of_different_actors_never_collide(kernel: Kernel) -> None:
    ops = Actor(id="ops", kind=ActorKind.HUMAN, permissions=frozenset({"desk.*"}))
    ops_bot = Actor(id="ops:bot", kind=ActorKind.AGENT, permissions=frozenset({"desk.*"}))

    first = kernel.execute(OpenTicket(subject="Printer"), actor=ops, idempotency_key="bot:1")
    second = kernel.execute(OpenTicket(subject="Printer"), actor=ops_bot, idempotency_key="1")

    assert not second.replayed
    assert first.result.id != second.result.id
    assert len(kernel.find(Ticket)) == 2


# What a store keeps must be the same whichever store it is.


def test_finding_by_a_field_that_does_not_exist_is_refused(kernel: Kernel, ticket: Ticket) -> None:
    with pytest.raises(RemitError, match=r"cannot find desk\.ticket by subjcet"):
        kernel.find(Ticket, subjcet="Printer")


def test_text_no_store_can_keep_is_refused_by_every_store(kernel: Kernel) -> None:
    with pytest.raises(Rejected, match=r"desk\.open_ticket: input\.subject contains U\+0000"):
        kernel.execute(OpenTicket(subject="Prin\x00ter"), actor=AGENT)
    assert kernel.find(Ticket) == []
    assert kernel.audit() == []


class OpenWithId(Command[Ticket]):
    ticket_id: UUID


@desk.command
def open_with_id(command: OpenWithId, context: Context) -> Ticket:
    """Raise a ticket with an id chosen by the caller."""
    return context.create(Ticket(id=command.ticket_id, subject="Chosen"))


def test_creating_a_document_whose_id_is_taken_is_refused_at_once(
    kernel: Kernel, ticket: Ticket
) -> None:
    with pytest.raises(Rejected, match=f"there is already a desk.ticket with id {ticket.id}"):
        kernel.execute(OpenWithId(ticket_id=ticket.id), actor=AGENT)
    assert kernel.get(Ticket, ticket.id) == ticket


class Blank(Command[Ticket]):
    ticket_id: UUID
    field: str


@desk.command
def blank(command: Blank, context: Context) -> Ticket:
    """Set one of a ticket's fields to nothing."""
    return context.update(context.get(Ticket, command.ticket_id), **{command.field: None})


def test_a_change_that_does_not_validate_is_rejected(kernel: Kernel, ticket: Ticket) -> None:
    with pytest.raises(Rejected, match=r"desk\.ticket .*: subject: Input should be a valid string"):
        kernel.execute(Blank(ticket_id=ticket.id, field="subject"), actor=AGENT)
    with pytest.raises(RemitError, match=r"desk\.ticket has no fields priority"):
        kernel.execute(Blank(ticket_id=ticket.id, field="priority"), actor=AGENT)
    assert kernel.get(Ticket, ticket.id) == ticket


# The store's own guards, which the kernel's checks normally keep from ever being reached.


def stored_as(kernel: Kernel, ticket: Ticket, *, version: int, final: bool = False) -> Commit:
    """A commit writing `ticket` at `version`, as a kernel bug might send it."""
    document = kernel.registry.document_name(Ticket)
    body = ticket.model_copy(update={"version": version}).model_dump(mode="json")
    audit = AuditEntry(
        at=datetime.now(UTC), actor=AGENT, action="test", input={}, documents=(), events=()
    )
    return Commit((StoredDocument(document, ticket.id, version, final, body),), (), audit)


def test_a_store_refuses_a_write_from_an_out_of_date_version(
    kernel: Kernel, ticket: Ticket
) -> None:
    kernel.execute(RenameTicket(ticket_id=ticket.id, subject="Scanner"), actor=AGENT)

    with pytest.raises(ConcurrencyError), kernel.store.unit() as unit:
        unit.commit(stored_as(kernel, ticket, version=2))
    assert kernel.get(Ticket, ticket.id).subject == "Scanner"


def test_a_store_refuses_to_change_a_final_document(kernel: Kernel, ticket: Ticket) -> None:
    with kernel.store.unit() as unit:
        unit.commit(stored_as(kernel, ticket, version=2, final=True))

    with pytest.raises(Rejected, match="final"), kernel.store.unit() as unit:
        unit.commit(stored_as(kernel, ticket, version=3))
    assert kernel.get(Ticket, ticket.id).version == 2


def test_a_store_refuses_an_idempotency_key_claimed_since_it_was_read(kernel: Kernel) -> None:
    kernel.execute(OpenTicket(subject="Printer"), actor=AGENT, idempotency_key="k1")
    audit = AuditEntry(
        at=datetime.now(UTC), actor=AGENT, action="test", input={}, documents=(), events=()
    )
    claim = Commit((), (), audit, idempotency=(AGENT.id, "k1", {}))

    with pytest.raises(ConcurrencyError), kernel.store.unit() as unit:
        unit.commit(claim)
