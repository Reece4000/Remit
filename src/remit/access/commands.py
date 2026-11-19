"""The access module's commands, and the query that signs a member in.

No one can grant, change or take away what they do not hold themselves. Giving a role,
taking one, changing what a role grants, and suspending or erasing a member are all
refused unless whoever does it holds every permission concerned. So a person who may
manage access cannot raise themselves, or anyone else, above their own permissions,
nor act against someone who holds more than they do.
"""

from collections.abc import Iterable
from typing import Annotated
from uuid import UUID

from remit.kernel import (
    ERASED,
    Actor,
    Code,
    Command,
    Context,
    Name,
    Personal,
    Problem,
    Query,
    Reader,
    Rejected,
)

from .model import (
    Issuer,
    Member,
    MemberEnrolled,
    MemberErased,
    MemberId,
    MemberKind,
    MemberReinstated,
    MemberRenamed,
    MemberSuspended,
    Permissions,
    Role,
    RoleChanged,
    RoleDefined,
    RoleGranted,
    RoleId,
    RoleIds,
    RoleRetired,
    RoleRevoked,
    Subject,
    module,
)


class DefineRole(Command[Role]):
    code: Code
    name: Name
    permissions: Permissions


class ChangeRole(Command[Role]):
    """Replace a role's name and permissions. Both are required."""

    role_id: RoleId
    version: int
    """The version the role was read at. Refused if it has changed since."""
    name: Name
    permissions: Permissions


class RetireRole(Command[Role]):
    role_id: RoleId


class EnrolMember(Command[Member]):
    name: Annotated[Name, Personal]
    kind: MemberKind
    issuer: Issuer
    """The identity provider the member signs in with, exactly as its tokens name it."""
    subject: Annotated[Subject, Personal]
    """Who the member is to that provider: the `sub` of the tokens it issues them."""
    roles: RoleIds = ()


class RenameMember(Command[Member]):
    member_id: MemberId
    version: int
    """The version the member was read at. Refused if it has changed since."""
    name: Annotated[Name, Personal]


class GrantRole(Command[Member]):
    member_id: MemberId
    role_id: RoleId


class RevokeRole(Command[Member]):
    member_id: MemberId
    role_id: RoleId


class SuspendMember(Command[Member]):
    member_id: MemberId


class ReinstateMember(Command[Member]):
    member_id: MemberId


class EraseMember(Command[Member]):
    member_id: MemberId


class SignIn(Query[Actor | None]):
    issuer: Issuer
    subject: Subject


@module.command
def define_role(command: DefineRole, context: Context) -> Role:
    """Define a role. Its code is unique and never reused."""
    _require_holds(context, command.permissions, f"define role {command.code}")
    role = context.create(Role.model_validate(command.model_dump()))
    context.emit(RoleDefined(role_id=role.id, code=role.code))
    return role


@module.command
def change_role(command: ChangeRole, context: Context) -> Role:
    """Change a role's name or permissions, for everyone who holds it."""
    role = _active_role(context, command.role_id, version=command.version)
    changing = {*role.permissions, *command.permissions}
    _require_holds(context, changing, f"change role {role.code}")
    changed, fields = context.revise(role, name=command.name, permissions=command.permissions)
    context.emit(RoleChanged(role_id=role.id, changed=fields))
    return changed


@module.command
def retire_role(command: RetireRole, context: Context) -> Role:
    """Retire a role, so that it grants nothing to anyone from now on."""
    role = context.get(Role, command.role_id)
    _require_holds(context, role.permissions, f"retire role {role.code}")
    retired = context.transition(role, "retire")
    context.emit(RoleRetired(role_id=retired.id))
    return retired


@module.command
def enrol_member(command: EnrolMember, context: Context) -> Member:
    """Enrol a person or agent, so that they can sign in with the roles given."""
    roles = [_active_role(context, role_id) for role_id in dict.fromkeys(command.roles)]
    _require_holds(context, _granted(roles), "enrol a member with these roles")
    member = context.create(
        Member.model_validate({**command.model_dump(), "roles": tuple(role.id for role in roles)})
    )
    context.emit(MemberEnrolled(member_id=member.id))
    return member


@module.command
def rename_member(command: RenameMember, context: Context) -> Member:
    """Correct a member's name."""
    member = context.get(Member, command.member_id, version=command.version)
    if member.state == "erased":
        raise Rejected("this member has been erased; their name cannot be set again")
    renamed, _ = context.revise(member, name=command.name)
    context.emit(MemberRenamed(member_id=member.id))
    return renamed


@module.command
def grant_role(command: GrantRole, context: Context) -> Member:
    """Give a member a role."""
    member = _active_member(context, command.member_id)
    role = _active_role(context, command.role_id)
    if role.id in member.roles:
        raise Rejected(f"the member already holds role {role.code}")
    _require_holds(context, role.permissions, f"grant role {role.code}")
    granted = context.update(member, roles=(*member.roles, role.id))
    context.emit(RoleGranted(member_id=member.id, role_id=role.id))
    return granted


@module.command
def revoke_role(command: RevokeRole, context: Context) -> Member:
    """Take a role from a member. It takes effect on their next request."""
    member = _active_member(context, command.member_id)
    if command.role_id not in member.roles:
        raise Rejected("the member does not hold that role")
    role = context.get(Role, command.role_id)
    _require_holds(context, role.permissions, f"revoke role {role.code}")
    revoked = context.update(member, roles=tuple(r for r in member.roles if r != role.id))
    context.emit(RoleRevoked(member_id=member.id, role_id=role.id))
    return revoked


@module.command
def suspend_member(command: SuspendMember, context: Context) -> Member:
    """Stop a member signing in, from their next request, until they are reinstated."""
    member = context.get(Member, command.member_id)
    _require_holds(context, _granted(_roles(context, member)), "suspend this member")
    suspended = context.transition(member, "suspend")
    context.emit(MemberSuspended(member_id=member.id))
    return suspended


@module.command
def reinstate_member(command: ReinstateMember, context: Context) -> Member:
    """Let a suspended member sign in again, with the roles they hold."""
    member = context.get(Member, command.member_id)
    _require_holds(context, _granted(_roles(context, member)), "reinstate this member")
    reinstated = context.transition(member, "reinstate")
    context.emit(MemberReinstated(member_id=member.id))
    return reinstated


@module.command
def erase_member(command: EraseMember, context: Context) -> Member:
    """Erase a suspended member's name and sign-in. What they did keeps their id."""
    member = context.get(Member, command.member_id)
    _require_holds(context, _granted(_roles(context, member)), "erase this member")
    erased = context.transition(member, "erase", name=ERASED, subject=None)
    context.erase(erased)
    context.emit(MemberErased(member_id=member.id))
    return erased


@module.query
def sign_in(query: SignIn, reader: Reader) -> Actor | None:
    """Who an identity provider's subject is here, and what they may do.

    None unless they are an active member. Their permissions are those of the roles
    they hold that are not retired, read afresh each time.
    """
    found = reader.find(Member, issuer=query.issuer, subject=query.subject, state="active")
    if not found:
        return None
    (member,) = found
    return Actor(
        id=str(member.id),
        kind=member.kind.actor,
        permissions=frozenset(_granted(_roles(reader, member))),
    )


def _granted(roles: Iterable[Role]) -> list[str]:
    """What the roles grant: the permissions of those that are active."""
    return [p for role in roles if role.state == "active" for p in role.permissions]


def _roles(reader: Reader, member: Member) -> list[Role]:
    return [reader.get(Role, role_id) for role_id in member.roles]


def _require_holds(context: Context, permissions: Iterable[str], doing: str) -> None:
    """Refuse unless the actor holds every one of the permissions.

    A pattern is held when one of the actor's own patterns matches it as written, so
    `ledger.*` holds `ledger.read.*`, while `ledger.read.*` does not hold `ledger.*`.
    That is exact for patterns whose parts are names or `*`, as permissions are.
    """
    actor = context.actor
    missing = sorted({p for p in permissions if not actor.may(p)})
    if missing:
        said = f"would need {', '.join(missing)}, which {actor.id} does not hold"
        raise Rejected(f"cannot {doing}: it {said}", problems=(Problem((), said),))


def _active_role(context: Context, role_id: UUID, *, version: int | None = None) -> Role:
    role = context.get(Role, role_id, version=version)
    if role.state != "active":
        raise Rejected(f"role {role.code} is {role.state}; it can no longer be given or changed")
    return role


def _active_member(context: Context, member_id: UUID) -> Member:
    member = context.get(Member, member_id)
    if member.state != "active":
        raise Rejected(f"this member is {member.state}; reinstate them first")
    return member
