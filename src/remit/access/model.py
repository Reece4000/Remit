"""The access module's documents and events.

A member is a person or an agent who works in the system. They sign in through an
identity provider, which vouches for who they are; what they may do is decided here,
by the roles they hold, so that every grant and every revocation is a command in the
audit trail like any other change.

A member's name and the subject their identity provider knows them by are personal
data (see docs/personal-data.md). Events carry ids and codes only.
"""

from enum import StrEnum
from typing import Annotated
from uuid import UUID

from pydantic import Field, StringConstraints

from remit.kernel import (
    ActorKind,
    Code,
    Document,
    Event,
    Lifecycle,
    Module,
    Name,
    Personal,
    References,
    SingleLine,
    Transition,
)

module = Module("access")

RoleId = Annotated[UUID, References("access.role")]
RoleIds = Annotated[tuple[UUID, ...], References("access.role")]
MemberId = Annotated[UUID, References("access.member")]

Permission = Annotated[
    str,
    StringConstraints(
        pattern=r"^(?:\*|[a-z][a-z0-9_]*)(?:\.(?:\*|[a-z][a-z0-9_]*))*$", max_length=200
    ),
]
"""A permission, or a pattern of them: `ledger.post_journal_entry`, `ledger.read.*`, `*`.

Each part between dots is a name or `*`, and `*` stands for anything, dots included, as
the kernel matches it. So `ledger.*` grants every command and read of the ledger.
"""

Permissions = Annotated[tuple[Permission, ...], Field(min_length=1, max_length=100)]

Issuer = Annotated[str, StringConstraints(pattern=r"^https?://\S+$", max_length=500), SingleLine]
"""An identity provider, as the `iss` of the tokens it issues. Which providers are
trusted, and whether any may use plain http, is the server's configuration."""

Subject = Annotated[str, StringConstraints(min_length=1, max_length=255), SingleLine]
"""Who a member is to their identity provider, as the `sub` of its tokens."""


class MemberKind(StrEnum):
    HUMAN = "human"
    AGENT = "agent"

    @property
    def actor(self) -> ActorKind:
        return ActorKind(self.value)


@module.document
class Role(Document):
    """A set of permissions that members are given together, such as Bookkeeper.

    Changing a role's permissions changes them for everyone who holds it, at once. A
    retired role grants nothing, though members keep it on their record.
    """

    lifecycle = Lifecycle(
        initial="active", transitions=(Transition("retire", source="active", target="retired"),)
    )
    unique = ("code",)
    searchable = ("code", "name")

    code: Code
    name: Name
    permissions: Permissions


@module.document
class Member(Document):
    """A person or agent who signs in, and the roles they hold.

    A suspended member cannot sign in until reinstated. Erasing a member removes their
    name and sign-in for good; what they did stays in the audit trail under their id.
    """

    lifecycle = Lifecycle(
        initial="active",
        transitions=(
            Transition("suspend", source="active", target="suspended"),
            Transition("reinstate", source="suspended", target="active"),
            Transition("erase", source="suspended", target="erased"),
        ),
    )
    unique = (("issuer", "subject"),)
    searchable = ("name",)

    name: Annotated[Name, Personal]
    kind: MemberKind
    issuer: Issuer
    subject: Annotated[Subject | None, Personal]
    """Unset once the member is erased, so that the sign-in can never match again."""
    roles: RoleIds = ()


@module.event
class RoleDefined(Event):
    """A role was defined."""

    role_id: RoleId
    code: str


@module.event
class RoleChanged(Event):
    """A role's name or permissions were changed. `changed` names the fields that differ."""

    role_id: RoleId
    changed: tuple[str, ...]


@module.event
class RoleRetired(Event):
    """A role was retired, and grants nothing from now on."""

    role_id: RoleId


@module.event
class MemberEnrolled(Event):
    """A person or agent was enrolled, and can sign in."""

    member_id: MemberId


@module.event
class MemberRenamed(Event):
    """A member's name was corrected."""

    member_id: MemberId


@module.event
class RoleGranted(Event):
    """A member was given a role."""

    member_id: MemberId
    role_id: RoleId


@module.event
class RoleRevoked(Event):
    """A role was taken from a member."""

    member_id: MemberId
    role_id: RoleId


@module.event
class MemberSuspended(Event):
    """A member was suspended, and cannot sign in."""

    member_id: MemberId


@module.event
class MemberReinstated(Event):
    """A suspended member was reinstated."""

    member_id: MemberId


@module.event
class MemberErased(Event):
    """A member's name and sign-in were erased."""

    member_id: MemberId
