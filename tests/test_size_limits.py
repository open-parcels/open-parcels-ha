"""The owner's size limits: 1000 lines per source file, 200 per function."""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).parent.parent
FILE_LIMIT = 1000
FUNCTION_LIMIT = 200


def _sources() -> list[Path]:
    files = sorted((ROOT / "custom_components").rglob("*.py")) + sorted(
        (ROOT / "tests").glob("*.py")
    )
    # Guard against a glob that silently matched nothing.
    assert len(files) > 10
    return files


def test_files_stay_under_the_limit() -> None:
    over = [
        f"{path.relative_to(ROOT)}: {lines} lines"
        for path in _sources()
        if (lines := len(path.read_text(encoding="utf-8").splitlines())) > FILE_LIMIT
    ]
    assert not over, "files over the size limit:\n" + "\n".join(over)


def test_functions_stay_under_the_limit() -> None:
    over = [
        f"{path.relative_to(ROOT)}:{node.lineno} {node.name}: {length} lines"
        for path in _sources()
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
        and (length := (node.end_lineno or node.lineno) - node.lineno + 1)
        > FUNCTION_LIMIT
    ]
    assert not over, "functions over the size limit:\n" + "\n".join(over)
