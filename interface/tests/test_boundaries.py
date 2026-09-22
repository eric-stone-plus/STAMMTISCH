"""Import-graph boundary rules, asserted statically over interface/ ASTs.

Encodes the README "Boundary rule" (plus the M3/M4 sanctioned seams) so
a violation fails loudly with the offending file:line:

(a) ZERO TOLERANCE: nothing under ``interface/`` imports the old
    ``tui/`` tree. The old package stays live until its M5 retirement,
    but this package must never reach into it statically.
(b) ``interface/services/`` is the service lane: it imports the
    snapshot contract, the stdlib, and — since the M7 true merge —
    the SHARED top-level ``services`` package (the one real
    implementation behind the reconciled quant-engine / feeds /
    feeds-health lanes); never ``interface.app`` /
    ``interface.screens`` / ``interface.render`` (reverse dependency)
    and never ``interface.collectors`` (collectors consume services,
    not the other way) nor Textual (no UI types in service code).
(c) UI modules — ``interface/screens/`` and ``interface/app/spine.py``
    — import only the sanctioned interface-internal set:
    ``snapshot``, ``render``, ``app.verbs``, ``app.router``,
    ``app.spine``, and ``services`` (the M3 workbench seam; today only
    workbench.py uses it). Composition roots (``app/shell.py``,
    ``status.py``, ``watch.py``) are exempt — building the provider is
    their job. Forbidden by this rule: ``collectors``, ``args``,
    ``status``, ``watch``, ``app.shell``.
(d) ``render/panels.py`` renders snapshots with Rich only — zero
    Textual imports at any depth (the render layer must stay usable
    from the stdlib/watch tiers).
(e) M6 absorbed screens reach their services by INJECTION ONLY: the
    five screen modules (``feeds_health``, ``ledger``, ``energy``,
    ``polymarket``, ``sentiment``) contain ZERO import-time
    ``interface.services`` imports — neither directly in the module
    body nor under a top-level ``if``/``try`` guard (both execute at
    import time; ``if TYPE_CHECKING`` bodies never do) — the transports
    resolve lazily inside function bodies (call time), so importing a
    screen can never drag a network/file transport onto the import
    path. (The M3 workbench seam stays exempt: ``resolve_engine`` is
    pure dispatch, not a transport.)
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import NamedTuple

INTERFACE_ROOT = Path(__file__).resolve().parent.parent

#: Rule (c): the only interface-internal modules UI code may import.
#: A prefix entry covers its submodules (e.g. ``interface.render.tokens``).
UI_ALLOWED_INTERFACE = (
    "interface.snapshot",
    "interface.render",
    "interface.app.verbs",
    "interface.app.router",
    "interface.app.spine",
    "interface.services",
)

#: Rule (b): reverse dependencies forbidden from the service lane.
SERVICES_FORBIDDEN_INTERFACE = (
    "interface.app",
    "interface.screens",
    "interface.render",
    "interface.collectors",
)

#: Rule (e): the M6 absorbed screens — injection-only service seams.
M6_SCREENS = (
    "feeds_health",
    "ledger",
    "energy",
    "polymarket",
    "sentiment",
)


class ImportEdge(NamedTuple):
    """One resolved import: file, line, and the dotted module."""

    path: Path
    line: int
    module: str


def _package_of(path: Path) -> list[str]:
    """Dotted package parts of ``path`` relative to the repo root."""
    rel = path.resolve().relative_to(INTERFACE_ROOT.parent)
    return list(rel.with_suffix("").parts[:-1])


def iter_imports(path: Path) -> list[ImportEdge]:
    """Every import edge in ``path`` (relative imports resolved)."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    parts = _package_of(path)  # e.g. ["interface", "collectors"]
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
            # ``from X import name`` — the alias itself may be a module.
            for alias in node.names:
                if alias.name != "*":
                    edges.append(ImportEdge(
                        path, node.lineno, f"{module}.{alias.name}"))
    return edges


def _interface_files(*subdirs: str) -> list[Path]:
    if not subdirs:
        return sorted(p for p in INTERFACE_ROOT.rglob("*.py"))
    out: list[Path] = []
    for sub in subdirs:
        out.extend(sorted((INTERFACE_ROOT / sub).rglob("*.py")))
    return out


def _is_or_below(module: str, root: str) -> bool:
    """True when ``module`` is ``root`` itself or any submodule of it."""
    return module == root or module.startswith(root + ".")


def _rule_prefixes(module: str, prefixes: tuple[str, ...]) -> bool:
    return module == "interface" or any(
        _is_or_below(module, p) for p in prefixes)


def _report(edges: list[ImportEdge]) -> str:
    return "\n".join(
        f"  {e.path.relative_to(INTERFACE_ROOT.parent)}:{e.line}: "
        f"imports {e.module!r}"
        for e in edges
    )


def test_no_interface_module_imports_tui() -> None:
    """Rule (a): zero static imports of the old tree, anywhere."""
    offenders = [
        edge for edge in
        (e for path in _interface_files() for e in iter_imports(path))
        if _is_or_below(edge.module, "tui")
    ]
    assert not offenders, (
        "interface/ must not import the old tui/ tree:\n" + _report(offenders))


def test_m6_services_are_copy_extractions_not_moves() -> None:
    """Named regression for the M6 absorption wave: the five new service
    modules (feeds_health / ledger / energy / polymarket / sentiment)
    are copy+adapt extractions — they (like everything under
    ``interface/``) carry zero ``tui/`` imports and no Textual, keeping
    the old tree live-but-unreferenced (RETIREMENT Option B)."""
    for name in ("feeds_health", "ledger", "energy", "polymarket",
                 "sentiment"):
        path = INTERFACE_ROOT / "services" / f"{name}.py"
        assert path.is_file(), f"missing M6 service module: {path}"
        for edge in iter_imports(path):
            assert not _is_or_below(edge.module, "tui"), (
                f"{path.name} must not import the old tui/ tree:\n"
                f"  {path}:{edge.line}: imports {edge.module!r}")
            assert not _is_or_below(edge.module, "textual"), (
                f"{path.name} must stay UI-free:\n"
                f"  {path}:{edge.line}: imports {edge.module!r}")


def test_services_import_no_ui_and_no_textual() -> None:
    """Rule (b): the service lane never depends back on the UI/collector
    layers and never imports Textual."""
    offenders = []
    for path in _interface_files("services"):
        for edge in iter_imports(path):
            breaks_reverse_dep = _rule_prefixes(
                edge.module, SERVICES_FORBIDDEN_INTERFACE)
            if breaks_reverse_dep or _is_or_below(edge.module, "textual"):
                offenders.append(edge)
    assert not offenders, (
        "services/ reverse-dependency / Textual violations:\n"
        + _report(offenders))


def test_screens_and_spine_import_only_sanctioned_modules() -> None:
    """Rule (c): UI modules touch only snapshot/render/verbs/router/spine
    (+ the services workbench seam) — never collectors/args/tiers/shell."""
    offenders = []
    paths = [INTERFACE_ROOT / "app" / "spine.py",
             *_interface_files("screens")]
    for path in paths:
        for edge in iter_imports(path):
            if edge.module.startswith("interface.") and not _rule_prefixes(
                    edge.module, UI_ALLOWED_INTERFACE):
                offenders.append(edge)
    assert not offenders, (
        "screens/ + app/spine.py boundary violations (allowed: "
        + ", ".join(UI_ALLOWED_INTERFACE) + "):\n" + _report(offenders))


def test_render_panels_has_zero_textual_imports() -> None:
    """Rule (d): render/panels.py is snapshot→Rich only."""
    edges = iter_imports(INTERFACE_ROOT / "render" / "panels.py")
    offenders = [edge for edge in edges
                 if _is_or_below(edge.module, "textual")]
    assert not offenders, (
        "render/panels.py must not import Textual:\n" + _report(offenders))


def _is_type_checking_guard(test: ast.expr) -> bool:
    """True for ``if TYPE_CHECKING:`` / ``if typing.TYPE_CHECKING:``."""
    if isinstance(test, ast.Name):
        return test.id == "TYPE_CHECKING"
    if isinstance(test, ast.Attribute):
        return test.attr == "TYPE_CHECKING"
    return False


def _import_time_services_imports(tree: ast.Module,
                                  parts: list[str]) -> list[tuple[int, str]]:
    """``interface.services`` imports that execute at MODULE IMPORT time.

    Everything at module scope runs when the module is imported — the
    direct body AND the bodies of top-level ``if``/``try`` statements
    (a guarded import still executes at import time; D-M6 finding 2
    caught the old walker letting those escape). The sanctioned
    non-importing scopes are skipped: function/class bodies (the lazy
    call-time seam) and ``if TYPE_CHECKING`` bodies (never executed at
    runtime — though an ``else`` branch under one still is, and is
    walked). Relative imports are resolved with the same rule
    :func:`iter_imports` uses. Returns ``(line, dotted_module)`` pairs.
    """
    offenders: list[tuple[int, str]] = []

    def _targets(node: ast.Import | ast.ImportFrom) -> list[str]:
        if isinstance(node, ast.Import):
            return [alias.name for alias in node.names]
        if node.level == 0:
            module = node.module or ""
        else:
            base = parts[: len(parts) - (node.level - 1)]
            module = ".".join(base + ([node.module] if node.module else []))
        names = [module] if module else []
        names += [f"{module}.{alias.name}" for alias in node.names
                  if alias.name != "*"]
        return names

    def _walk(stmts: list[ast.stmt]) -> None:
        for node in stmts:
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                offenders.extend(
                    (node.lineno, target)
                    for target in _targets(node)
                    if _is_or_below(target, "interface.services"))
            elif isinstance(node, ast.If):
                if not _is_type_checking_guard(node.test):
                    _walk(node.body)
                _walk(node.orelse)
            elif isinstance(node, ast.Try):
                _walk(node.body)
                for handler in node.handlers:
                    _walk(handler.body)
                _walk(node.orelse)
                _walk(node.finalbody)

    _walk(tree.body)
    return offenders


def test_m6_screens_import_services_by_injection_only() -> None:
    """Rule (e): the absorbed screens carry ZERO import-time services
    imports — module-scope imports run at import time whether they sit
    directly in the body or under a top-level ``if``/``try`` guard (only
    ``if TYPE_CHECKING`` bodies never run). The sanctioned seams are
    injection and function-body (lazy, call-time) imports, so importing
    a screen can never pull a network/file transport onto the path."""
    offenders: list[str] = []
    for name in M6_SCREENS:
        path = INTERFACE_ROOT / "screens" / f"{name}.py"
        tree = ast.parse(path.read_text(encoding="utf-8"))
        offenders.extend(
            f"{path.relative_to(INTERFACE_ROOT.parent)}:{line}: "
            f"imports {module!r}"
            for line, module in _import_time_services_imports(
                tree, _package_of(path)))
    assert offenders == [], (
        "M6 screens must reach services by injection/lazy import only "
        "(no module-level interface.services imports, guarded or not):\n  "
        + "\n  ".join(offenders))


def test_rule_e_walker_flags_guarded_module_level_imports() -> None:
    """Negative self-check for the rule (e) walker (D-M6 finding 2): the
    old walker inspected only DIRECT ``tree.body`` children, so a banned
    import under a top-level ``try`` or non-TYPE_CHECKING ``if`` escaped.
    A synthetic module (parsed in-test; nothing violating is written
    into the tree) MUST flag the guarded imports and MUST NOT flag the
    TYPE_CHECKING guard or the function-body lazy seam."""
    synthetic = ast.parse(
        "import os\n"
        "if TYPE_CHECKING:\n"
        "    from interface.services.energy import SeriesRow\n"
        "try:\n"
        "    import interface.services.polymarket as _pm\n"
        "except ImportError:\n"
        "    _pm = None\n"
        "if os.name == 'nt':\n"
        "    from interface.services import energy as _energy\n"
        "def lazy():\n"
        "    from interface.services.polymarket import fetch_markets\n"
    )
    flagged = _import_time_services_imports(synthetic,
                                            ["interface", "screens"])
    modules = [module for _, module in flagged]
    assert "interface.services.polymarket" in modules, (
        "a try-guarded module-level services import must be flagged")
    assert "interface.services" in modules and (
        "interface.services.energy" in modules), (
        "an if-guarded module-level services import must be flagged")
    assert len(flagged) == 3, f"only the two guarded lines: {flagged!r}"
    linenos = {line for line, _ in flagged}
    assert 2 not in linenos, "the TYPE_CHECKING guard never runs"
    assert 10 not in linenos, "the function body is the sanctioned seam"
