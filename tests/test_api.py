"""The REST API, driven as a client drives it: signed tokens in, problem details out.

The identity provider here is a real one in all but transport: it has a real RSA key
pair, publishes its discovery document and keys, and signs real tokens. Only the HTTP
it is reached over is in memory.
"""

import base64
import json
import threading
import time
from collections.abc import Iterator
from typing import Any
from uuid import uuid7

import httpx2
import pytest
from conftest import ROOT, kernel_on
from fastapi.testclient import TestClient
from joserfc import jwt
from joserfc.jwk import Key, KeySet, OctKey, RSAKey
from openapi_spec_validator import OpenAPIV32SpecValidator, validate

from remit import access, parties
from remit.kernel import (
    Actor,
    ActorKind,
    Code,
    Command,
    ConcurrencyError,
    Context,
    Document,
    Kernel,
    Lifecycle,
    Module,
    Name,
    Query,
    Reader,
    Store,
    Transition,
)
from remit.server import Provider, Unauthenticated, Verifier, create_app

ISSUER = "https://id.example.com/realms/acme"
AUDIENCE = "remit"
OWNER = Actor(id="system:setup", kind=ActorKind.SYSTEM, permissions=frozenset({"*"}))


KEY = RSAKey.generate_key(2048, parameters={"kid": "first"})
"""The provider's signing key. Made once: making an RSA key takes a while."""


class IdentityProvider:
    """An OpenID Connect provider with a real key pair, reached over in-memory HTTP."""

    def __init__(self, issuer: str = ISSUER) -> None:
        self.issuer = issuer
        self.key = KEY
        self.published: list[Key] = [self.key]
        self.fetches = 0
        self.down = False

    def handle(self, request: httpx2.Request) -> httpx2.Response:
        if self.down:
            return httpx2.Response(503)
        if str(request.url) == f"{self.issuer}/.well-known/openid-configuration":
            return httpx2.Response(
                200, json={"issuer": self.issuer, "jwks_uri": f"{self.issuer}/keys"}
            )
        if str(request.url) == f"{self.issuer}/keys":
            self.fetches += 1
            return httpx2.Response(200, json=KeySet(self.published).as_dict(private=False))
        return httpx2.Response(404)

    def token(
        self,
        subject: str,
        *,
        audience: str | list[str] = AUDIENCE,
        lasts: float = 300,
        key: RSAKey | None = None,
        **claims: Any,
    ) -> str:
        signing = key or self.key
        now = time.time()
        payload = {
            "iss": self.issuer,
            "sub": subject,
            "aud": audience,
            "iat": int(now),
            "exp": int(now + lasts),
            **claims,
        }
        return jwt.encode({"alg": "RS256", "kid": signing.kid}, payload, signing)


desk = Module("desk")


@desk.document
class Ticket(Document):
    """Something someone has asked for."""

    lifecycle = Lifecycle(initial="open", transitions=(Transition("close", "open", "closed"),))
    unique = ("code",)
    searchable = ("code", "subject")

    code: Code
    subject: Name


class OpenTicket(Command[Ticket]):
    code: Code
    subject: Name


class Waiting(Query[int]):
    """How many tickets are open."""


class Jam(Command[None]):
    """Something that always collides with other work."""


class Break(Command[None]):
    """Something that fails, as a bug in a module would."""


@desk.command
def open_ticket(command: OpenTicket, context: Context) -> Ticket:
    """Open a ticket."""
    return context.create(Ticket(code=command.code, subject=command.subject))


@desk.command
def jam(command: Jam, context: Context) -> None:
    """Collide with other work, every time."""
    raise ConcurrencyError("another unit changed this first")


@desk.command
def break_(command: Break, context: Context) -> None:
    """Fail."""
    raise RuntimeError("a bug")


@desk.query
def waiting(query: Waiting, reader: Reader) -> int:
    """How many tickets are open."""
    return len(reader.find(Ticket, state="open"))


@pytest.fixture
def provider() -> IdentityProvider:
    return IdentityProvider()


@pytest.fixture
def kernel(store: Store) -> Kernel:
    return kernel_on(store, [access.module, parties.module, desk])


@pytest.fixture
def client(kernel: Kernel, provider: IdentityProvider) -> Iterator[TestClient]:
    http = httpx2.Client(transport=httpx2.MockTransport(provider.handle))
    verifier = Verifier((Provider(issuer=ISSUER, audience=AUDIENCE),), http=http)
    with TestClient(create_app(kernel, verifier, heartbeat=0.05)) as client:
        yield client


def enrol(kernel: Kernel, subject: str, *permissions: str) -> access.Member:
    roles = ()
    if permissions:
        code = f"R{uuid7().hex[-8:].upper()}"
        role = kernel.execute(
            access.DefineRole(code=code, name=code, permissions=permissions), actor=OWNER
        ).result
        roles = (role.id,)
    command = access.EnrolMember(
        name=subject.title(),
        kind=access.MemberKind.HUMAN,
        issuer=ISSUER,
        subject=subject,
        roles=roles,
    )
    return kernel.execute(command, actor=OWNER).result


@pytest.fixture
def clerk(kernel: Kernel, provider: IdentityProvider) -> dict[str, str]:
    """Headers for a member who may work the desk and read parties."""
    enrol(kernel, "clerk", "desk.*", "parties.read.*")
    return {"Authorization": f"Bearer {provider.token('clerk')}"}


def problem(response: httpx2.Response, status: int, kind: str) -> dict[str, Any]:
    assert response.status_code == status, response.text
    assert response.headers["content-type"] == "application/problem+json"
    body = response.json()
    assert body["type"] == f"urn:remit:problem:{kind}"
    assert body["status"] == status
    return body


def open_ticket_now(client: TestClient, headers: dict[str, str], code: str) -> dict[str, Any]:
    response = client.post(
        "/commands/desk.open_ticket",
        json={"code": code, "subject": f"About {code}"},
        headers=headers,
    )
    assert response.status_code == 200, response.text
    return response.json()


# Signing in.


def test_a_request_without_a_token_is_told_how_to_send_one(client: TestClient) -> None:
    response = client.get("/me")
    problem(response, 401, "unauthenticated")
    assert response.headers["www-authenticate"] == 'Bearer realm="remit"'


def test_a_member_with_a_valid_token_is_signed_in(
    client: TestClient, kernel: Kernel, clerk: dict[str, str]
) -> None:
    me = client.get("/me", headers=clerk).json()
    assert me["kind"] == "human"
    assert me["permissions"] == ["desk.*", "parties.read.*"]
    assert "desk.open_ticket" in me["allowed"]
    assert "parties.read.party" in me["allowed"]
    assert "parties.register_party" not in me["allowed"]
    assert "kernel.read.audit" not in me["allowed"]


def refused(client: TestClient, token: str) -> str:
    response = client.get("/me", headers={"Authorization": f"Bearer {token}"})
    body = problem(response, 401, "unauthenticated")
    challenge = response.headers["www-authenticate"]
    assert challenge.startswith('Bearer realm="remit", error="invalid_token", error_description="')
    return body["detail"]


def test_tokens_that_are_not_acceptable_are_refused(
    client: TestClient, kernel: Kernel, provider: IdentityProvider
) -> None:
    enrol(kernel, "ana")
    assert "expired" in refused(client, provider.token("ana", lasts=-60))
    assert "aud" in refused(client, provider.token("ana", audience="another-service"))
    assert "not yet valid" in refused(client, provider.token("ana", nbf=int(time.time()) + 600))
    assert "not a signed JWT" in refused(client, "not.a.token")
    assert "not a signed JWT" in refused(client, "garbage")


def test_an_audience_among_several_is_accepted(
    client: TestClient, kernel: Kernel, provider: IdentityProvider
) -> None:
    enrol(kernel, "ana")
    token = provider.token("ana", audience=["account", AUDIENCE])
    assert client.get("/me", headers={"Authorization": f"Bearer {token}"}).status_code == 200


def test_a_token_from_an_issuer_not_trusted_is_refused(client: TestClient, kernel: Kernel) -> None:
    enrol(kernel, "ana")
    elsewhere = IdentityProvider("https://id.elsewhere.example.com")
    assert "not issued by a provider this server trusts" in refused(client, elsewhere.token("ana"))


def test_a_token_signed_by_a_key_the_issuer_does_not_publish_is_refused(
    client: TestClient, kernel: Kernel, provider: IdentityProvider
) -> None:
    enrol(kernel, "ana")
    forger = RSAKey.generate_key(2048, parameters={"kid": "first"})
    assert "signature" in refused(client, provider.token("ana", key=forger))


def test_a_token_signed_with_the_public_key_as_a_shared_secret_is_refused(
    client: TestClient, kernel: Kernel, provider: IdentityProvider
) -> None:
    """The classic confusion: HS256 'signed' with the issuer's own published key."""
    enrol(kernel, "ana")
    secret = OctKey.import_key(json.dumps(provider.key.as_dict(private=False)).encode())
    claims = {"iss": ISSUER, "sub": "ana", "aud": AUDIENCE, "exp": int(time.time()) + 300}
    forged = jwt.encode({"alg": "HS256", "kid": "first"}, claims, secret, algorithms=["HS256"])
    assert "signature" in refused(client, forged)


def test_an_unsigned_token_is_refused(client: TestClient, kernel: Kernel) -> None:
    enrol(kernel, "ana")

    def part(value: dict[str, Any]) -> str:
        return base64.urlsafe_b64encode(json.dumps(value).encode()).rstrip(b"=").decode()

    claims = {"iss": ISSUER, "sub": "ana", "aud": AUDIENCE, "exp": int(time.time()) + 300}
    assert "signature" in refused(client, f"{part({'alg': 'none'})}.{part(claims)}.")


def test_a_provider_that_rotates_its_keys_is_followed(
    client: TestClient, kernel: Kernel, provider: IdentityProvider
) -> None:
    enrol(kernel, "ana")
    assert client.get(
        "/me", headers={"Authorization": f"Bearer {provider.token('ana')}"}
    ).is_success
    rotated = RSAKey.generate_key(2048, parameters={"kid": "second"})
    provider.published = [provider.key, rotated]

    token = provider.token("ana", key=rotated)
    assert client.get("/me", headers={"Authorization": f"Bearer {token}"}).is_success
    assert provider.fetches == 2

    unknown = RSAKey.generate_key(2048, parameters={"kid": "third"})
    for _ in range(3):
        refused(client, provider.token("ana", key=unknown))
    assert provider.fetches == 2, "an unknown key must not fetch the keys on every request"


def test_cached_keys_are_used_while_the_provider_is_down(
    client: TestClient, kernel: Kernel, provider: IdentityProvider
) -> None:
    enrol(kernel, "ana")
    headers = {"Authorization": f"Bearer {provider.token('ana')}"}
    assert client.get("/me", headers=headers).is_success
    provider.down = True
    assert client.get("/me", headers=headers).is_success


def test_a_provider_that_cannot_be_reached_signs_no_one_in(
    client: TestClient, kernel: Kernel, provider: IdentityProvider
) -> None:
    enrol(kernel, "ana")
    provider.down = True
    said = refused(client, provider.token("ana"))
    assert said == f"the keys of {ISSUER} cannot be fetched"


def test_a_provider_that_is_down_is_not_asked_again_on_every_request(
    provider: IdentityProvider,
) -> None:
    asked: list[str] = []

    def down(request: httpx2.Request) -> httpx2.Response:
        asked.append(str(request.url))
        return httpx2.Response(503)

    now = [time.time()]
    verifier = verifier_for(down, clock=lambda: now[0])
    for _ in range(3):
        with pytest.raises(Unauthenticated):
            verifier.verify(provider.token("ana"))
    assert len(asked) == 1
    now[0] += 10
    with pytest.raises(Unauthenticated):
        verifier.verify(provider.token("ana"))
    assert len(asked) == 2


def test_a_token_whose_issuer_is_not_text_is_refused(client: TestClient) -> None:
    claims = {"iss": [ISSUER], "sub": "ana", "aud": AUDIENCE, "exp": int(time.time()) + 300}
    forged = jwt.encode({"alg": "RS256", "kid": "first"}, claims, KEY)
    assert "not issued by a provider this server trusts" in refused(client, forged)


def test_a_verified_subject_who_is_not_a_member_is_refused(
    client: TestClient, provider: IdentityProvider
) -> None:
    response = client.get("/me", headers={"Authorization": f"Bearer {provider.token('stranger')}"})
    problem(response, 403, "not-a-member")


def test_suspension_and_revocation_take_effect_on_the_next_request(
    client: TestClient, kernel: Kernel, provider: IdentityProvider
) -> None:
    ana = enrol(kernel, "ana", "desk.*")
    headers = {"Authorization": f"Bearer {provider.token('ana')}"}
    open_ticket_now(client, headers, "T-1")

    kernel.execute(access.RevokeRole(member_id=ana.id, role_id=ana.roles[0]), actor=OWNER)
    response = client.post(
        "/commands/desk.open_ticket", json={"code": "T-2", "subject": "More"}, headers=headers
    )
    problem(response, 403, "unauthorised")

    kernel.execute(access.SuspendMember(member_id=ana.id), actor=OWNER)
    problem(client.get("/me", headers=headers), 403, "not-a-member")


# Commands.


def test_a_command_runs_and_reports_what_it_wrote(
    client: TestClient, kernel: Kernel, clerk: dict[str, str]
) -> None:
    executed = open_ticket_now(client, clerk, "T-1")
    ticket = executed["result"]
    assert ticket["code"] == "T-1"
    assert executed["documents"] == [{"kind": "desk.ticket", "id": ticket["id"], "version": 1}]
    assert executed["committed"] is True
    assert executed["replayed"] is False

    [entry] = [e for e in kernel.audit(actor=ROOT) if e.action == "desk.open_ticket"]
    member = kernel.ask(access.SignIn(issuer=ISSUER, subject="clerk"), actor=ROOT)
    assert member is not None
    assert entry.actor == member


def test_a_command_sent_twice_with_one_key_runs_once(
    client: TestClient, kernel: Kernel, clerk: dict[str, str]
) -> None:
    sent = {"code": "T-1", "subject": "Once"}
    headers = {**clerk, "Idempotency-Key": "retry-me"}
    first = client.post("/commands/desk.open_ticket", json=sent, headers=headers).json()
    again = client.post("/commands/desk.open_ticket", json=sent, headers=headers).json()

    assert again["result"] == first["result"]
    assert again["replayed"] is True
    assert len(kernel.page(Ticket, actor=ROOT).items) == 1


def test_a_dry_run_changes_nothing(
    client: TestClient, kernel: Kernel, clerk: dict[str, str]
) -> None:
    response = client.post(
        "/commands/desk.open_ticket?dry_run=true",
        json={"code": "T-1", "subject": "Try"},
        headers=clerk,
    )
    assert response.json()["committed"] is False
    assert kernel.page(Ticket, actor=ROOT).items == ()


def test_input_a_command_does_not_accept_is_refused_field_by_field(
    client: TestClient, clerk: dict[str, str]
) -> None:
    response = client.post(
        "/commands/desk.open_ticket", json={"code": "lower case", "extra": 1}, headers=clerk
    )
    body = problem(response, 422, "rejected")
    paths = {tuple(p["path"]) for p in body["problems"]}
    assert paths == {("code",), ("subject",), ("extra",)}


def test_a_command_the_rules_refuse_says_which_field_and_why(
    client: TestClient, clerk: dict[str, str]
) -> None:
    open_ticket_now(client, clerk, "T-1")
    response = client.post(
        "/commands/desk.open_ticket", json={"code": "T-1", "subject": "Again"}, headers=clerk
    )
    body = problem(response, 422, "rejected")
    assert body["problems"] == [
        {"path": ["code"], "message": "'T-1' is already in use", "document": "desk.ticket"}
    ]


def test_a_command_the_member_may_not_run_is_refused(
    client: TestClient, clerk: dict[str, str]
) -> None:
    response = client.post(
        "/commands/parties.register_party",
        json={"code": "C1", "kind": "organisation", "name": "Acme"},
        headers=clerk,
    )
    body = problem(response, 403, "unauthorised")
    assert "parties.register_party" in body["detail"]


def test_an_idempotency_key_has_bounds(client: TestClient, clerk: dict[str, str]) -> None:
    response = client.post(
        "/commands/desk.open_ticket",
        json={"code": "T-1", "subject": "Long key"},
        headers={**clerk, "Idempotency-Key": "k" * 256},
    )
    body = problem(response, 422, "rejected")
    assert body["problems"][0]["path"] == ["header", "Idempotency-Key"]


def test_there_is_no_route_for_what_is_not_installed(
    client: TestClient, clerk: dict[str, str]
) -> None:
    problem(client.post("/commands/desk.burn_it_down", json={}, headers=clerk), 404, "not-found")
    problem(client.get("/documents/desk.nothing", headers=clerk), 404, "not-found")


# Documents.


def test_a_document_is_read_with_its_version_as_its_tag(
    client: TestClient, clerk: dict[str, str]
) -> None:
    ticket = open_ticket_now(client, clerk, "T-1")["result"]
    response = client.get(f"/documents/desk.ticket/{ticket['id']}", headers=clerk)
    assert response.json() == ticket
    assert response.headers["etag"] == '"1"'
    assert response.headers["cache-control"] == "no-store"

    unchanged = client.get(
        f"/documents/desk.ticket/{ticket['id']}", headers={**clerk, "If-None-Match": '"1"'}
    )
    assert unchanged.status_code == 304


def test_a_document_that_does_not_exist_is_not_found(
    client: TestClient, clerk: dict[str, str]
) -> None:
    problem(client.get(f"/documents/desk.ticket/{uuid7()}", headers=clerk), 404, "not-found")
    problem(client.get("/documents/desk.ticket/not-an-id", headers=clerk), 422, "rejected")


def test_documents_are_listed_filtered_searched_and_paged(
    client: TestClient, clerk: dict[str, str]
) -> None:
    for code in ("T-1", "T-2", "T-3"):
        open_ticket_now(client, clerk, code)

    def listed(**parameters: Any) -> dict[str, Any]:
        response = client.get("/documents/desk.ticket", params=parameters, headers=clerk)
        assert response.status_code == 200, response.text
        return response.json()

    first = listed(order="-code", limit=2)
    assert [t["code"] for t in first["items"]] == ["T-3", "T-2"]
    rest = listed(order="-code", limit=2, after=first["next"])
    assert [t["code"] for t in rest["items"]] == ["T-1"]
    assert rest["next"] is None

    chosen = listed(where=json.dumps({"code": {"in": ["T-1", "T-3"]}}), order="code")
    assert [t["code"] for t in chosen["items"]] == ["T-1", "T-3"]
    assert [t["code"] for t in listed(search="about t-2")["items"]] == ["T-2"]


def test_a_listing_that_cannot_be_run_says_why(client: TestClient, clerk: dict[str, str]) -> None:
    body = problem(
        client.get("/documents/desk.ticket", params={"where": "[1]"}, headers=clerk),
        422,
        "rejected",
    )
    assert body["problems"] == [{"path": ["where"], "message": "must be a JSON object"}]
    body = problem(
        client.get(
            "/documents/desk.ticket",
            params={"where": json.dumps({"nothing": 1}), "order": "nothing"},
            headers=clerk,
        ),
        422,
        "rejected",
    )
    assert {tuple(p["path"]) for p in body["problems"]} == {("where", "nothing"), ("order",)}
    problem(
        client.get("/documents/desk.ticket", params={"limit": 201}, headers=clerk), 422, "rejected"
    )


def test_a_documents_history(client: TestClient, kernel: Kernel, clerk: dict[str, str]) -> None:
    ticket = open_ticket_now(client, clerk, "T-1")["result"]
    history = client.get(f"/documents/desk.ticket/{ticket['id']}/history", headers=clerk).json()
    [change] = history["items"]
    assert change["action"] == "desk.open_ticket"
    assert change["documents"] == [{"kind": "desk.ticket", "id": ticket["id"], "version": 1}]
    assert history["position"] == change["position"]


def test_documents_the_member_may_not_read_are_refused(
    client: TestClient, kernel: Kernel, provider: IdentityProvider
) -> None:
    enrol(kernel, "outsider", "parties.read.*")
    headers = {"Authorization": f"Bearer {provider.token('outsider')}"}
    problem(client.get("/documents/desk.ticket", headers=headers), 403, "unauthorised")
    problem(client.get(f"/documents/desk.ticket/{uuid7()}", headers=headers), 403, "unauthorised")


# Queries.


def test_a_query_is_answered(client: TestClient, clerk: dict[str, str]) -> None:
    open_ticket_now(client, clerk, "T-1")
    response = client.post("/queries/desk.waiting", json={}, headers=clerk)
    assert response.json() == 1
    body = problem(
        client.post("/queries/desk.waiting", json={"x": 1}, headers=clerk), 422, "rejected"
    )
    assert body["problems"][0]["path"] == ["x"]


# Following changes.


def test_the_changes_feed_shows_only_what_the_member_may_read(
    client: TestClient, kernel: Kernel, provider: IdentityProvider, clerk: dict[str, str]
) -> None:
    open_ticket_now(client, clerk, "T-1")
    kernel.execute(
        parties.RegisterParty(code="C1", kind=parties.PartyKind.ORGANISATION, name="Acme"),
        actor=OWNER,
    )
    seen = client.get("/changes", headers=clerk).json()
    assert {d["kind"] for c in seen["items"] for d in c["documents"]} >= {
        "desk.ticket",
        "parties.party",
    }

    enrol(kernel, "desk_only", "desk.read.*")
    headers = {"Authorization": f"Bearer {provider.token('desk_only')}"}
    limited = client.get("/changes", headers=headers).json()
    assert {d["kind"] for c in limited["items"] for d in c["documents"]} == {"desk.ticket"}
    assert limited["position"] == seen["position"] + 2  # the enrolment wrote a role and a member


def read_stream(
    client: TestClient, headers: dict[str, str], **parameters: Any
) -> list[dict[str, Any]]:
    """Every server-sent event until the stream ends, as {field: value}."""
    events: list[dict[str, Any]] = []
    current: dict[str, Any] = {}
    with client.stream("GET", "/changes/stream", headers=headers, params=parameters) as response:
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        for line in response.iter_lines():
            if not line:
                if current:
                    events.append(current)
                current = {}
                continue
            field, _, value = line.partition(":")
            current[field or "comment"] = value.strip()
        if current:
            events.append(current)
    return events


def test_the_stream_sends_changes_as_they_commit_until_the_token_expires(
    client: TestClient, kernel: Kernel, provider: IdentityProvider, clerk: dict[str, str]
) -> None:
    open_ticket_now(client, clerk, "T-1")
    headers = {"Authorization": f"Bearer {provider.token('clerk', lasts=1.5)}"}

    later = threading.Timer(
        0.4, lambda: kernel.execute(OpenTicket(code="T-2", subject="Later"), actor=OWNER)
    )
    later.start()
    events = read_stream(client, headers)
    later.join()

    changes = [e for e in events if e.get("event") == "change"]
    # The clerk may not read access's documents, so the enrolment is not among them.
    assert [json.loads(e["data"])["documents"][0]["kind"] for e in changes] == [
        "desk.ticket",
        "desk.ticket",
    ]
    positions = [int(e["id"]) for e in changes]
    assert positions == sorted(positions)
    assert any(e.get("comment") == "nothing new" for e in events)
    assert events[-1]["event"] == "ended"
    assert json.loads(events[-1]["data"]) == {"reason": "the access token expired"}

    fresh = {"Authorization": f"Bearer {provider.token('clerk', lasts=2)}"}
    resumed = read_stream(client, {**fresh, "Last-Event-ID": str(positions[-2])})
    assert [int(e["id"]) for e in resumed if e.get("event") == "change"] == positions[-1:]


def test_the_stream_ends_when_the_member_is_suspended(
    client: TestClient, kernel: Kernel, provider: IdentityProvider
) -> None:
    ana = enrol(kernel, "ana", "desk.*")
    headers = {"Authorization": f"Bearer {provider.token('ana', lasts=10)}"}
    later = threading.Timer(
        0.3, lambda: kernel.execute(access.SuspendMember(member_id=ana.id), actor=OWNER)
    )
    later.start()
    events = read_stream(client, headers)
    later.join()
    assert json.loads(events[-1]["data"]) == {"reason": "no longer a member"}


# The audit trail, the event log, and what is installed.


def test_the_audit_trail_needs_its_own_permission(
    client: TestClient, kernel: Kernel, provider: IdentityProvider, clerk: dict[str, str]
) -> None:
    ticket = open_ticket_now(client, clerk, "T-1")["result"]
    problem(client.get("/audit", headers=clerk), 403, "unauthorised")

    enrol(kernel, "auditor", "*.read.*")
    headers = {"Authorization": f"Bearer {provider.token('auditor')}"}
    trail = client.get("/audit", headers=headers).json()
    assert [e["action"] for e in trail["items"]] == [
        "access.define_role",
        "access.enrol_member",
        "desk.open_ticket",
        "access.define_role",
        "access.enrol_member",
    ]
    of_ticket = client.get(
        "/audit", params={"kind": "desk.ticket", "id": ticket["id"]}, headers=headers
    ).json()
    assert [e["action"] for e in of_ticket["items"]] == ["desk.open_ticket"]
    assert of_ticket["items"][0]["input"] == {"code": "T-1", "subject": "About T-1"}

    body = problem(
        client.get("/audit", params={"kind": "desk.ticket"}, headers=headers), 422, "rejected"
    )
    assert body["problems"][0]["path"] == ["id"]
    problem(
        client.get("/audit", params={"kind": "desk.nothing", "id": str(uuid7())}, headers=headers),
        404,
        "not-found",
    )


def test_the_event_log_needs_its_own_permission(
    client: TestClient, kernel: Kernel, provider: IdentityProvider, clerk: dict[str, str]
) -> None:
    problem(client.get("/events", headers=clerk), 403, "unauthorised")
    enrol(kernel, "auditor", "*.read.*")
    headers = {"Authorization": f"Bearer {provider.token('auditor')}"}
    log = client.get("/events", params={"limit": 1}, headers=headers).json()
    assert log["items"][0]["name"] == "access.role_defined"
    assert log["position"] == log["items"][0]["position"]


def test_the_registry_is_described_to_members(client: TestClient, clerk: dict[str, str]) -> None:
    described = client.get("/registry", headers=clerk).json()
    assert "desk.open_ticket" in described["desk"]["commands"]


def test_the_openapi_document_is_valid_and_types_every_route(client: TestClient) -> None:
    document = client.get("/openapi.json").json()
    assert document["openapi"] == "3.2.0"
    validate(document, cls=OpenAPIV32SpecValidator)
    paths = document["paths"]
    assert "/commands/desk.open_ticket" in paths
    assert "/documents/desk.ticket/{id}" in paths
    assert "/queries/desk.waiting" in paths
    body = paths["/commands/desk.open_ticket"]["post"]["requestBody"]["content"]["application/json"]
    assert body["schema"]["$ref"].endswith("/OpenTicket")
    assert document["components"]["securitySchemes"]["HTTPBearer"]["scheme"] == "bearer"


# What every request gets.


def test_health_needs_no_token(client: TestClient) -> None:
    assert client.get("/health/live").json() == {"status": "live"}
    assert client.get("/health/ready").json() == {"status": "ready"}


def test_every_response_carries_a_request_id(client: TestClient) -> None:
    assert len(client.get("/health/live").headers["x-request-id"]) == 36
    given = client.get("/health/live", headers={"X-Request-ID": "trace-1"})
    assert given.headers["x-request-id"] == "trace-1"


def test_a_body_too_large_is_refused_before_it_is_read(
    kernel: Kernel, provider: IdentityProvider
) -> None:
    http = httpx2.Client(transport=httpx2.MockTransport(provider.handle))
    verifier = Verifier((Provider(issuer=ISSUER, audience=AUDIENCE),), http=http)
    with TestClient(create_app(kernel, verifier, max_body=100)) as client:
        response = client.post("/commands/desk.open_ticket", content=b"x" * 101)
        problem(response, 413, "too-large")


def test_the_api_needs_the_access_module(store: Store) -> None:
    with pytest.raises(Exception, match="access module"):
        create_app(kernel_on(store, [desk]), Verifier((), http=httpx2.Client()))


def test_work_that_keeps_colliding_is_a_conflict(client: TestClient, clerk: dict[str, str]) -> None:
    body = problem(client.post("/commands/desk.jam", json={}, headers=clerk), 409, "conflict")
    assert "another unit changed this first" in body["detail"]


def test_a_failure_is_reported_by_its_request_id_and_nothing_more(
    client: TestClient, clerk: dict[str, str], caplog: pytest.LogCaptureFixture
) -> None:
    response = client.post("/commands/desk.break", json={}, headers=clerk)
    body = problem(response, 500, "failed")
    request_id = response.headers["x-request-id"]
    assert "a bug" not in body["detail"]
    assert request_id in body["detail"]
    assert response.headers["cache-control"] == "no-store"
    assert f"POST /commands/desk.break failed, request {request_id}" in caplog.text
    assert "RuntimeError: a bug" in caplog.text


def test_a_subject_no_member_could_have_is_not_a_member(
    client: TestClient, provider: IdentityProvider
) -> None:
    token = provider.token("x" * 300)
    problem(client.get("/me", headers={"Authorization": f"Bearer {token}"}), 403, "not-a-member")


def test_a_where_that_is_not_json_is_refused(client: TestClient, clerk: dict[str, str]) -> None:
    response = client.get("/documents/desk.ticket", params={"where": "{"}, headers=clerk)
    assert problem(response, 422, "rejected")["problems"][0]["path"] == ["where"]


def test_a_body_of_unknown_length_is_refused(client: TestClient, clerk: dict[str, str]) -> None:
    chunks = iter([b'{"code": "T-1", ', b'"subject": "Chunked"}'])
    response = client.post("/commands/desk.open_ticket", content=chunks, headers=clerk)
    problem(response, 411, "length-required")


def test_readiness_reports_a_store_that_is_not_ready(
    client: TestClient, kernel: Kernel, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(kernel.store, "missing", lambda modules: ("not installed",))
    response = client.get("/health/ready")
    assert (response.status_code, response.json()["problems"]) == (503, ["not installed"])

    def unreachable(modules: object) -> tuple[str, ...]:
        raise OSError("connection refused")

    monkeypatch.setattr(kernel.store, "missing", unreachable)
    assert client.get("/health/ready").json()["problems"] == ["the store does not answer: OSError"]


# Discovering a provider's keys.


def verifier_for(handle: Any, clock: Any = time.time) -> Verifier:
    http = httpx2.Client(transport=httpx2.MockTransport(handle))
    return Verifier((Provider(issuer=ISSUER, audience=AUDIENCE),), http=http, clock=clock)


def test_a_provider_reached_at_another_address_is_discovered_there(
    provider: IdentityProvider,
) -> None:
    """As inside a private network, where the issuer's public address does not answer."""

    def inside(request: httpx2.Request) -> httpx2.Response:
        if request.url.host != "identity.internal":
            return httpx2.Response(503)
        if request.url.path == "/.well-known/openid-configuration":
            return httpx2.Response(
                200, json={"issuer": ISSUER, "jwks_uri": "http://identity.internal/keys"}
            )
        return httpx2.Response(200, json=KeySet([KEY]).as_dict(private=False))

    internal = "http://identity.internal/.well-known/openid-configuration"
    http = httpx2.Client(transport=httpx2.MockTransport(inside))
    verifier = Verifier(
        (Provider(issuer=ISSUER, audience=AUDIENCE, discovery=internal),), http=http
    )
    assert verifier.verify(provider.token("ana")).subject == "ana"


def test_a_provider_that_names_another_issuer_is_not_trusted(
    provider: IdentityProvider, caplog: pytest.LogCaptureFixture
) -> None:
    def impostor(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, json={"issuer": "https://evil.example.com", "jwks_uri": "x"})

    with pytest.raises(Unauthenticated, match="cannot be fetched"):
        verifier_for(impostor).verify(provider.token("ana"))
    assert "its discovery document names 'https://evil.example.com'" in caplog.text


@pytest.mark.parametrize("discovery", [{"issuer": ISSUER}, ["not", "an", "object"]])
def test_a_provider_that_publishes_no_keys_signs_no_one_in(
    provider: IdentityProvider, discovery: object
) -> None:
    def keyless(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, json=discovery)

    with pytest.raises(Unauthenticated, match="cannot be fetched"):
        verifier_for(keyless).verify(provider.token("ana"))


def test_keys_are_fetched_again_once_kept_for_an_hour_and_kept_if_that_fails(
    provider: IdentityProvider,
) -> None:
    now = [time.time()]
    verifier = verifier_for(provider.handle, clock=lambda: now[0])
    verifier.verify(provider.token("ana"))
    now[0] += 3600
    provider.down = True
    assert verifier.verify(provider.token("ana", lasts=7200)).subject == "ana"
    provider.down = False
    now[0] += 3600
    verifier.verify(provider.token("ana", lasts=10800))
    assert provider.fetches == 2


def test_a_failure_after_the_response_started_cuts_the_connection(
    caplog: pytest.LogCaptureFixture,
) -> None:
    from remit.server.app import _Hygiene  # pyright: ignore[reportPrivateUsage]

    async def streaming(scope: Any, receive: Any, send: Any) -> None:
        await send({"type": "http.response.start", "status": 200, "headers": []})
        raise RuntimeError("halfway")

    client = TestClient(_Hygiene(streaming, max_body=1024))
    with pytest.raises(RuntimeError, match="halfway"):
        client.get("/")
    assert "GET / failed, request" in caplog.text
