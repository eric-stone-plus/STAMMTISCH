"""Import-graph boundary rules for the shared services package.

Asserted statically over ``services/`` ASTs (the M7 true-merge pin):

(a) ZERO TOLERANCE: nothing under ``services/`` imports ``tui`` or
    ``interface`` — the shared lane is a leaf both front ends consume.
    A reverse edge would recreate exactly the coupling the merge
    dismantled (services wearing a front-end path, screens facade
    pokes, parser copies kept alive to dodge an import ban).
(b) nothing under ``services/`` imports Textual or Rich: UI-free by
    construction is a promise the reviewers can assert, not a hope.
(c) scope: ``services/tests`` is exempt from (a) only for deliberate
    cross-boundary integration assertions (e.g. broker routing through
    the screen that renders it); the shipped package code never is.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import NamedTuple

SERVICES_ROOT = Path(__file__).resolve().parent.parent


class ImportEdge(NamedTuple):
    """One resolved import: file, line, and the dotted module."""

    path: Path
    line: int
    module: str


def _package_of(path: Path) -> list[str]:
    rel = path.resolve().relative_to(SERVICES_ROOT.parent)
    return list(rel.with_suffix("").parts[:-1])


def iter_imports(path: Path) -> list[ImportEdge]:
    """Every import edge in ``path`` (relative imports resolved)."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    parts = _package_of(path)
    edges: list[ImportEdge] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                edges.append(ImportEdge(path, node.lineno, alias.name))
        elif isinstance(node, ast.ImportFrom):
            base = list(parts[: len(parts) - (node.level - 1)]
                        if node.level else [])
            if node.level == 0:
                module = node.module or ""
            else:
                module = ".".join(
                    base + ([node.module] if node.module else []))
            if module:
                edges.append(ImportEdge(path, node.lineno, module))
            for alias in node.names:
                if alias.name != "*":
                    edges.append(ImportEdge(
                        path, node.lineno, f"{module}.{alias.name}"))
    return edges


def _shipped_files() -> list[Path]:
    """Package code under services/, excluding the tests package."""
    return sorted(
        p for p in SERVICES_ROOT.rglob("*.py")
        if "__pycache__" not in p.parts and "tests" not in p.parts
    )


def _is_or_below(module: str, root: str) -> bool:
    return module == root or module.startswith(root + ".")


def test_no_shipped_services_module_imports_a_frontend() -> None:
    """Rule (a): the shared lane never reaches back into a front end."""
    offenders = [
        edge for path in _shipped_files() for edge in iter_imports(path)
        if _is_or_below(edge.module, "tui") or _is_or_below(edge.module, "interface")
    ]
    assert not offenders, (
        "services/ must not import the tui/ or interface/ front ends:\n"
        + "\n".join(
            f"  {e.path.relative_to(SERVICES_ROOT.parent)}:{e.line}: "
            f"imports {e.module!r}" for e in offenders)
    )


def test_no_shipped_services_module_imports_ui_toolkits() -> None:
    """Rule (b): UI-free by construction — no Textual, no Rich."""
    offenders = [
        edge for path in _shipped_files() for edge in iter_imports(path)
        if _is_or_below(edge.module, "textual")
        or _is_or_below(edge.module, "rich")
    ]
    assert not offenders, (
        "services/ must stay UI-toolkit-free:\n"
        + "\n".join(
            f"  {e.path.relative_to(SERVICES_ROOT.parent)}:{e.line}: "
            f"imports {e.module!r}" for e in offenders)
    )


def test_services_tests_import_tui_only_deliberately() -> None:
    """Rule (c): a test may cross the seam, but only under `tui.tests`-
    style integration — flag any `from tui import ...` of a module that
    the merge relocated (a stale absolute path means the move broke a
    test silently and pytest is skipping it, not running it).
    """
    moved = {"intake", "ai_driver", "chart_server", "validated_bars",
             "decide", "timeseries", "screener", "batch_screener",
             "symbols", "subproc", "history", "engine", "config",
             "portfolio", "livefeed", "intake_job", "intake_steward",
             "datafeeds", "brokers"}
    offenders = []
    for path in sorted((SERVICES_ROOT / "tests").glob("*.py")):
        for edge in iter_imports(path):
            if _is_or_below(edge.module, "tui"):
                head = edge.module.split(".")[1] if "." in edge.module else ""
                if head in moved:
                    offenders.append(edge)
    assert not offenders, (
        "services/tests still name a relocated module under tui/:\n"
        + "\n".join(
            f"  {e.path.relative_to(SERVICES_ROOT.parent)}:{e.line}: "
            f"imports {e.module!r}" for e in offenders)
    )
