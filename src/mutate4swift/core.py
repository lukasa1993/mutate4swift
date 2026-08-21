from __future__ import annotations

import base64
import json
import os
import signal
import subprocess
import tempfile
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable, Iterator, Sequence

from tree_sitter_language_pack import get_parser

LANGUAGE = "swift"
EXTENSIONS = (".swift",)
EXCLUDED_DIRS = frozenset((".git", ".hg", ".idea", ".pytest_cache", ".tox", ".venv", ".build", "build", "coverage", "dist", "node_modules", "target", "vendor", "venv", "DerivedData", "Pods"))
TEST_DIRS = frozenset(("Tests", "tests"))
TEST_SUFFIXES = ("Tests.swift", "Test.swift")
JOURNAL_PATH = Path("target/mutation/active.json")
SKIPPED_ANCESTORS = frozenset({"comment", "line_comment", "block_comment", "string_literal", "raw_string_literal", "char_literal", "string"})
BINARY_PARENTS = frozenset({"binary_expression", "logical_expression", "boolean_expression", "arithmetic_expression", "comparison_expression"})
REPLACEMENTS = {"==": "!=", "!=": "==", ">": "<=", "<": ">=", ">=": "<", "<=": ">", "&&": "||", "||": "&&", "true": "false", "false": "true"}
ARITHMETIC_REPLACEMENTS = {"+": "-", "-": "+", "*": "/", "/": "*"}


class MutationError(RuntimeError):
    pass


@dataclass(frozen=True)
class Mutation:
    id: int
    file: str
    line: int
    column: int
    original: str
    replacement: str
    start: int
    end: int

    def public_dict(self) -> dict[str, object]:
        value = asdict(self)
        value.pop("start")
        value.pop("end")
        return value


@dataclass(frozen=True)
class CommandResult:
    returncode: int | None
    timed_out: bool
    duration_seconds: float
    output: str


@dataclass(frozen=True)
class MutationResult:
    mutation: Mutation
    status: str
    exit_code: int | None
    duration_seconds: float
    detail: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {**self.mutation.public_dict(), "status": self.status, "exit_code": self.exit_code, "duration_seconds": round(self.duration_seconds, 3), "detail": self.detail}


def _is_test_path(relative: str) -> bool:
    path = Path(relative)
    lowered = {value.lower() for value in TEST_DIRS}
    return any(part in TEST_DIRS or part.lower() in lowered for part in path.parts) or path.name.endswith(TEST_SUFFIXES)


def discover_files(root: Path, filters: Sequence[str] = (), include_tests: bool = False) -> list[Path]:
    output: list[Path] = []
    for directory, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(name for name in dirnames if name not in EXCLUDED_DIRS)
        for filename in sorted(filenames):
            if not filename.endswith(EXTENSIONS):
                continue
            path = Path(directory, filename)
            relative = path.relative_to(root).as_posix()
            if not include_tests and _is_test_path(relative):
                continue
            if filters and not any(fragment in relative for fragment in filters):
                continue
            output.append(path)
    return output


def _walk(node: Any) -> Iterator[Any]:
    yield node
    for child in node.children:
        yield from _walk(child)


def _text(node: Any, source: bytes) -> str:
    return source[node.start_byte:node.end_byte].decode("utf-8", errors="replace")


def _ancestor_types(node: Any) -> set[str]:
    values: set[str] = set()
    parent = getattr(node, "parent", None)
    while parent is not None:
        values.add(parent.type)
        parent = getattr(parent, "parent", None)
    return values


def _point(node: Any) -> tuple[int, int]:
    point = node.start_point
    return (int(point.row) + 1, int(point.column) + 1) if hasattr(point, "row") else (int(point[0]) + 1, int(point[1]) + 1)


def _candidate(node: Any, source: bytes) -> str | None:
    if node.children or _ancestor_types(node) & SKIPPED_ANCESTORS:
        return None
    text = _text(node, source)
    if text in REPLACEMENTS:
        return REPLACEMENTS[text]
    parent = getattr(node, "parent", None)
    if text in ARITHMETIC_REPLACEMENTS and parent is not None and parent.type in BINARY_PARENTS:
        return ARITHMETIC_REPLACEMENTS[text]
    return None


def parse_valid(source: bytes) -> bool:
    return not get_parser(LANGUAGE).parse(source).root_node.has_error


def enumerate_mutations(path: Path, root: Path, start_id: int = 1) -> list[Mutation]:
    source = path.read_bytes()
    tree = get_parser(LANGUAGE).parse(source)
    if tree.root_node.has_error:
        raise MutationError(f"source contains parse errors: {path}")
    candidates: list[Mutation] = []
    relative = path.relative_to(root).as_posix()
    for node in _walk(tree.root_node):
        replacement = _candidate(node, source)
        if replacement is None:
            continue
        line, column = _point(node)
        candidates.append(Mutation(0, relative, line, column, _text(node, source), replacement, node.start_byte, node.end_byte))
    candidates.sort(key=lambda item: (item.start, -(item.end - item.start), item.replacement))
    selected: list[Mutation] = []
    last_end = -1
    for mutation in candidates:
        if mutation.start < last_end:
            continue
        values = {key: value for key, value in asdict(mutation).items() if key != "id"}
        selected.append(Mutation(start_id + len(selected), **values))
        last_end = mutation.end
    return selected


def collect_mutations(root: Path, filters: Sequence[str] = (), include_tests: bool = False) -> list[Mutation]:
    output: list[Mutation] = []
    for path in discover_files(root, filters, include_tests):
        output.extend(enumerate_mutations(path, root, len(output) + 1))
    return output


def run_command(command: str, root: Path, timeout_seconds: float) -> CommandResult:
    started = time.monotonic()
    process = subprocess.Popen(command, cwd=root, shell=True, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, start_new_session=(os.name != "nt"))
    try:
        output, _ = process.communicate(timeout=timeout_seconds)
        return CommandResult(process.returncode, False, time.monotonic() - started, output or "")
    except subprocess.TimeoutExpired:
        if os.name != "nt":
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        else:
            process.kill()
        output, _ = process.communicate()
        return CommandResult(None, True, time.monotonic() - started, output or "")


def _atomic_write(path: Path, data: bytes, mode: int | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        if mode is not None:
            os.chmod(temporary_name, mode)
        os.replace(temporary_name, path)
    finally:
        if os.path.exists(temporary_name):
            os.unlink(temporary_name)


def recover_active(root: Path) -> bool:
    journal = root / JOURNAL_PATH
    if not journal.exists():
        return False
    payload = json.loads(journal.read_text(encoding="utf-8"))
    path = root / str(payload["file"])
    _atomic_write(path, base64.b64decode(str(payload["content"])), int(payload.get("mode", 0o644)))
    journal.unlink(missing_ok=True)
    return True


class SourceMutation:
    def __init__(self, root: Path, mutation: Mutation):
        self.root = root
        self.mutation = mutation
        self.path = root / mutation.file
        self.original = self.path.read_bytes()
        self.mode = self.path.stat().st_mode
        self.journal = root / JOURNAL_PATH

    def __enter__(self) -> Path:
        self.journal.parent.mkdir(parents=True, exist_ok=True)
        payload = {"file": self.mutation.file, "content": base64.b64encode(self.original).decode("ascii"), "mode": self.mode}
        _atomic_write(self.journal, (json.dumps(payload, sort_keys=True) + "\n").encode("utf-8"), 0o600)
        mutated = self.original[:self.mutation.start] + self.mutation.replacement.encode() + self.original[self.mutation.end:]
        _atomic_write(self.path, mutated, self.mode)
        return self.path

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        _atomic_write(self.path, self.original, self.mode)
        self.journal.unlink(missing_ok=True)


def detect_test_command(root: Path) -> str | None:
    return "swift test --quiet" if (root / "Package.swift").exists() else None


def detect_validation_command(root: Path) -> str | None:
    return "swift build --quiet" if (root / "Package.swift").exists() else None


def run_mutations(root: Path, mutations: Iterable[Mutation], test_command: str, timeout_seconds: float, validation_command: str | None, max_mutants: int | None = None) -> list[MutationResult]:
    recover_active(root)
    results: list[MutationResult] = []
    for mutation in mutations:
        if max_mutants is not None and len(results) >= max_mutants:
            break
        with SourceMutation(root, mutation) as path:
            if not parse_valid(path.read_bytes()):
                results.append(MutationResult(mutation, "invalid", None, 0.0, "mutated source does not parse"))
                continue
            if validation_command:
                validation = run_command(validation_command, root, timeout_seconds)
                if validation.timed_out:
                    results.append(MutationResult(mutation, "timeout", None, validation.duration_seconds, "validation timed out"))
                    continue
                if validation.returncode != 0:
                    results.append(MutationResult(mutation, "compile-error", validation.returncode, validation.duration_seconds, validation.output[-2000:]))
                    continue
            execution = run_command(test_command, root, timeout_seconds)
            if execution.timed_out:
                results.append(MutationResult(mutation, "timeout", None, execution.duration_seconds, "test command timed out"))
            elif execution.returncode == 0:
                results.append(MutationResult(mutation, "survived", 0, execution.duration_seconds))
            else:
                results.append(MutationResult(mutation, "killed", execution.returncode, execution.duration_seconds))
    return results


def write_manifest(path: Path, tool: str, version: str, root: Path, results: Sequence[MutationResult]) -> None:
    counts: dict[str, int] = {}
    for result in results:
        counts[result.status] = counts.get(result.status, 0) + 1
    payload = {"schema_version": 1, "tool": tool, "version": version, "root": root.as_posix(), "summary": {"total": len(results), **counts}, "mutants": [result.to_dict() for result in results]}
    path.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write(path, (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode("utf-8"), 0o644)
