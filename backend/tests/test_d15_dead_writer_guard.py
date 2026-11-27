"""
§45.D-15 / D15-1-B — the dead sixth boundary writer stays dead.
===============================================================

``app/timesheets/time_tracking.py`` defines a real FastAPI router
(``prefix="/time"``) with ``POST /clock-in/{user_id}`` and
``POST /clock-out/{user_id}``. It writes BOTH session boundaries and records no
provenance, and its ``_resolve_target`` lets an admin, manager, HR user or
owner act on any user in their organization.

It is currently unreachable: nothing imports it, ``main.py`` mounts 38 routers
and none of them is this one, there is no dynamic router discovery, the package
has no ``__init__.py``, and no caller exists anywhere in the repository
including the frontend. Owner ruling D15-1-B therefore keeps it out of scope —
no provenance, no runtime change, not mounted, not deleted.

WHY A GUARD AND NOT JUST A SENTENCE
-----------------------------------
Mounting it is a one-line change in ``main.py``. Without this test that change
would put two unattributed boundary writers into production with no failing
test, no type error and no reviewer signal — the exact silent-bypass failure
§45.D-15 exists to prevent. The repository already carries a stale note calling
this file "a clock-out writer"
(``tests/test_phase4_regressions.py``), which shows that prose about this module
does not survive contact with later work.

This guard is static. It adds no runtime behaviour, monkeypatches nothing, and
does not modify the dead module.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

BACKEND = pathlib.Path(__file__).resolve().parent.parent
APP = BACKEND / "app"
MAIN = BACKEND / "main.py"

DEAD_PACKAGE = "app.timesheets"
DEAD_MODULE = "app.timesheets.time_tracking"
DEAD_FILE = APP / "timesheets" / "time_tracking.py"

#: The failure text. Both fragments are asserted below so the message cannot be
#: watered down into something a future reader would not act on.
_WHY = (
    "§45.D-15 (owner decision D15-1-B): app/timesheets/time_tracking.py is an "
    "UNMOUNTED clock-in/clock-out writer that records NO session-boundary "
    "provenance. Importing or mounting it would put an unattributed boundary "
    "writer into production. Mounting it requires a D-15 compliance review "
    "first: it must either append clock_session_boundary_event rows for both "
    "boundaries, or the owner must rule it out of scope again. Do not silence "
    "this test to ship a mount."
)


def _production_files():
    """Every production Python file, excluding the dead package itself."""
    files = [MAIN]
    for path in sorted(APP.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        if path.is_relative_to(APP / "timesheets"):
            continue
        files.append(path)
    return files


def _imports_of(tree: ast.AST):
    """Every module name this file imports, absolute form where determinable."""
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                found.add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                # `from .timesheets import x` inside app/ resolves to
                # app.timesheets; record both spellings.
                found.add(node.module)
                if node.level:
                    found.add(f"app.{node.module}")
            for alias in node.names:
                if node.module:
                    found.add(f"{node.module}.{alias.name}")
    return found


def _code_string_literals(tree: ast.AST):
    """Every string literal that is CODE, not a docstring.

    Docstrings are excluded so a module may explain why the dead writer is out
    of scope without tripping the guard against itself.
    """
    docstrings = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef,
                             ast.AsyncFunctionDef)):
            body = getattr(node, "body", None)
            if body and isinstance(body[0], ast.Expr) \
                    and isinstance(body[0].value, ast.Constant) \
                    and isinstance(body[0].value.value, str):
                docstrings.add(id(body[0].value))
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str) \
                and id(node) not in docstrings:
            yield node.value


def _references_the_dead_package(name: str) -> bool:
    return name == DEAD_PACKAGE or name.startswith(DEAD_PACKAGE + ".") \
        or name == "timesheets" or name.startswith("timesheets.")


class TestNothingImportsTheDeadWriter:
    def test_main_does_not_import_it(self):
        tree = ast.parse(MAIN.read_text(encoding="utf-8"))
        offenders = sorted(n for n in _imports_of(tree)
                           if _references_the_dead_package(n))
        assert offenders == [], f"{MAIN.name} imports {offenders}. {_WHY}"

    def test_no_production_module_imports_it(self):
        offenders = []
        for path in _production_files():
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for name in _imports_of(tree):
                if _references_the_dead_package(name):
                    offenders.append(
                        f"{path.relative_to(BACKEND).as_posix()} -> {name}")
        assert offenders == [], f"{offenders}. {_WHY}"

    def test_no_module_reaches_it_dynamically(self):
        """`importlib.import_module("app.timesheets.time_tracking")` would slip
        past an import-statement scan, so string literals are checked too.

        Docstrings and comments are excluded deliberately: a module is allowed
        to DISCUSS the dead writer — this package's own docstring explains why
        it is out of scope — while still not being allowed to reach it.
        """
        needles = ("app.timesheets", "app/timesheets",
                   "timesheets.time_tracking", "timesheets/time_tracking")
        offenders = []
        for path in _production_files():
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for value in _code_string_literals(tree):
                for needle in needles:
                    if needle in value:
                        offenders.append(
                            f"{path.relative_to(BACKEND).as_posix()} builds "
                            f"{value!r}")
        assert offenders == [], f"{offenders}. {_WHY}"

    def test_no_router_from_it_is_mounted(self):
        """`include_router` is the mount verb. Its argument must never be a
        name bound from the dead package."""
        tree = ast.parse(MAIN.read_text(encoding="utf-8"))

        bound_from_dead = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module \
                    and _references_the_dead_package(node.module):
                for alias in node.names:
                    bound_from_dead.add(alias.asname or alias.name)

        mounted = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if getattr(node.func, "attr", None) != "include_router":
                continue
            for arg in node.args:
                if isinstance(arg, ast.Name):
                    mounted.append(arg.id)
                elif isinstance(arg, ast.Attribute):
                    mounted.append(ast.unparse(arg))

        assert mounted, "no include_router calls found — the scan is vacuous"
        offenders = [m for m in mounted
                     if m in bound_from_dead
                     or "time_tracking" in m or "timesheet" in m.lower()]
        assert offenders == [], f"mounted {offenders}. {_WHY}"

    def test_the_guard_message_names_the_ruling_and_the_required_review(self):
        """The failure text is the whole mechanism: it has to tell whoever hit
        it what to do. Pinned so it cannot be softened."""
        assert "§45.D-15" in _WHY
        assert "D-15 compliance review" in _WHY


class TestTheDeadWriterIsUnchanged:
    """D15-1-B: not mounted, and equally NOT deleted and NOT modified."""

    def test_it_still_exists(self):
        assert DEAD_FILE.is_file(), (
            "app/timesheets/time_tracking.py was deleted. D15-1-B ruled it "
            "kept in place, unmounted and guarded — not removed."
        )

    def test_it_still_has_both_boundary_writers(self):
        """If it ever loses them it stops being the risk this guards, and the
        ruling should be revisited rather than the test quietly passing."""
        text = DEAD_FILE.read_text(encoding="utf-8")
        assert "clock_in_time" in text
        assert "clock_out_time" in text

    def test_d15_did_not_wire_provenance_into_it(self):
        """It is explicitly NOT a production writer, so it must not have been
        given provenance — that would contradict the ruling and imply it is
        safe to mount."""
        text = DEAD_FILE.read_text(encoding="utf-8")
        for token in ("clock_session_boundary", "append_boundary_event",
                      "Provenance", "actor_label"):
            assert token not in text, (
                f"{token} was added to the dead writer; D15-1-B assigns it no "
                f"provenance. {_WHY}"
            )

    def test_the_package_is_still_not_importable_as_a_regular_package(self):
        """No `__init__.py`: one more reason it cannot be reached by accident.
        Informational — the import scans above are the real guard."""
        assert not (APP / "timesheets" / "__init__.py").exists()


class TestTheGuardIsNotVacuous:
    """A scan that finds nothing because it is looking in the wrong place is
    worse than no scan. These pin the scan's own inputs."""

    def test_it_scans_a_realistic_number_of_production_files(self):
        files = _production_files()
        assert len(files) > 100, len(files)
        assert MAIN in files

    def test_it_excludes_the_dead_package_from_the_scan(self):
        """Otherwise the dead module's own `from .time_tracking import` style
        content could trip the guard against itself."""
        assert all(not p.is_relative_to(APP / "timesheets")
                   for p in _production_files())

    @pytest.mark.parametrize("name,expected", [
        ("app.timesheets", True),
        ("app.timesheets.time_tracking", True),
        ("timesheets", True),
        ("timesheets.time_tracking", True),
        ("app.timesheet_utils", False),
        ("app.employees.router", False),
        ("app.clock_session_boundary", False),
    ])
    def test_the_matcher_recognises_the_dead_package_and_nothing_else(
            self, name, expected):
        assert _references_the_dead_package(name) is expected

    def test_the_matcher_would_catch_a_real_mount(self):
        """Proves the mount scan's predicate, without editing main.py."""
        tree = ast.parse(
            "from app.timesheets.time_tracking import router as tt_router\n"
            "app.include_router(tt_router)\n"
        )
        bound = {
            alias.asname or alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module
            and _references_the_dead_package(node.module)
            for alias in node.names
        }
        assert bound == {"tt_router"}
        mounted = [
            arg.id for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and getattr(node.func, "attr", None) == "include_router"
            for arg in node.args if isinstance(arg, ast.Name)
        ]
        assert mounted == ["tt_router"]
        assert [m for m in mounted if m in bound] == ["tt_router"]
