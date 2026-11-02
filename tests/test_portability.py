"""Source-level guards for the Windows hazards CI only sees on a Windows runner.

* A text-mode write without ``newline=`` turns ``\\n`` into ``\\r\\n`` on Windows, which changes
  the bytes that manifests and v1-equivalence hashes cover.
* ``os.path.relpath`` raises ValueError when the two paths are on different drives; the one
  call goes through ``openleo.scenario.parse._relative_posix``, which falls back to the
  absolute POSIX path.
"""

import ast
from pathlib import Path

SOURCE = Path(__file__).resolve().parents[1] / "src" / "openleo"
FILES = sorted(SOURCE.rglob("*.py"))


def _calls(path: Path):
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    parents = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parents[child] = node
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            function = node.func
            name = (
                function.attr
                if isinstance(function, ast.Attribute)
                else getattr(function, "id", "")
            )
            yield node, name, parents


def _text_write_mode(call: ast.Call) -> bool:
    modes = [k.value for k in call.keywords if k.arg == "mode"] + call.args[:1]
    if isinstance(call.func, ast.Name):  # builtin open(path, mode)
        modes = [k.value for k in call.keywords if k.arg == "mode"] + call.args[1:2]
    for mode in modes:
        if isinstance(mode, ast.Constant) and isinstance(mode.value, str):
            return "b" not in mode.value and any(c in mode.value for c in "wax+")
    return False


def _enclosing_function(node: ast.AST, parents: dict) -> str:
    while node in parents:
        node = parents[node]
        if isinstance(node, ast.FunctionDef):
            return node.name
    return ""


def test_text_writes_pin_their_newlines() -> None:
    unpinned = [
        f"{path.relative_to(SOURCE).as_posix()}:{node.lineno}"
        for path in FILES
        for node, name, _ in _calls(path)
        if (name == "write_text" or (name == "open" and _text_write_mode(node)))
        and not any(keyword.arg == "newline" for keyword in node.keywords)
    ]
    assert unpinned == [], f"text writes without newline=: {unpinned}"


def test_relative_paths_have_the_cross_drive_fallback() -> None:
    unguarded = [
        f"{path.relative_to(SOURCE).as_posix()}:{node.lineno}"
        for path in FILES
        for node, name, parents in _calls(path)
        if name == "relpath"
        and not (
            path == SOURCE / "scenario" / "parse.py"
            and _enclosing_function(node, parents) == "_relative_posix"
        )
    ]
    assert unguarded == [], f"os.path.relpath outside _relative_posix: {unguarded}"


def test_the_guards_see_the_calls_they_check() -> None:
    parse = SOURCE / "scenario" / "parse.py"
    assert any(name == "relpath" for _, name, _ in _calls(parse))
    hydrometeors = SOURCE / "calc" / "hydrometeors.py"
    assert any(name == "write_text" for _, name, _ in _calls(hydrometeors))
    gases = SOURCE / "calc" / "gases.py"
    assert any(name == "open" and _text_write_mode(node) for node, name, _ in _calls(gases))
