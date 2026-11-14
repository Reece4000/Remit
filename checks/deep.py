"""Proof that the tests can fail. Run by `./check --deep`.

A passing suite says little unless breaking the code makes it fail. Each mutation here
swaps one piece of the framework for a broken version, in this process only, and runs
the tests that are supposed to guard it. A mutation that survives means those tests
would not notice a real bug of that kind.

It also repeats the concurrency tests, because a race that passes once proves nothing.
"""

import contextlib
import os
import sys
from collections.abc import Callable, Sequence
from contextlib import AbstractContextManager
from dataclasses import dataclass, replace
from types import SimpleNamespace
from typing import Any
from unittest import mock

import pytest
from hypothesis import Phase, settings
from psycopg import Connection, IsolationLevel

import remit.kernel.postgres as postgres
import remit.ledger.books as books
import remit.ledger.commands as commands
from remit.kernel import Document, Money

KERNEL = ["tests/test_kernel.py"]
LEDGER = ["tests/test_ledger.py", "tests/test_ledger_properties.py"]
RACES = ["tests/test_postgres.py"]
RACE_RUNS = 20

real_movements = books.movements


@dataclass(frozen=True)
class Mutation:
    breaks: str
    guarded_by: Sequence[str]
    apply: Callable[[], AbstractContextManager[Any]]


def swap(target: object, name: str, broken: object) -> Callable[[], AbstractContextManager[Any]]:
    return lambda: mock.patch.object(target, name, broken)


def read_committed(connection: Connection[Any]) -> None:
    connection.isolation_level = IsolationLevel.READ_COMMITTED


def nothing(*_: object, **__: object) -> None:
    return None


MUTATIONS = [
    Mutation(
        "final documents can be changed",
        KERNEL,
        swap(Document, "is_final", property(lambda _: False)),
    ),
    Mutation(
        "postings are not validated",
        LEDGER,
        swap(commands, "_require_valid", nothing),
    ),
    Mutation(
        "closed periods accept postings",
        LEDGER,
        swap(commands, "closed_through", nothing),
    ),
    Mutation(
        "accounts with a balance can be archived",
        LEDGER,
        swap(
            commands,
            "balance",
            lambda *_, **__: SimpleNamespace(is_zero=True, base=Money.zero("GBP")),
        ),
    ),
    Mutation(
        "journal entries never reach pgledger",
        LEDGER,
        swap(commands, "record", nothing),
    ),
    Mutation(
        "movements go the wrong way",
        LEDGER,
        swap(
            books,
            "movements",
            lambda postings, base: [
                replace(move, source=move.target, target=move.source)
                for move in real_movements(postings, base)
            ],
        ),
    ),
    Mutation(
        "one movement of an entry is lost",
        LEDGER,
        swap(books, "movements", lambda postings, base: real_movements(postings, base)[:-1]),
    ),
    Mutation(
        "foreign amounts are not recorded",
        LEDGER,
        swap(
            books,
            "movements",
            lambda postings, base: [
                move for move in real_movements(postings, base) if move.source[1] == base
            ],
        ),
    ),
    Mutation(
        "units of work are not serialisable",
        RACES,
        swap(postgres, "_serialisable", read_committed),
    ),
]


def passes(tests: Sequence[str]) -> bool:
    """Run tests quietly in this process, stopping at the first failure."""
    arguments = ["-q", "-x", "--tb=no", "--no-header", "-p", "no:cacheprovider", *tests]
    with open(os.devnull, "w") as quiet, contextlib.redirect_stdout(quiet):
        return pytest.main(arguments) == 0


def main() -> int:
    # Mutated runs must not save failing examples for later replay, and need not shrink them.
    settings.register_profile("deep", database=None, phases=[Phase.explicit, Phase.generate])
    settings.load_profile("deep")

    if not passes([*KERNEL, *LEDGER, *RACES]):
        print("FAILED    the tests do not pass before anything is broken; run ./check first")
        return 1
    print("ok        the tests pass before anything is broken")

    survived = 0
    for mutation in MUTATIONS:
        with mutation.apply():
            caught = not passes(mutation.guarded_by)
        survived += not caught
        print(f"{'caught  ' if caught else 'SURVIVED'}  {mutation.breaks}")

    failed_runs = sum(not passes(RACES) for _ in range(RACE_RUNS))
    print(
        f"{'FAILED' if failed_runs else 'ok    '}    "
        f"concurrency tests: {RACE_RUNS - failed_runs} of {RACE_RUNS} runs passed"
    )
    return 1 if survived or failed_runs else 0


if __name__ == "__main__":
    sys.exit(main())
