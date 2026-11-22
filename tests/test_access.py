"""Access: members, roles, signing in, and the rule that no one grants what they lack."""

from uuid import uuid7

import pytest
from conftest import ROOT, kernel_on
from pydantic import ValidationError

from remit import access, parties
from remit.access import MemberKind
from remit.kernel import ERASED, MAX_LIMIT, Actor, ActorKind, Kernel, Rejected, Store

ISSUER = "https://id.example.com/realms/remit"
OWNER = Actor(id="system:setup", kind=ActorKind.SYSTEM, permissions=frozenset({"*"}))


@pytest.fixture
def kernel(store: Store) -> Kernel:
    return kernel_on(store, [access.module, parties.module])


def role(kernel: Kernel, code: str, *permissions: str, actor: Actor = OWNER) -> access.Role:
    command = access.DefineRole(code=code, name=code.title(), permissions=permissions)
    return kernel.execute(command, actor=actor).result


def enrol(kernel: Kernel, subject: str, *roles: access.Role, actor: Actor = OWNER) -> access.Member:
    command = access.EnrolMember(
        name=subject.title(),
        kind=MemberKind.HUMAN,
        issuer=ISSUER,
        subject=subject,
        roles=tuple(r.id for r in roles),
    )
    return kernel.execute(command, actor=actor).result


def signed_in(kernel: Kernel, subject: str, issuer: str = ISSUER) -> Actor | None:
    return kernel.ask(access.SignIn(issuer=issuer, subject=subject), actor=ROOT)


def as_member(kernel: Kernel, subject: str) -> Actor:
    actor = signed_in(kernel, subject)
    assert actor is not None
    return actor


def test_a_member_signs_in_with_what_their_roles_grant(kernel: Kernel) -> None:
    clerk = role(kernel, "CLERK", "parties.*")
    reader = role(kernel, "READER", "*.read.*")
    ana = enrol(kernel, "ana", clerk, reader)

    actor = signed_in(kernel, "ana")
    assert actor == Actor(
        id=str(ana.id), kind=ActorKind.HUMAN, permissions=frozenset({"parties.*", "*.read.*"})
    )


def test_only_an_enrolled_subject_of_the_same_issuer_signs_in(kernel: Kernel) -> None:
    enrol(kernel, "ana")
    assert signed_in(kernel, "ana") is not None
    assert signed_in(kernel, "ben") is None
    assert signed_in(kernel, "ana", issuer="https://elsewhere.example.com") is None


def test_one_sign_in_belongs_to_one_member(kernel: Kernel) -> None:
    enrol(kernel, "ana")
    with pytest.raises(Rejected, match=r"issuer and subject .* are already in use together"):
        enrol(kernel, "ana")


def test_an_agent_signs_in_as_an_agent(kernel: Kernel) -> None:
    command = access.EnrolMember(
        name="Payables agent", kind=MemberKind.AGENT, issuer=ISSUER, subject="payables-bot"
    )
    kernel.execute(command, actor=OWNER)
    assert as_member(kernel, "payables-bot").kind is ActorKind.AGENT


def test_changing_a_role_changes_it_for_everyone_who_holds_it_at_once(kernel: Kernel) -> None:
    clerk = role(kernel, "CLERK", "parties.read.*")
    enrol(kernel, "ana", clerk)
    enrol(kernel, "ben", clerk)

    command = access.ChangeRole(
        role_id=clerk.id, version=clerk.version, name="Clerk", permissions=("parties.*",)
    )
    kernel.execute(command, actor=OWNER)
    assert as_member(kernel, "ana").permissions == {"parties.*"}
    assert as_member(kernel, "ben").permissions == {"parties.*"}


def test_a_retired_role_grants_nothing(kernel: Kernel) -> None:
    clerk = role(kernel, "CLERK", "parties.*")
    enrol(kernel, "ana", clerk)
    kernel.execute(access.RetireRole(role_id=clerk.id), actor=OWNER)

    assert as_member(kernel, "ana").permissions == frozenset()
    with pytest.raises(Rejected, match="role CLERK is retired"):
        enrol(kernel, "ben", clerk)


def test_granting_and_revoking_a_role(kernel: Kernel) -> None:
    clerk = role(kernel, "CLERK", "parties.*")
    ana = enrol(kernel, "ana")

    kernel.execute(access.GrantRole(member_id=ana.id, role_id=clerk.id), actor=OWNER)
    assert as_member(kernel, "ana").permissions == {"parties.*"}
    with pytest.raises(Rejected, match="already holds role CLERK"):
        kernel.execute(access.GrantRole(member_id=ana.id, role_id=clerk.id), actor=OWNER)

    kernel.execute(access.RevokeRole(member_id=ana.id, role_id=clerk.id), actor=OWNER)
    assert as_member(kernel, "ana").permissions == frozenset()
    with pytest.raises(Rejected, match="does not hold that role"):
        kernel.execute(access.RevokeRole(member_id=ana.id, role_id=clerk.id), actor=OWNER)


def test_a_suspended_member_cannot_sign_in_until_reinstated(kernel: Kernel) -> None:
    clerk = role(kernel, "CLERK", "parties.*")
    ana = enrol(kernel, "ana", clerk)

    kernel.execute(access.SuspendMember(member_id=ana.id), actor=OWNER)
    assert signed_in(kernel, "ana") is None
    with pytest.raises(Rejected, match="this member is suspended"):
        kernel.execute(access.GrantRole(member_id=ana.id, role_id=clerk.id), actor=OWNER)

    kernel.execute(access.ReinstateMember(member_id=ana.id), actor=OWNER)
    assert as_member(kernel, "ana").permissions == {"parties.*"}


def test_an_erased_member_keeps_their_id_and_loses_their_name_and_sign_in(
    kernel: Kernel,
) -> None:
    ana = enrol(kernel, "ana")
    with pytest.raises(Rejected, match="cannot erase from state 'active'"):
        kernel.execute(access.EraseMember(member_id=ana.id), actor=OWNER)

    kernel.execute(access.SuspendMember(member_id=ana.id), actor=OWNER)
    erased = kernel.execute(access.EraseMember(member_id=ana.id), actor=OWNER).result
    assert (erased.id, erased.name, erased.subject) == (ana.id, ERASED, None)
    assert signed_in(kernel, "ana") is None
    enrolled = next(e for e in kernel.audit(actor=ROOT) if e.action == "access.enrol_member")
    assert enrolled.input["name"] == ERASED
    assert enrolled.input["subject"] == ERASED

    again = enrol(kernel, "ana")
    assert again.id != ana.id
    with pytest.raises(Rejected, match="has been erased"):
        kernel.execute(
            access.RenameMember(member_id=ana.id, version=erased.version, name="Ana"),
            actor=OWNER,
        )


def test_a_member_is_renamed(kernel: Kernel) -> None:
    ana = enrol(kernel, "ana")
    command = access.RenameMember(member_id=ana.id, version=ana.version, name="Ana Lima")
    assert kernel.execute(command, actor=OWNER).result.name == "Ana Lima"


# No one grants, changes or takes away what they do not hold themselves.


@pytest.fixture
def manager(kernel: Kernel) -> Actor:
    """Someone who manages access and may work with parties, and nothing else."""
    managing = role(kernel, "MANAGER", "access.*", "parties.*")
    enrol(kernel, "manager", managing)
    return as_member(kernel, "manager")


def test_a_manager_cannot_define_a_role_above_themselves(kernel: Kernel, manager: Actor) -> None:
    with pytest.raises(Rejected, match=r"would need \*, ledger\.\*, which .* does not hold"):
        role(kernel, "ROOT", "*", "ledger.*", "parties.read.*", actor=manager)
    assert role(kernel, "CLERK", "parties.read.*", actor=manager).code == "CLERK"


def test_a_manager_cannot_grant_a_role_above_themselves(kernel: Kernel, manager: Actor) -> None:
    admin = role(kernel, "ADMIN", "*")
    ana = enrol(kernel, "ana")
    with pytest.raises(Rejected, match=r"cannot grant role ADMIN: it would need \*"):
        kernel.execute(access.GrantRole(member_id=ana.id, role_id=admin.id), actor=manager)
    with pytest.raises(Rejected, match="cannot enrol a member with these roles"):
        enrol(kernel, "ben", admin, actor=manager)


def test_a_manager_cannot_widen_a_role_they_could_not_define(
    kernel: Kernel, manager: Actor
) -> None:
    clerk = role(kernel, "CLERK", "parties.*")
    command = access.ChangeRole(
        role_id=clerk.id, version=clerk.version, name="Clerk", permissions=("*",)
    )
    with pytest.raises(Rejected, match=r"cannot change role CLERK: it would need \*"):
        kernel.execute(command, actor=manager)


def test_a_manager_cannot_act_against_someone_who_holds_more(
    kernel: Kernel, manager: Actor
) -> None:
    admin = role(kernel, "ADMIN", "*")
    owner = enrol(kernel, "owner", admin)
    for command in (
        access.SuspendMember(member_id=owner.id),
        access.RevokeRole(member_id=owner.id, role_id=admin.id),
        access.RetireRole(role_id=admin.id),
        access.ChangeRole(
            role_id=admin.id, version=admin.version, name="Admin", permissions=("parties.*",)
        ),
    ):
        with pytest.raises(Rejected, match=r"would need \*"):
            kernel.execute(command, actor=manager)
    assert as_member(kernel, "owner").permissions == {"*"}


def test_holding_a_pattern_is_matching_it_as_written(kernel: Kernel) -> None:
    auditor = Actor(
        id="auditor", kind=ActorKind.HUMAN, permissions=frozenset({"access.*", "*.read.*"})
    )
    role(kernel, "LEDGER_READER", "ledger.read.*", actor=auditor)
    for wider in ("*", "ledger.*", "*.read"):
        with pytest.raises(Rejected, match="would need"):
            role(kernel, f"WIDER{len(wider)}", wider, actor=auditor)


@pytest.mark.parametrize(
    "permission", ["", "Ledger.read", "ledger.read.acc*", "ledger..read", "ledger.?", ".ledger"]
)
def test_a_permission_is_names_and_stars_between_dots(permission: str) -> None:
    with pytest.raises(ValidationError):
        access.DefineRole(code="X", name="X", permissions=(permission,))


def test_a_role_grants_something() -> None:
    with pytest.raises(ValidationError):
        access.DefineRole(code="X", name="X", permissions=())


def test_signing_in_reads_only_with_its_own_permission(kernel: Kernel) -> None:
    enrol(kernel, "ana")
    server = Actor(
        id="system:api", kind=ActorKind.SYSTEM, permissions=frozenset({"access.read.sign_in"})
    )
    assert kernel.ask(access.SignIn(issuer=ISSUER, subject="ana"), actor=server) is not None
    with pytest.raises(Exception, match=r"does not hold the permission access\.read\.member"):
        kernel.get(access.Member, uuid7(), actor=server)


def test_actors_are_named_as_members_are_called_until_erased(kernel: Kernel) -> None:
    ana, ben = enrol(kernel, "ana"), enrol(kernel, "ben")
    enrolled = access.EnrolMember(
        name="Payables agent", kind=MemberKind.AGENT, issuer=ISSUER, subject="payables-bot"
    )
    agent = kernel.execute(enrolled, actor=OWNER).result
    for member in (ana, ben):
        kernel.execute(access.SuspendMember(member_id=member.id), actor=OWNER)
    kernel.execute(access.EraseMember(member_id=ben.id), actor=OWNER)

    server = Actor(
        id="system:api", kind=ActorKind.SYSTEM, permissions=frozenset({"access.read.actor_names"})
    )
    actors = (str(ana.id), str(ben.id), str(agent.id), OWNER.id, str(uuid7()))
    named = kernel.ask(access.ActorNames(actors=actors), actor=server)
    assert named == {str(ana.id): "Ana", str(agent.id): "Payables agent"}
    assert kernel.ask(access.ActorNames(actors=(OWNER.id,)), actor=server) == {}


def test_as_many_actors_are_named_at_once_as_a_page_of_changes_holds(kernel: Kernel) -> None:
    ana = enrol(kernel, "ana")
    unknown = tuple(str(uuid7()) for _ in range(MAX_LIMIT - 1))
    named = kernel.ask(access.ActorNames(actors=(*unknown, str(ana.id))), actor=ROOT)
    assert named == {str(ana.id): "Ana"}
    with pytest.raises(ValidationError, match="at most 200 items"):
        access.ActorNames(actors=(*unknown, str(ana.id), str(uuid7())))
