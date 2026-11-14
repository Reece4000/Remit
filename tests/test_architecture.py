"""The layering rules, enforced: the kernel knows no module, and modules use only its public API."""

import ast
from collections.abc import Iterator
from pathlib import Path

import remit

ROOT = Path(remit.__file__).parent
MODULES = sorted(path.name for path in ROOT.iterdir() if path.is_dir() and path.name != "kernel")


def framework_imports(package: str) -> Iterator[tuple[str, str]]:
    """Yield (file, imported name) for every import of framework code within a package."""
    for path in sorted((ROOT / package).rglob("*.py")):
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.ImportFrom) and node.level > 1:
                yield path.name, "." * node.level + (node.module or "")
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                if node.module.split(".")[0] == "remit":
                    yield path.name, node.module
            elif isinstance(node, ast.Import):
                yield from (
                    (path.name, alias.name)
                    for alias in node.names
                    if alias.name.split(".")[0] == "remit"
                )


def test_the_kernel_imports_no_module() -> None:
    assert list(framework_imports("kernel")) == []


def test_modules_import_only_the_public_kernel_api() -> None:
    assert MODULES, "expected at least one module beside the kernel"
    for module in MODULES:
        deeper = [found for found in framework_imports(module) if found[1] != "remit.kernel"]
        assert deeper == [], f"{module} reaches past remit.kernel: {deeper}"
