"""The REST API: everything the registry describes, over HTTP, for people and agents alike.

Every route is generated from the registry, so a module's commands, documents and
queries are served as soon as it is installed, and the OpenAPI document describes each
one with its own types:

- `POST /commands/{name}` runs a command. `Idempotency-Key` makes a retry safe, and
  `?dry_run=true` reports what it would do without doing it.
- `GET /documents/{kind}` lists documents (`where`, `search`, `order`, `after`, `limit`)
  and says how many there are in all, `GET /documents/{kind}/values/{field}` counts the
  values a field holds across a listing, `GET /documents/{kind}/{id}` reads one, and
  `.../history` lists what wrote it.
- `POST /queries/{name}` asks a query. It changes nothing; it is a POST because its
  input is a body.
- `GET /changes` follows what is written, and `GET /changes/stream` sends it as it
  commits, as server-sent events. Each change, there and in a document's history, names
  who made it.
- `GET /audit` and `GET /events` read the audit trail and the event log.
- `GET /registry` describes what is installed, and `GET /me` who is asking and what
  they may do.

Each request carries an access token from a trusted identity provider (see `auth`).
Its subject is signed in through the access module on every request, so a revoked role
or a suspended member is refused from the next request on. The kernel then checks the
permission itself, as it does for every caller.

Errors are RFC 9457 problem details, with the fields at fault listed in `problems`.
"""

import asyncio
import inspect
import json
import logging
import time
from collections.abc import AsyncGenerator, AsyncIterator, Callable, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from enum import Enum
from functools import partial
from typing import Annotated, Any, cast
from uuid import UUID, uuid7

import anyio.to_thread
from fastapi import APIRouter, Body, Depends, FastAPI, Header, Path, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from fastapi.sse import EventSourceResponse, ServerSentEvent
from pydantic import ValidationError
from starlette.exceptions import HTTPException
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from remit import access
from remit.kernel import (
    DEFAULT_LIMIT,
    DEFAULT_VALUES,
    MAX_LIMIT,
    MAX_VALUES,
    Actor,
    ActorKind,
    Changes,
    CommandSpec,
    ConcurrencyError,
    Document,
    Kernel,
    NotFound,
    Problem,
    QuerySpec,
    Rejected,
    RemitError,
    Unauthorised,
    said,
    system_name,
)

from .auth import Identity, Unauthenticated, Verifier
from .feed import Commits
from .wire import (
    AuditEntrySent,
    AuditSent,
    ChangeSent,
    ChangesSent,
    EventSent,
    EventsSent,
    Executed,
    FieldProblem,
    Listed,
    Me,
    PositionSent,
    ProblemSent,
    ValuesSent,
    Written,
)

log = logging.getLogger(__name__)


MAX_BODY = 1_048_576
"""The largest request body accepted, in bytes."""

_PROBLEM = "application/problem+json"
_PROBLEMS: dict[int | str, dict[str, Any]] = {
    status: {"model": ProblemSent, "content": {_PROBLEM: {}}, "description": description}
    for status, description in {
        401: "No acceptable access token",
        403: "Not a member, or not permitted",
        404: "Not found",
        409: "Conflicted with another change; read again and retry",
        422: "Refused, with the fields at fault",
    }.items()
}


class NotMember(Exception):
    """A verified token whose subject is not an active member here."""


class NoToken(Unauthenticated):
    """The request carries no bearer token at all."""


@dataclass(frozen=True)
class _Served:
    """What a running API serves, kept in the app's state for its routes to reach."""

    kernel: Kernel
    verifier: Verifier
    commits: Commits
    heartbeat: float
    server: Actor
    """The server's own actor. It may ask who a token's subject is, and what the actors
    of the changes it shows are called, and nothing more."""


def _served(request: Request) -> _Served:
    return cast(_Served, request.app.state.remit)


_bearer = HTTPBearer(auto_error=False, description="An access token from a trusted provider.")


def _identity(
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
) -> Identity:
    if credentials is None:
        raise NoToken("send an access token from a trusted identity provider")
    return _served(request).verifier.verify(credentials.credentials)


def _sign_in(served: _Served, identity: Identity) -> Actor | None:
    try:
        asked = access.SignIn(issuer=identity.issuer, subject=identity.subject)
    except ValidationError:
        return None
    return served.kernel.ask(asked, actor=served.server)


def _named(served: _Served, changes: Changes) -> dict[str, str]:
    """What the actors of these changes are called, by id: people and agents as the access
    module names them, asked in one read for them all, and the system in words from its
    ids. Actors with no name, such as members whose names were erased, are left out."""
    named: dict[str, str] = {}
    members: set[str] = set()
    for change in changes.items:
        if change.actor_kind is not ActorKind.SYSTEM:
            members.add(change.actor)
        elif (called := system_name(change.actor)) is not None:
            named[change.actor] = called
    if members:
        asked = access.ActorNames(actors=tuple(sorted(members)))
        named |= served.kernel.ask(asked, actor=served.server)
    return named


def _member(request: Request, identity: Annotated[Identity, Depends(_identity)]) -> Actor:
    actor = _sign_in(_served(request), identity)
    if actor is None:
        raise NotMember
    return actor


SignedIn = Annotated[Identity, Depends(_identity)]
Member = Annotated[Actor, Depends(_member)]
After = Annotated[int, Query(ge=0, description="Read on from this position.")]
Limit = Annotated[int, Query(ge=1, le=MAX_LIMIT)]
Where = Annotated[
    str | None,
    Query(
        description='A JSON object: `{"state": "active"}`, or with operators, '
        '`{"weight": {"gte": 10}}`.'
    ),
]

_system = APIRouter(tags=["system"])


@_system.get("/health/live", include_in_schema=False)
def live() -> dict[str, str]:
    return {"status": "live"}


@_system.get("/health/ready", include_in_schema=False)
def ready(request: Request) -> JSONResponse:
    kernel = _served(request).kernel
    try:
        problems = kernel.store.missing(tuple(kernel.registry.modules.values()))
    except Exception as error:
        problems = (f"the store does not answer: {type(error).__name__}",)
    if problems:
        return JSONResponse({"status": "unready", "problems": list(problems)}, status_code=503)
    return JSONResponse({"status": "ready"})


@_system.get("/registry", operation_id="describe_registry")
def registry(request: Request, _: Member) -> dict[str, Any]:
    """Everything installed: modules, documents, commands, queries and events."""
    return _served(request).kernel.registry.describe()


@_system.get("/me", operation_id="me")
def me(request: Request, actor: Member) -> Me:
    """Who is asking, and every permission of what is installed that they hold."""
    return Me(
        id=actor.id,
        kind=actor.kind,
        permissions=sorted(actor.permissions),
        allowed=[p for p in _served(request).kernel.registry.permissions() if actor.may(p)],
    )


@_system.get("/changes", operation_id="changes")
def changes(
    request: Request, actor: Member, after: After = 0, limit: Limit = DEFAULT_LIMIT
) -> ChangesSent:
    """What has been written after a position, in commit order.

    Lists only the documents the reader may read, and leaves out work that wrote none of
    them. The position moves on regardless, so reading on always advances. Each change
    names who made it.
    """
    served = _served(request)
    found = served.kernel.changes(actor=actor, after=after, limit=limit)
    return ChangesSent.of(found, _named(served, found))


@_system.get("/changes/position", operation_id="changes_position")
def position(request: Request, actor: Member) -> PositionSent:
    """Where the changes feed has got to. Follow it from here to see only what is new."""
    return PositionSent(position=_served(request).kernel.position(actor=actor))


@_system.get("/changes/stream", operation_id="stream_changes", response_class=EventSourceResponse)
async def stream(
    request: Request,
    identity: SignedIn,
    _: Member,
    after: After = 0,
    last_event_id: Annotated[str | None, Header()] = None,
) -> AsyncIterator[ServerSentEvent]:
    """The changes feed as it commits, as server-sent events.

    Each `change` event carries its position as its id, so a client that reconnects with
    `Last-Event-ID` misses nothing. Comments keep the stream open while nothing happens.
    Permissions are read again on every pass, and the stream ends, with an `ended` event
    saying why, when the access token expires or the member is no longer active:
    reconnect with a fresh token.
    """
    served = _served(request)
    kernel = served.kernel
    position = int(last_event_id) if last_event_id and last_event_id.isdigit() else after
    yield ServerSentEvent(retry=3000, comment="following changes")
    while True:
        if time.time() >= identity.expires:
            yield ServerSentEvent(event="ended", data={"reason": "the access token expired"})
            return
        woken = served.commits.next()
        actor = await anyio.to_thread.run_sync(_sign_in, served, identity)
        if actor is None:
            yield ServerSentEvent(event="ended", data={"reason": "no longer a member"})
            return
        stretch = await anyio.to_thread.run_sync(
            partial(kernel.changes, actor=actor, after=position, limit=MAX_LIMIT)
        )
        names = await anyio.to_thread.run_sync(_named, served, stretch)
        for change in stretch.items:
            yield ServerSentEvent(
                id=str(change.position), event="change", data=ChangeSent.of(change, names)
            )
        if stretch.position != position:
            position = stretch.position
            continue
        with anyio.move_on_after(min(served.heartbeat, max(0.0, identity.expires - time.time()))):
            await woken.wait()
        if not woken.is_set():
            yield ServerSentEvent(comment="nothing new")


@_system.get("/audit", operation_id="audit")
def audit(
    request: Request,
    actor: Member,
    after: After = 0,
    limit: Limit = DEFAULT_LIMIT,
    kind: Annotated[str | None, Query(description="With `id`: only what wrote it.")] = None,
    id: UUID | None = None,
) -> AuditSent:
    """The audit trail after a position: who did what, and what they sent."""
    kernel = _served(request).kernel
    document = None
    if (kind is None and id is not None) or (kind is not None and id is None):
        missing = "kind" if kind is None else "id"
        said = "is needed with the other"
        raise Rejected(f"{missing} {said}", problems=(Problem((missing,), said),))
    if kind is not None and id is not None:
        found = kernel.registry.find_document(kind)
        if found is None:
            raise NotFound(f"there is no document {kind!r}")
        document = (found, id)
    entries = kernel.audit(actor=actor, after=after, document=document, limit=limit)
    return AuditSent(
        items=[AuditEntrySent.of(entry) for entry in entries],
        position=entries[-1].position if entries else after,
    )


@_system.get("/events", operation_id="events")
def events(
    request: Request, actor: Member, after: After = 0, limit: Limit = DEFAULT_LIMIT
) -> EventsSent:
    """The event log after a position, in the order events were committed."""
    recorded = _served(request).kernel.events(actor=actor, after=after, limit=limit)
    return EventsSent(
        items=[EventSent.of(event) for event in recorded],
        position=recorded[-1].position if recorded else after,
    )


def create_app(
    kernel: Kernel, verifier: Verifier, *, heartbeat: float = 15.0, max_body: int = MAX_BODY
) -> FastAPI:
    """The API for a kernel, trusting tokens the verifier accepts."""
    if access.module.name not in kernel.registry.modules:
        raise RemitError("the API signs members in through the access module; install it")
    commits = Commits(kernel.store)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncGenerator[None]:
        commits.start(asyncio.get_running_loop())
        try:
            yield
        finally:
            commits.stop()

    app = FastAPI(
        title="Remit",
        summary="Every command, document and query installed, for people and agents alike.",
        version="1",
        lifespan=lifespan,
        responses=_PROBLEMS,
        separate_input_output_schemas=False,
    )
    # FastAPI describes the stream of changes with `itemSchema`, which is OpenAPI 3.2's.
    app.openapi_version = "3.2.0"
    asks = (access.SignIn, access.ActorNames)
    server = Actor.system(
        "server", permissions=(kernel.registry.query(asked).permission for asked in asks)
    )
    app.state.remit = _Served(kernel, verifier, commits, heartbeat, server)
    _handle_errors(app)
    app.add_middleware(_Hygiene, max_body=max_body)
    app.include_router(_system)
    for module in kernel.registry.modules.values():
        for spec in module.commands.values():
            _command_route(app, kernel, spec)
        for name, kind in module.documents.items():
            _document_routes(app, kernel, name, kind)
        for query in module.queries.values():
            _query_route(app, kernel, query)
    return app


def _command_route(app: FastAPI, kernel: Kernel, spec: CommandSpec) -> None:
    def execute(
        command: Any, actor: Actor, idempotency_key: str | None, dry_run: bool
    ) -> dict[str, Any]:
        outcome = kernel.execute(
            command, actor=actor, idempotency_key=idempotency_key, dry_run=dry_run
        )
        name = kernel.registry.document_name
        return {
            "result": outcome.result,
            "documents": [
                Written(kind=name(type(d)), id=d.id, version=d.version) for d in outcome.documents
            ],
            "events": [kernel.registry.event_name(type(e)) for e in outcome.events],
            "committed": outcome.committed,
            "replayed": outcome.replayed,
        }

    parameters = {
        "command": Annotated[spec.command, Body()],
        "actor": Member,
        "idempotency_key": (
            Annotated[
                str | None,
                Header(
                    alias="Idempotency-Key",
                    min_length=1,
                    max_length=255,
                    description="Send the same key to retry safely: the command runs once.",
                ),
            ],
            None,
        ),
        "dry_run": (
            Annotated[bool, Query(description="Report what would happen; change nothing.")],
            False,
        ),
    }
    app.post(
        f"/commands/{spec.name}",
        response_model=Executed[spec.result],
        summary=spec.summary.partition("\n")[0],
        description=spec.summary,
        operation_id=f"execute_{_operation(spec.name)}",
        tags=[spec.module],
    )(_signed(execute, parameters))


def _document_routes(app: FastAPI, kernel: Kernel, name: str, kind: type[Document]) -> None:
    summary = (kind.__doc__ or "").strip()
    tags: list[str | Enum] = [name.partition(".")[0]]
    # Documents are read and listed with the fields their extensions add.
    extended = kernel.registry.extended(kind)

    def read(
        id: UUID, actor: Actor, if_none_match: str | None, response: Response
    ) -> Document | Response:
        document = kernel.get(extended, id, actor=actor)
        tag = f'"{document.version}"'
        if if_none_match is not None and tag in {t.strip() for t in if_none_match.split(",")}:
            return Response(status_code=304, headers={"ETag": tag})
        response.headers["ETag"] = tag
        return document

    app.get(
        f"/documents/{name}/{{id}}",
        response_model=extended,
        summary=f"Read a {name}",
        description=summary,
        operation_id=f"get_{_operation(name)}",
        tags=tags,
        responses={304: {"description": "Not changed since the version named"}},
    )(
        _signed(
            read,
            {
                "id": Annotated[UUID, Path()],
                "actor": Member,
                "if_none_match": (Annotated[str | None, Header()], None),
                "response": Response,
            },
        )
    )

    def listing(
        actor: Actor,
        where: str | None,
        search: str | None,
        order: str | None,
        after: str | None,
        limit: int,
    ) -> dict[str, Any]:
        page = kernel.page(
            extended,
            actor=actor,
            where=_object("where", where),
            search=search,
            order=order,
            after=after,
            limit=limit,
        )
        return {"items": page.items, "next": page.next, "total": page.total}

    app.get(
        f"/documents/{name}",
        response_model=Listed[extended],
        summary=f"List {name} documents",
        description=f"{summary}\n\nThe registry's `listing` says what can be filtered, "
        "ordered and searched.",
        operation_id=f"list_{_operation(name)}",
        tags=tags,
    )(
        _signed(
            listing,
            {
                "actor": Member,
                "where": (Where, None),
                "search": (Annotated[str | None, Query()], None),
                "order": (
                    Annotated[
                        str | None,
                        Query(
                            description="Fields separated by commas, most significant first, "
                            "each with `-` before it for descending: `-received,code`."
                        ),
                    ],
                    None,
                ),
                "after": (
                    Annotated[str | None, Query(description="The last page's `next`.")],
                    None,
                ),
                "limit": (Annotated[int, Query(ge=1, le=MAX_LIMIT)], DEFAULT_LIMIT),
            },
        )
    )

    def values(
        field: str, actor: Actor, where: str | None, search: str | None, limit: int
    ) -> ValuesSent:
        found = kernel.values(
            kind, field, actor=actor, where=_object("where", where), search=search, limit=limit
        )
        return ValuesSent.of(found)

    # Before the history of a document, which would otherwise take `values/history` for it.
    app.get(
        f"/documents/{name}/values/{{field}}",
        response_model=ValuesSent,
        summary=f"Count the values in a field of {name} documents",
        description="Each value the field holds in the documents `where` and `search` find, "
        "with how many hold it: null first, then lowest to highest, as a filter's choices "
        "are shown. Only fields a listing can filter on are counted; the registry's "
        "`listing` names them.",
        operation_id=f"values_{_operation(name)}",
        tags=tags,
    )(
        _signed(
            values,
            {
                "field": Annotated[str, Path()],
                "actor": Member,
                "where": (Where, None),
                "search": (Annotated[str | None, Query()], None),
                "limit": (Annotated[int, Query(ge=1, le=MAX_VALUES)], DEFAULT_VALUES),
            },
        )
    )

    def history(request: Request, id: UUID, actor: Actor, after: int, limit: int) -> ChangesSent:
        found = kernel.history(kind, id, actor=actor, after=after, limit=limit)
        return ChangesSent.of(found, _named(_served(request), found))

    app.get(
        f"/documents/{name}/{{id}}/history",
        response_model=ChangesSent,
        summary=f"What wrote a {name}",
        description="Each unit of work that wrote the document, oldest first, with who did it.",
        operation_id=f"history_{_operation(name)}",
        tags=tags,
    )(
        _signed(
            history,
            {
                "request": Request,
                "id": Annotated[UUID, Path()],
                "actor": Member,
                "after": (Annotated[int, Query(ge=0)], 0),
                "limit": (Annotated[int, Query(ge=1, le=MAX_LIMIT)], DEFAULT_LIMIT),
            },
        )
    )


def _query_route(app: FastAPI, kernel: Kernel, spec: QuerySpec) -> None:
    def ask(query: Any, actor: Actor) -> Any:
        return kernel.ask(query, actor=actor)

    app.post(
        f"/queries/{spec.name}",
        response_model=spec.result,
        summary=spec.summary.partition("\n")[0],
        description=f"{spec.summary}\n\nChanges nothing.",
        operation_id=f"ask_{_operation(spec.name)}",
        tags=[spec.module],
    )(_signed(ask, {"query": Annotated[spec.query, Body()], "actor": Member}))


def _signed[F: Callable[..., Any]](function: F, parameters: Mapping[str, Any]) -> F:
    """The function, declaring the parameters FastAPI is to fill: name to annotation, or
    to (annotation, default)."""
    declared = [
        inspect.Parameter(
            name,
            inspect.Parameter.KEYWORD_ONLY,
            annotation=given[0] if isinstance(given, tuple) else given,
            default=given[1] if isinstance(given, tuple) else inspect.Parameter.empty,
        )
        for name, given in parameters.items()
    ]
    function.__signature__ = inspect.Signature(declared)  # type: ignore[attr-defined]
    return function


def _operation(name: str) -> str:
    return name.replace(".", "_")


def _object(parameter: str, given: str | None) -> dict[str, object] | None:
    if given is None:
        return None
    try:
        found = json.loads(given)
    except ValueError:
        found = None
    if not isinstance(found, dict):
        said = "must be a JSON object"
        raise Rejected(f"{parameter} {said}", problems=(Problem((parameter,), said),))
    return found  # pyright: ignore[reportUnknownVariableType]


# Errors, as problem details.


def _problem(
    status: int,
    kind: str,
    title: str,
    detail: str,
    problems: tuple[Problem, ...] = (),
    *,
    headers: Mapping[str, str] | None = None,
) -> JSONResponse:
    body = ProblemSent(
        type=f"urn:remit:problem:{kind}",
        title=title,
        status=status,
        detail=detail,
        problems=[
            FieldProblem(path=list(p.path), message=p.message, document=p.document)
            for p in problems
        ],
    )
    return JSONResponse(
        body.model_dump(mode="json", exclude_defaults=True),
        status_code=status,
        media_type=_PROBLEM,
        headers=headers,
    )


def _handle_errors(app: FastAPI) -> None:
    @app.exception_handler(Unauthenticated)
    def unauthenticated(_: Request, error: Unauthenticated) -> JSONResponse:
        reason = str(error)
        if isinstance(error, NoToken):
            # RFC 6750: a request with no token at all is told only how to send one.
            challenge = 'Bearer realm="remit"'
        else:
            described = reason.replace("\\", "").replace('"', "'")
            challenge = (
                f'Bearer realm="remit", error="invalid_token", error_description="{described}"'
            )
        return _problem(
            401, "unauthenticated", "Not signed in", reason, headers={"WWW-Authenticate": challenge}
        )

    @app.exception_handler(NotMember)
    def not_member(_: Request, __: NotMember) -> JSONResponse:
        return _problem(
            403,
            "not-a-member",
            "Not a member",
            "the identity provider vouches for you, but you are not an active member here",
        )

    @app.exception_handler(Unauthorised)
    def unauthorised(_: Request, error: Unauthorised) -> JSONResponse:
        return _problem(403, "unauthorised", "Not permitted", str(error))

    @app.exception_handler(NotFound)
    def not_found(_: Request, error: NotFound) -> JSONResponse:
        return _problem(404, "not-found", "Not found", str(error), error.problems)

    @app.exception_handler(Rejected)
    def rejected(_: Request, error: Rejected) -> JSONResponse:
        return _problem(422, "rejected", "Refused", str(error), error.problems)

    @app.exception_handler(RequestValidationError)
    def invalid(_: Request, error: RequestValidationError) -> JSONResponse:
        problems = tuple(
            Problem(_located(tuple(found["loc"])), said(found))  # pyright: ignore[reportArgumentType]
            for found in error.errors()
        )
        detail = "; ".join(
            f"{'.'.join(map(str, p.path)) or 'input'}: {p.message}" for p in problems
        )
        return _problem(422, "rejected", "Refused", detail, problems)

    @app.exception_handler(ConcurrencyError)
    def conflict(_: Request, error: ConcurrencyError) -> JSONResponse:
        return _problem(409, "conflict", "Conflicted with another change", str(error))

    @app.exception_handler(HTTPException)
    def http(_: Request, error: HTTPException) -> JSONResponse:
        titles = {404: "Not found", 405: "Method not allowed"}
        title = titles.get(error.status_code, "Error")
        kind = title.lower().replace(" ", "-")
        return _problem(error.status_code, kind, title, str(error.detail), headers=error.headers)


def _located(location: tuple[str | int, ...]) -> tuple[str | int, ...]:
    """Where a field is in what was sent: the body's fields, or a parameter's name."""
    where, *rest = location
    return tuple(rest) if where == "body" else (where, *rest)


class _Hygiene:
    """What every response needs: an id to trace it by, no caching, and a bounded body.

    Bodies must give their length, so that one too large is refused before it is read.
    A failure nothing else handled is logged under the request's id, and answered with
    a problem that gives the id and nothing of the failure.
    """

    def __init__(self, app: ASGIApp, *, max_body: int) -> None:
        self._app = app
        self._max_body = max_body

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return
        headers = dict(scope["headers"])
        given = headers.get(b"x-request-id", b"").decode("latin-1")
        request_id = given if 0 < len(given) <= 128 and given.isprintable() else str(uuid7())

        started = False

        async def sending(message: Message) -> None:
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
                message["headers"] = [
                    *message.get("headers", []),
                    (b"x-request-id", request_id.encode("latin-1")),
                    (b"cache-control", b"no-store"),
                ]
            await send(message)

        length = headers.get(b"content-length")
        refusal: tuple[int, str, str] | None = None
        if length is None and b"transfer-encoding" in headers:
            refusal = (411, "length-required", "send the body with a Content-Length")
        elif length is not None and (not length.isdigit() or int(length) > self._max_body):
            refusal = (413, "too-large", f"the body must be at most {self._max_body} bytes")
        if refusal:
            status, kind, detail = refusal
            response = _problem(status, kind, "Body not accepted", detail)
            await response(scope, receive, sending)
            return
        try:
            await self._app(scope, receive, sending)
        except Exception:
            log.exception("%s %s failed, request %s", scope["method"], scope["path"], request_id)
            if started:
                raise  # too late to answer otherwise; the connection is cut
            detail = f"the server failed to handle this request; it is logged as {request_id}"
            await _problem(500, "failed", "Failed", detail)(scope, receive, sending)
