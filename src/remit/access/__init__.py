"""Access: the people and agents who sign in, and the roles that say what they may do.

A standard module, built only on the kernel's public API. Identity providers vouch for
who someone is; this module decides what they may do, through commands, so every
grant and revocation is audited. A server asks `SignIn` on every request, so a change
takes effect on the next one, and `ActorNames` to say who made the changes it shows.
"""

from .commands import (
    ActorNames,
    ChangeRole,
    DefineRole,
    EnrolMember,
    EraseMember,
    GrantRole,
    ReinstateMember,
    RenameMember,
    RetireRole,
    RevokeRole,
    SignIn,
    SuspendMember,
)
from .model import (
    Member,
    MemberEnrolled,
    MemberErased,
    MemberId,
    MemberKind,
    MemberReinstated,
    MemberRenamed,
    MemberSuspended,
    Permission,
    Role,
    RoleChanged,
    RoleDefined,
    RoleGranted,
    RoleId,
    RoleRetired,
    RoleRevoked,
    module,
)

__all__ = [
    "ActorNames",
    "ChangeRole",
    "DefineRole",
    "EnrolMember",
    "EraseMember",
    "GrantRole",
    "Member",
    "MemberEnrolled",
    "MemberErased",
    "MemberId",
    "MemberKind",
    "MemberReinstated",
    "MemberRenamed",
    "MemberSuspended",
    "Permission",
    "ReinstateMember",
    "RenameMember",
    "RetireRole",
    "RevokeRole",
    "Role",
    "RoleChanged",
    "RoleDefined",
    "RoleGranted",
    "RoleId",
    "RoleRetired",
    "RoleRevoked",
    "SignIn",
    "SuspendMember",
    "module",
]
