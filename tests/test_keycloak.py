"""Signing in through a real identity provider: the Keycloak of compose.yaml.

    docker compose up -d identity
    REMIT_TEST_KEYCLOAK=http://localhost:8080 ./check

Skipped, and saying so, when no Keycloak is named; failed, when one is named and does
not answer. The realm is deploy/keycloak/remit-realm.json.
"""

import os
from collections.abc import Iterator

import httpx2
import pytest
from conftest import kernel_on
from fastapi.testclient import TestClient

from remit import access
from remit.kernel import Actor, ActorKind, Kernel, Store
from remit.server import Provider, Verifier, create_app

KEYCLOAK = os.environ.get("REMIT_TEST_KEYCLOAK", "")
REALM = f"{KEYCLOAK}/realms/remit"
OWNER_ID = "0b9f6a3e-2c1d-4f5e-8a7b-1c2d3e4f5a6b"
AGENT_ID = "7d3c2b1a-9e8f-4a6b-b5c4-d3e2f1a0b9c8"
SETUP = Actor(id="system:setup", kind=ActorKind.SYSTEM, permissions=frozenset({"*"}))

pytestmark = pytest.mark.skipif(
    not KEYCLOAK, reason="no Keycloak named; set REMIT_TEST_KEYCLOAK to sign in through one"
)


def token(**form: str) -> str:
    response = httpx2.post(f"{REALM}/protocol/openid-connect/token", data=form, timeout=10)
    response.raise_for_status()
    return response.json()["access_token"]


@pytest.fixture
def kernel(store: Store) -> Kernel:
    return kernel_on(store, [access.module])


@pytest.fixture
def client(kernel: Kernel) -> Iterator[TestClient]:
    with httpx2.Client() as http:
        verifier = Verifier((Provider(issuer=REALM, audience="remit"),), http=http)
        with TestClient(create_app(kernel, verifier)) as client:
            yield client


def enrol(kernel: Kernel, subject: str, kind: access.MemberKind) -> None:
    command = access.EnrolMember(name=subject, kind=kind, issuer=REALM, subject=subject)
    kernel.execute(command, actor=SETUP)


def test_a_person_signs_in_with_a_keycloak_token(client: TestClient, kernel: Kernel) -> None:
    enrol(kernel, OWNER_ID, access.MemberKind.HUMAN)
    issued = token(
        grant_type="password", client_id="remit-desktop", username="owner", password="owner"
    )
    me = client.get("/me", headers={"Authorization": f"Bearer {issued}"})
    assert me.status_code == 200, me.text
    assert me.json()["kind"] == "human"


def test_an_agent_signs_in_with_its_client_credentials(client: TestClient, kernel: Kernel) -> None:
    enrol(kernel, AGENT_ID, access.MemberKind.AGENT)
    issued = token(
        grant_type="client_credentials", client_id="remit-agent", client_secret="local-agent-secret"
    )
    me = client.get("/me", headers={"Authorization": f"Bearer {issued}"})
    assert me.status_code == 200, me.text
    assert me.json()["kind"] == "agent"


def test_a_keycloak_token_for_someone_not_enrolled_is_refused(client: TestClient) -> None:
    issued = token(
        grant_type="password", client_id="remit-desktop", username="owner", password="owner"
    )
    assert client.get("/me", headers={"Authorization": f"Bearer {issued}"}).status_code == 403
