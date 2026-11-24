"""`remit`: install, serve, work, and enrol the first member.

    remit install                 install the application's modules into the database
    remit serve [--port 8000]     serve the API
    remit worker                  deliver events to subscriptions
    remit enrol NAME --subject S  enrol a member holding every permission; for the
               [--issuer I]       first one, who then enrols everyone else through
               [--agent]          the API

Each reads its configuration from the environment (see `settings`). `serve` and
`worker` stop cleanly on SIGTERM, as a container is stopped.
"""

import argparse
import logging
import signal
import sys
import threading
from collections.abc import Sequence
from types import FrameType

import httpx2
import uvicorn

from remit import access
from remit.kernel import Actor, ActorKind, Kernel, PostgresStore, Rejected

from .app import create_app
from .auth import Verifier
from .settings import Settings
from .worker import work

SETUP = Actor(id="system:setup", kind=ActorKind.SYSTEM, permissions=frozenset({"*"}))
"""Who `enrol` acts as. Anyone who can run it can reach the database already."""

OWNER = "OWNER"
"""The code of the role `enrol` grants: every permission."""


def main(arguments: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="remit", description="Run a Remit deployment.")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("install", help="install the application's modules")
    serving = commands.add_parser("serve", help="serve the API")
    serving.add_argument("--host", default="0.0.0.0")  # noqa: S104  a container listens on all
    serving.add_argument("--port", type=int, default=8000)
    commands.add_parser("worker", help="deliver events to subscriptions")
    enrolling = commands.add_parser("enrol", help="enrol a member with every permission")
    enrolling.add_argument("name")
    enrolling.add_argument("--subject", required=True, help="the `sub` of their tokens")
    enrolling.add_argument("--issuer", help="the provider; the only one configured if left out")
    enrolling.add_argument("--agent", action="store_true", help="an agent, not a person")
    chosen = parser.parse_args(arguments)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    settings = Settings()  # pyright: ignore[reportCallIssue]  filled from the environment
    application = settings.load()
    store = PostgresStore(settings.database, connections=settings.connections)
    try:
        if chosen.command == "install":
            store.install(application.installed)
            print(f"installed {', '.join(m.name for m in application.installed)}")
        elif chosen.command == "serve":
            _serve(settings, application.kernel(store), chosen.host, chosen.port)
        elif chosen.command == "worker":
            stop = threading.Event()

            def stopping(signal_number: int, frame: FrameType | None) -> None:
                stop.set()

            signal.signal(signal.SIGTERM, stopping)
            signal.signal(signal.SIGINT, stopping)
            work(application.kernel(store), stop=stop)
        else:
            _enrol(settings, application.kernel(store), chosen)
    finally:
        store.close()


def _serve(settings: Settings, kernel: Kernel, host: str, port: int) -> None:
    if not settings.providers:
        sys.exit(
            "set REMIT_PROVIDERS: the API trusts no identity provider, so no one could sign in"
        )
    with httpx2.Client() as http:
        app = create_app(kernel, Verifier(settings.providers, http=http))
        uvicorn.run(app, host=host, port=port, proxy_headers=True, log_config=None)


def _enrol(settings: Settings, kernel: Kernel, chosen: argparse.Namespace) -> None:
    issuer: str | None = chosen.issuer
    if issuer is None:
        if len(settings.providers) != 1:
            sys.exit("name the --issuer: more than one provider, or none, is configured")
        issuer = settings.providers[0].issuer
    owners = kernel.page(access.Role, actor=SETUP, where={"code": OWNER}).items
    owner = (
        owners[0]
        if owners
        else kernel.execute(
            access.DefineRole(code=OWNER, name="Owner", permissions=("*",)), actor=SETUP
        ).result
    )
    kind = access.MemberKind.AGENT if chosen.agent else access.MemberKind.HUMAN
    command = access.EnrolMember(
        name=chosen.name, kind=kind, issuer=issuer, subject=chosen.subject, roles=(owner.id,)
    )
    try:
        member = kernel.execute(command, actor=SETUP).result
    except Rejected as refusal:
        sys.exit(str(refusal))
    print(f"enrolled {member.name} as {member.id}, holding role {OWNER}")
