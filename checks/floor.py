"""Refuse a coverage floor lower than the one on main. Run by `./check`.

The floor may rise as gaps are closed, but never fall. Any error here fails the gate:
a check that cannot run must not pass.
"""

import subprocess
import sys
import tomllib
from pathlib import Path
from typing import Any


def floor(pyproject: dict[str, Any]) -> float | None:
    return pyproject.get("tool", {}).get("coverage", {}).get("report", {}).get("fail_under")


def main() -> int:
    here = floor(tomllib.loads(Path("pyproject.toml").read_text()))
    if here is None:
        print("pyproject.toml sets no coverage floor", file=sys.stderr)
        return 1
    for ref in ("main", "origin/main"):
        shown = subprocess.run(  # noqa: S603  the arguments are fixed, never user input
            ["git", "show", f"{ref}:pyproject.toml"],  # noqa: S607  git from PATH
            capture_output=True,
            text=True,
            check=False,
        )
        if shown.returncode == 0:
            break
    else:
        print("no main or origin/main to compare the coverage floor with", file=sys.stderr)
        return 1
    before = floor(tomllib.loads(shown.stdout))
    if before is not None and here < before:
        print(f"the coverage floor was lowered from {before} to {here}", file=sys.stderr)
        return 1
    print(f"coverage floor {here}" + (f" (main: {before})" if before is not None else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
