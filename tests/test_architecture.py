"""The layering rules, enforced.

The kernel knows no module. Each module is a package that exposes its `Module` as
`module`, and imports only the kernel's public API and the public API (the top-level
package) of the modules it declares in `requires`. Standard modules get no access that a
third party's module would not have.
"""

import ast
import importlib
from collections.abc import Iterator
from pathlib import Path

import pytest

import remit
from remit.kernel import Module

ROOT = Path(remit.__file__).parent
MODULES = sorted(
    path.name
    for path in ROOT.iterdir()
    if path.is_dir() and path.name not in {"kernel", "__pycache__"}
)


def framework_imports(package: str) -> Iterator[tuple[str, str]]:
    """Yield (file, imported name) for every import of framework code within a package."""
    for path in sorted((ROOT / package).rglob("*.py")):
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.ImportFrom) and node.level > 1:
                yield path.name, "." * node.level + (node.module or "")
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module == "remit":
                yield from ((path.name, f"remit.{alias.name}") for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                if node.module.split(".")[0] == "remit":
                    yield path.name, node.module
            elif isinstance(node, ast.Import):
                yield from (
                    (path.name, alias.name)
                    for alias in node.names
                    if alias.name.split(".")[0] == "remit"
                )


def declared(package: str) -> Module:
    module = getattr(importlib.import_module(f"remit.{package}"), "module", None)
    assert isinstance(module, Module), f"remit.{package} must expose its Module as `module`"
    assert module.name == package, f"remit.{package} declares a module named {module.name!r}"
    return module


def test_the_kernel_imports_no_module() -> None:
    assert list(framework_imports("kernel")) == []


def test_there_are_modules_beside_the_kernel() -> None:
    assert "ledger" in MODULES


@pytest.mark.parametrize("package", MODULES)
def test_a_module_imports_only_the_kernel_and_what_it_requires(package: str) -> None:
    module = declared(package)
    allowed = {"remit.kernel"} | {f"remit.{dependency.name}" for dependency in module.requires}
    beyond = [found for found in framework_imports(package) if found[1] not in allowed]
    assert beyond == [], f"{package} reaches past its declared requirements: {beyond}"


@pytest.mark.parametrize("package", MODULES)
def test_a_standard_module_requires_only_standard_modules(package: str) -> None:
    for dependency in declared(package).requires:
        assert dependency is declared(dependency.name)
