"""Set Kestrel up in a new deployment, make a year of history, and play out the first
weeks of this month.

    python -m kestrel.seed

Configured from the environment as the server is (REMIT_DATABASE, REMIT_APPLICATION,
REMIT_PROVIDERS), with the one provider being the Kestrel realm in
deploy/kestrel-realm.json, whose people and agents have fixed ids. Runs once: a
deployment that already has Kestrel's roles is left alone.

The setup runs on the first of the month a year ago, on a clock the history moves
forward to the end of last month; this month's first weeks are then played now.
"""

import sys
from datetime import UTC, datetime, time
from time import perf_counter

from remit import access
from remit.kernel import Kernel, PostgresStore
from remit.server import Settings

from . import history, month
from .month import SETUP, Person

PEOPLE = {
    "md": Person(
        "Morgan Hale", access.MemberKind.HUMAN, "MD", "6a0f3c52-8b1e-4d7a-9c25-3e8f71b0d4a1"
    ),
    "tenders": Person(
        "Priya Shah", access.MemberKind.HUMAN, "TENDERS", "b2d84e19-5c3a-47f0-8e61-0a9c2f7d35b8"
    ),
    "logistics": Person(
        "Tom Okafor", access.MemberKind.HUMAN, "LOGISTICS", "d47a1b60-2e9f-4c83-b5d2-7f1e08a64c39"
    ),
    "sourcing": Person(
        "Sourcing agent",
        access.MemberKind.AGENT,
        "SOURCING",
        "e8c35a07-91d4-4b2f-a6e0-5d7b13c8f924",
    ),
    "export_documents": Person(
        "Export documents agent",
        access.MemberKind.AGENT,
        "EXPORT_DOCUMENTS",
        "f1b69d24-3a7c-4e05-9d8b-62c0e4a57f13",
    ),
}
"""Kestrel's people and agents, as the realm knows them: each `subject` is the id of a
realm user, or of an agent client's service account."""


def main() -> None:
    settings = Settings()  # pyright: ignore[reportCallIssue]  filled from the environment
    if len(settings.providers) != 1:
        sys.exit("configure exactly one provider in REMIT_PROVIDERS: the Kestrel realm")
    application = settings.load()
    store = PostgresStore(settings.database)
    try:
        store.install(application.installed)
        today = datetime.now(UTC).date()
        this_month = today.replace(day=1)
        start = this_month.replace(year=this_month.year - 1)
        clock = history.Clock(datetime.combine(start, time(7), tzinfo=UTC))
        kernel = Kernel(
            application.installed, settings=application.settings, store=store, clock=clock
        )
        if kernel.page(access.Role, actor=SETUP, where={"code": "MD"}).items:
            print("Kestrel is set up already; remove the database volume to start again")
            return
        began = perf_counter()
        staff = month.enrol(kernel, settings.providers[0].issuer, PEOPLE)
        month.set_up(kernel, staff, start)
        history.play(kernel, staff, clock, start, this_month, history.FULL)
        clock.now = datetime.now(UTC)
        played = month.play(kernel, staff, today)
    finally:
        store.close()
    print(
        f"Kestrel is set up, with its trading since {start:%-d %B %Y}, in "
        f"{perf_counter() - began:.0f} seconds."
    )
    print(
        f"This month, quotation {played.tender.code} became order {played.order.code}; "
        f"purchase order {played.over_limit.code} waits for the managing director's approval, "
        f"as does quotation {played.thin.code}, and goods are still to arrive on "
        f"{played.part_received.code}."
    )


if __name__ == "__main__":
    main()
