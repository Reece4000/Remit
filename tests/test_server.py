"""The server around the API: its configuration, the worker, and the `remit` command."""

import asyncio
import logging
import threading
from collections.abc import Generator
from contextlib import contextmanager
from uuid import UUID

import pytest
from conftest import DSN, ROOT, every, kernel_on

from remit import access, parties
from remit.kernel import (
    Command,
    Context,
    Document,
    Event,
    Kernel,
    Lifecycle,
    Listener,
    MemoryStore,
    Module,
    PostgresStore,
    Store,
)
from remit.server import Application, Settings
from remit.server.cli import main
from remit.server.feed import Commits
from remit.server.worker import work

ISSUER = "https://id.example.com/realms/acme"

post = Module("post")


@post.document
class Letter(Document):
    """A letter posted."""

    lifecycle = Lifecycle(initial="posted")
    to: str


@post.document
class Receipt(Document):
    """That a letter was delivered."""

    lifecycle = Lifecycle(initial="kept")
    letter_id: UUID


@post.event
class Posted(Event):
    """A letter was posted."""

    letter_id: UUID
    to: str


class PostLetter(Command[Letter]):
    to: str


@post.command
def post_letter(command: PostLetter, context: Context) -> Letter:
    """Post a letter."""
    letter = context.create(Letter(to=command.to))
    context.emit(Posted(letter_id=letter.id, to=letter.to))
    return letter


@post.subscribe(Posted)
def deliver(event: Posted, context: Context) -> None:
    """Deliver a posted letter, unless it is addressed nowhere."""
    if event.to == "nowhere":
        raise ValueError("there is no such address")
    context.create(Receipt(letter_id=event.letter_id))


application = Application(modules=(parties.module,))


def building() -> Application:
    return application


not_an_application = object()


# The worker.


def run_worker(kernel: Kernel, **options: float) -> tuple[threading.Event, threading.Thread]:
    stop = threading.Event()
    worker = threading.Thread(target=work, args=(kernel,), kwargs={"stop": stop, **options})
    worker.start()
    return stop, worker


def eventually(condition: object, seconds: float = 10) -> None:
    import time

    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if callable(condition) and condition():
            return
        time.sleep(0.02)
    raise AssertionError("it did not happen in time")


def test_the_worker_delivers_events_as_they_commit(store: Store) -> None:
    kernel = kernel_on(store, [post])
    stop, worker = run_worker(kernel)
    try:
        for number in range(3):
            kernel.execute(PostLetter(to=f"house {number}"), actor=ROOT)
        eventually(lambda: len(every(kernel, Receipt)) == 3)
    finally:
        stop.set()
        worker.join(10)
    assert not worker.is_alive()


def test_the_worker_logs_a_failing_delivery_and_tries_it_again_later(
    store: Store, caplog: pytest.LogCaptureFixture
) -> None:
    kernel = kernel_on(store, [post])
    kernel.execute(PostLetter(to="nowhere"), actor=ROOT)
    with caplog.at_level(logging.ERROR, logger="remit.server.worker"):
        stop, worker = run_worker(kernel, retry=0.05)
        try:
            eventually(lambda: len(caplog.records) >= 2)
        finally:
            stop.set()
            worker.join(10)
    assert "post.deliver failed on event 1: ValueError: there is no such address" in caplog.text
    assert every(kernel, Receipt) == []


# Waking streams when anything commits.


def test_streams_are_woken_by_a_commit(store: Store) -> None:
    kernel = kernel_on(store, [post])

    async def woken() -> bool:
        commits = Commits(kernel.store)
        commits.start(asyncio.get_running_loop())
        try:
            await asyncio.sleep(0.2)  # the listener is up, and has announced itself
            waiting = commits.next()
            await asyncio.to_thread(kernel.execute, PostLetter(to="house"), actor=ROOT)
            await asyncio.wait_for(waiting.wait(), 5)
            return waiting.is_set()
        finally:
            commits.stop()

    assert asyncio.run(woken())


class Flaky(MemoryStore):
    """A store whose first attempt to listen fails, as a dropped connection does."""

    def __init__(self) -> None:
        super().__init__()
        self.attempts = 0

    @contextmanager
    def listen(self) -> Generator[Listener]:
        self.attempts += 1
        if self.attempts == 1:
            raise ConnectionError("the connection was dropped")
        with super().listen() as listener:
            yield listener


def test_listening_again_after_the_store_drops_the_connection(
    caplog: pytest.LogCaptureFixture,
) -> None:
    store = Flaky()

    async def listened() -> int:
        commits = Commits(store, reconnect=0.01)
        commits.start(asyncio.get_running_loop())
        try:
            await asyncio.wait_for(commits.next().wait(), 5)
        finally:
            commits.stop()
        return store.attempts

    with caplog.at_level(logging.ERROR, logger="remit.server.feed"):
        assert asyncio.run(listened()) == 2
    assert "listening again" in caplog.text


# Configuration.


def settings(**given: object) -> Settings:
    defaults: dict[str, object] = {"database": DSN, "application": "test_server:application"}
    return Settings.model_validate({**defaults, **given})


def test_an_application_is_named_by_where_it_is_defined() -> None:
    assert settings().load() is application
    assert settings(application="test_server:building").load() is application
    with pytest.raises(Exception, match="is not an Application"):
        settings(application="test_server:not_an_application").load()


def test_an_application_always_installs_access() -> None:
    assert application.installed == (access.module, parties.module)


def test_providers_on_plain_http_are_refused_unless_allowed() -> None:
    local = [{"issuer": "http://localhost:8080/realms/dev", "audience": "remit"}]
    with pytest.raises(ValueError, match="plain http"):
        settings(providers=local)
    assert (
        settings(providers=local, insecure_providers=True).providers[0].issuer.startswith("http:")
    )


def test_settings_are_read_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("REMIT_DATABASE", DSN)
    monkeypatch.setenv("REMIT_APPLICATION", "test_server:application")
    monkeypatch.setenv("REMIT_PROVIDERS", f'[{{"issuer": "{ISSUER}", "audience": "remit"}}]')
    read = Settings()  # pyright: ignore[reportCallIssue]
    assert read.providers[0].issuer == ISSUER
    assert read.connections == 10


# The `remit` command.


@pytest.fixture
def environment(monkeypatch: pytest.MonkeyPatch, postgres: PostgresStore) -> None:
    monkeypatch.setenv("REMIT_DATABASE", DSN)
    monkeypatch.setenv("REMIT_APPLICATION", "test_server:application")
    monkeypatch.setenv("REMIT_PROVIDERS", f'[{{"issuer": "{ISSUER}", "audience": "remit"}}]')


@pytest.mark.usefixtures("environment")
def test_install_then_enrol_the_first_member(
    postgres: PostgresStore, capsys: pytest.CaptureFixture[str]
) -> None:
    main(["install"])
    assert "installed access, parties" in capsys.readouterr().out

    main(["enrol", "Ana Lima", "--subject", "ana"])
    assert "holding role OWNER" in capsys.readouterr().out
    main(["enrol", "Payables", "--subject", "payables", "--agent"])

    kernel = application.kernel(postgres)
    ana = kernel.ask(access.SignIn(issuer=ISSUER, subject="ana"), actor=ROOT)
    agent = kernel.ask(access.SignIn(issuer=ISSUER, subject="payables"), actor=ROOT)
    assert ana is not None
    assert ana.permissions == {"*"}
    assert agent is not None
    assert agent.kind.value == "agent"
    assert len(every(kernel, access.Role)) == 1

    with pytest.raises(SystemExit, match="already in use together"):
        main(["enrol", "Ana again", "--subject", "ana"])


@pytest.mark.usefixtures("environment")
def test_enrol_needs_an_issuer_when_more_than_one_is_trusted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    main(["install"])
    two = (
        f'[{{"issuer": "{ISSUER}", "audience": "a"}}, {{"issuer": "{ISSUER}/2", "audience": "b"}}]'
    )
    monkeypatch.setenv("REMIT_PROVIDERS", two)
    with pytest.raises(SystemExit, match="name the --issuer"):
        main(["enrol", "Ana", "--subject", "ana"])
    main(["enrol", "Ana", "--subject", "ana", "--issuer", f"{ISSUER}/2"])


@pytest.mark.usefixtures("environment")
def test_serve_refuses_to_start_trusting_no_one(monkeypatch: pytest.MonkeyPatch) -> None:
    main(["install"])
    monkeypatch.setenv("REMIT_PROVIDERS", "[]")
    with pytest.raises(SystemExit, match="trusts no identity provider"):
        main(["serve"])


def test_the_standard_application_keeps_its_books_in_the_currency_given(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from remit import ledger
    from remit.server.standard import application as standard

    monkeypatch.delenv("REMIT_BASE_CURRENCY", raising=False)
    with pytest.raises(ValueError, match="set REMIT_BASE_CURRENCY"):
        standard()
    monkeypatch.setenv("REMIT_BASE_CURRENCY", "EUR")
    built = standard()
    assert ledger.module in built.modules
    assert built.settings == (ledger.LedgerSettings(base_currency="EUR"),)


def test_remit_runs_as_a_module(monkeypatch: pytest.MonkeyPatch) -> None:
    import runpy
    import sys

    monkeypatch.setattr(sys, "argv", ["remit", "--help"])
    with pytest.raises(SystemExit) as exited:
        runpy.run_module("remit.server", run_name="__main__")
    assert exited.value.code == 0


@pytest.mark.usefixtures("environment")
def test_serve_runs_the_api_on_the_port_given(monkeypatch: pytest.MonkeyPatch) -> None:
    import uvicorn
    from fastapi import FastAPI

    main(["install"])
    served: dict[str, object] = {}
    monkeypatch.setattr(uvicorn, "run", lambda app, **options: served.update(app=app, **options))
    main(["serve", "--port", "9001"])
    assert isinstance(served["app"], FastAPI)
    assert served["port"] == 9001


@pytest.mark.usefixtures("environment")
def test_the_worker_stops_on_a_signal(monkeypatch: pytest.MonkeyPatch) -> None:
    import signal

    from remit.server import cli

    main(["install"])
    handlers: dict[int, object] = {}
    monkeypatch.setattr(
        signal, "signal", lambda number, handler: handlers.update({number: handler})
    )

    def working(kernel: Kernel, *, stop: threading.Event) -> None:
        handler = handlers[signal.SIGTERM]
        assert callable(handler)
        handler(signal.SIGTERM, None)
        assert stop.is_set()

    monkeypatch.setattr(cli, "work", working)
    main(["worker"])
    assert set(handlers) == {signal.SIGTERM, signal.SIGINT}
