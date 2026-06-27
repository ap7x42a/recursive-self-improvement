from __future__ import annotations

import fnmatch
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import tempfile
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable, Iterator

from .config import LimitsConfig


class ToolError(RuntimeError):
    pass


@dataclass(slots=True, frozen=True)
class FileState:
    kind: str
    digest: str
    size: int
    mode: int


def _excluded(relative: Path, excludes: set[str]) -> bool:
    if not relative.parts:
        return False
    return relative.parts[0] in excludes or relative.as_posix() in excludes


def file_manifest(root: Path, excludes: Iterable[str]) -> dict[str, FileState]:
    root = root.resolve()
    excluded = set(excludes)
    result: dict[str, FileState] = {}
    stack = [root]
    while stack:
        directory = stack.pop()
        try:
            entries = list(os.scandir(directory))
        except OSError:
            continue
        for entry in entries:
            path = Path(entry.path)
            try:
                relative = path.relative_to(root)
            except ValueError:
                continue
            if _excluded(relative, excluded):
                continue
            try:
                metadata = entry.stat(follow_symlinks=False)
            except OSError:
                continue
            mode = stat.S_IMODE(metadata.st_mode)
            if entry.is_symlink():
                target = os.readlink(path)
                digest = hashlib.sha256(target.encode("utf-8", errors="surrogateescape")).hexdigest()
                result[relative.as_posix()] = FileState("symlink", digest, len(target), mode)
            elif entry.is_dir(follow_symlinks=False):
                result[relative.as_posix() + "/"] = FileState("directory", "", 0, mode)
                stack.append(path)
            elif entry.is_file(follow_symlinks=False):
                hasher = hashlib.sha256()
                try:
                    with path.open("rb") as handle:
                        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                            hasher.update(chunk)
                except OSError:
                    continue
                result[relative.as_posix()] = FileState("file", hasher.hexdigest(), metadata.st_size, mode)
            else:
                result[relative.as_posix()] = FileState("special", "", metadata.st_size, mode)
    return result


def manifest_digest(manifest: dict[str, FileState]) -> str:
    hasher = hashlib.sha256()
    for path, state in sorted(manifest.items()):
        hasher.update(path.encode("utf-8", errors="surrogateescape"))
        hasher.update(b"\0")
        hasher.update(json.dumps(asdict(state), sort_keys=True).encode("utf-8"))
        hasher.update(b"\n")
    return hasher.hexdigest()


def diff_manifests(before: dict[str, FileState], after: dict[str, FileState]) -> dict[str, list[str]]:
    before_keys = set(before)
    after_keys = set(after)
    return {
        "created": sorted(after_keys - before_keys),
        "deleted": sorted(before_keys - after_keys),
        "modified": sorted(path for path in before_keys & after_keys if before[path] != after[path]),
    }


def changed_line_estimate(before_root: Path, after_root: Path, diff: dict[str, list[str]]) -> int:
    """Cheap deterministic line estimate used only as a tie-breaker."""
    total = 0
    for category in ("created", "deleted", "modified"):
        for relative in diff[category]:
            if relative.endswith("/"):
                continue
            for root in ([after_root] if category == "created" else [before_root] if category == "deleted" else [before_root, after_root]):
                path = root / relative
                if path.is_file() and not path.is_symlink():
                    try:
                        with path.open("rb") as handle:
                            total += sum(chunk.count(b"\n") for chunk in iter(lambda: handle.read(1024 * 1024), b"")) + 1
                    except OSError:
                        total += 1
    return total


def inventory(root: Path, *, depth: int, max_entries: int, excludes: Iterable[str]) -> list[str]:
    root = root.resolve()
    excluded = set(excludes)
    lines: list[str] = []
    count = 0
    stack: list[tuple[Path, int]] = [(root, 0)]
    while stack:
        directory, level = stack.pop()
        if level >= depth:
            continue
        try:
            entries = sorted(os.scandir(directory), key=lambda item: item.name, reverse=True)
        except OSError:
            continue
        for entry in entries:
            path = Path(entry.path)
            try:
                relative = path.relative_to(root)
            except ValueError:
                continue
            if _excluded(relative, excluded):
                continue
            suffix = "/" if entry.is_dir(follow_symlinks=False) else "@" if entry.is_symlink() else ""
            lines.append(relative.as_posix() + suffix)
            count += 1
            if max_entries and count >= max_entries:
                return sorted(lines)
            if entry.is_dir(follow_symlinks=False):
                stack.append((path, level + 1))
    return sorted(lines)


class WorkspaceTools:
    def __init__(
        self,
        root: Path,
        *,
        excludes: Iterable[str],
        protected_paths: Iterable[str],
        limits: LimitsConfig,
        command_timeout: float,
    ) -> None:
        self.root = root.resolve()
        self.excludes = set(excludes)
        self.protected = {Path(item).as_posix().strip("/") for item in protected_paths}
        self.limits = limits
        self.command_timeout = command_timeout
        self.baseline = file_manifest(self.root, self.excludes)

    def _cap(self, text: str) -> str:
        limit = self.limits.tool_output_bytes
        if limit and len(text.encode("utf-8", errors="replace")) > limit:
            encoded = text.encode("utf-8", errors="replace")
            clipped = encoded[:limit].decode("utf-8", errors="replace")
            return clipped + f"\n...[truncated at {limit} bytes]"
        return text

    def _relative(self, value: str) -> Path:
        raw = Path(value or ".")
        if raw.is_absolute():
            raise ToolError("file tools require workspace-relative paths")
        candidate = (self.root / raw).resolve(strict=False)
        try:
            relative = candidate.relative_to(self.root)
        except ValueError as exc:
            raise ToolError(f"path leaves workspace: {value}") from exc
        if _excluded(relative, self.excludes):
            raise ToolError(f"path is excluded: {value}")
        return relative

    def _path(self, value: str) -> Path:
        return self.root / self._relative(value)

    def _ensure_mutable(self, relative: Path) -> None:
        posix = relative.as_posix().strip("/")
        for protected in self.protected:
            if posix == protected or posix.startswith(protected.rstrip("/") + "/"):
                raise ToolError(f"path is protected by the trusted evaluator: {posix}")

    def execute(self, action: str, args: dict[str, Any]) -> dict[str, Any]:
        handler = getattr(self, f"tool_{action}", None)
        if handler is None or action.startswith("_"):
            raise ToolError(f"unknown action: {action}")
        result = handler(**args)
        if isinstance(result, str):
            return {"ok": True, "output": self._cap(result)}
        if isinstance(result, dict):
            return {"ok": True, **result}
        return {"ok": True, "result": result}

    def tool_list(self, path: str = ".", recursive: bool = False, depth: int = 1) -> str:
        relative = self._relative(path)
        base = self.root / relative
        if not base.is_dir():
            raise ToolError(f"not a directory: {path}")
        max_depth = depth if recursive else 1
        entries = inventory(base, depth=max_depth, max_entries=self.limits.inventory_entries, excludes=self.excludes)
        return "\n".join(entries)

    def tool_read(self, path: str, start_line: int = 1, end_line: int = 0) -> str:
        file_path = self._path(path)
        if not file_path.is_file():
            raise ToolError(f"not a file: {path}")
        if start_line < 1 or end_line < 0 or (end_line and end_line < start_line):
            raise ToolError("invalid line range")
        byte_limit = self.limits.read_bytes
        output: list[str] = []
        used = 0
        with file_path.open("r", encoding="utf-8", errors="replace") as handle:
            for number, line in enumerate(handle, 1):
                if number < start_line:
                    continue
                if end_line and number > end_line:
                    break
                rendered = f"{number:>7}: {line}"
                encoded = rendered.encode("utf-8", errors="replace")
                if byte_limit and used + len(encoded) > byte_limit:
                    output.append(f"...[read limit reached at {byte_limit} bytes]\n")
                    break
                output.append(rendered)
                used += len(encoded)
        return "".join(output)

    def tool_glob(self, pattern: str) -> str:
        matches: list[str] = []
        maximum = self.limits.search_matches
        for path in self.root.glob(pattern):
            try:
                relative = path.resolve(strict=False).relative_to(self.root)
            except ValueError:
                continue
            if _excluded(relative, self.excludes):
                continue
            matches.append(relative.as_posix() + ("/" if path.is_dir() else ""))
            if maximum and len(matches) >= maximum:
                break
        return "\n".join(sorted(matches))

    def tool_search(
        self,
        query: str,
        path: str = ".",
        glob: str = "*",
        regex: bool = False,
        case_sensitive: bool = True,
    ) -> str:
        base = self._path(path)
        if not base.exists():
            raise ToolError(f"path does not exist: {path}")
        flags = 0 if case_sensitive else re.IGNORECASE
        expression = re.compile(query if regex else re.escape(query), flags)
        maximum = self.limits.search_matches
        matches: list[str] = []
        paths = [base] if base.is_file() else base.rglob("*")
        for candidate in paths:
            if not candidate.is_file() or candidate.is_symlink():
                continue
            try:
                relative = candidate.relative_to(self.root)
            except ValueError:
                continue
            if _excluded(relative, self.excludes) or not fnmatch.fnmatch(candidate.name, glob):
                continue
            try:
                with candidate.open("r", encoding="utf-8", errors="replace") as handle:
                    for number, line in enumerate(handle, 1):
                        if expression.search(line):
                            matches.append(f"{relative.as_posix()}:{number}:{line.rstrip()}")
                            if maximum and len(matches) >= maximum:
                                return "\n".join(matches)
            except OSError:
                continue
        return "\n".join(matches)

    def tool_write(self, path: str, content: str, append: bool = False) -> str:
        relative = self._relative(path)
        self._ensure_mutable(relative)
        target = self.root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        if append:
            with target.open("a", encoding="utf-8") as handle:
                handle.write(content)
        else:
            descriptor, temporary_name = tempfile.mkstemp(prefix=target.name + ".", dir=target.parent)
            temporary = Path(temporary_name)
            try:
                with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                    handle.write(content)
                    handle.flush()
                    os.fsync(handle.fileno())
                if target.exists():
                    shutil.copymode(target, temporary, follow_symlinks=False)
                temporary.replace(target)
            finally:
                if temporary.exists():
                    temporary.unlink()
        return f"wrote {len(content.encode('utf-8'))} bytes to {relative.as_posix()}"

    def tool_replace(self, path: str, old: str, new: str, count: int = 0) -> str:
        relative = self._relative(path)
        self._ensure_mutable(relative)
        target = self.root / relative
        if not target.is_file():
            raise ToolError(f"not a file: {path}")
        content = target.read_text(encoding="utf-8", errors="strict")
        occurrences = content.count(old)
        if occurrences == 0:
            raise ToolError("old text not found")
        if count < 0:
            raise ToolError("count cannot be negative")
        replaced = content.replace(old, new, count if count else -1)
        self.tool_write(path, replaced)
        actual = min(occurrences, count) if count else occurrences
        return f"replaced {actual} occurrence(s) in {relative.as_posix()}"

    def tool_delete(self, path: str) -> str:
        relative = self._relative(path)
        self._ensure_mutable(relative)
        target = self.root / relative
        if not target.exists() and not target.is_symlink():
            raise ToolError(f"path does not exist: {path}")
        if target.is_dir() and not target.is_symlink():
            shutil.rmtree(target)
        else:
            target.unlink()
        return f"deleted {relative.as_posix()}"

    def tool_move(self, source: str, destination: str) -> str:
        source_relative = self._relative(source)
        destination_relative = self._relative(destination)
        self._ensure_mutable(source_relative)
        self._ensure_mutable(destination_relative)
        source_path = self.root / source_relative
        destination_path = self.root / destination_relative
        if not source_path.exists() and not source_path.is_symlink():
            raise ToolError(f"source does not exist: {source}")
        destination_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(source_path), str(destination_path))
        return f"moved {source_relative.as_posix()} to {destination_relative.as_posix()}"

    def tool_mkdir(self, path: str, parents: bool = True) -> str:
        relative = self._relative(path)
        self._ensure_mutable(relative)
        target = self.root / relative
        target.mkdir(parents=parents, exist_ok=True)
        return f"created directory {relative.as_posix()}"

    def tool_chmod(self, path: str, mode: str | int) -> str:
        relative = self._relative(path)
        self._ensure_mutable(relative)
        target = self.root / relative
        parsed = int(str(mode), 8) if isinstance(mode, str) else int(mode)
        target.chmod(parsed)
        return f"changed mode of {relative.as_posix()} to {parsed:o}"

    def tool_run(self, command: str, timeout: float | None = None) -> dict[str, Any]:
        effective_timeout = float(timeout) if timeout is not None else self.command_timeout
        if effective_timeout <= 0:
            raise ToolError("timeout must be positive")
        start = time.monotonic()
        try:
            completed = subprocess.run(
                command,
                cwd=self.root,
                shell=True,
                text=True,
                capture_output=True,
                timeout=effective_timeout,
                check=False,
            )
            return {
                "returncode": completed.returncode,
                "stdout": self._cap(completed.stdout),
                "stderr": self._cap(completed.stderr),
                "duration_seconds": time.monotonic() - start,
            }
        except subprocess.TimeoutExpired as exc:
            stdout = exc.stdout.decode(errors="replace") if isinstance(exc.stdout, bytes) else (exc.stdout or "")
            stderr = exc.stderr.decode(errors="replace") if isinstance(exc.stderr, bytes) else (exc.stderr or "")
            return {
                "returncode": None,
                "stdout": self._cap(stdout),
                "stderr": self._cap(stderr),
                "duration_seconds": time.monotonic() - start,
                "timed_out": True,
            }

    def tool_status(self) -> dict[str, Any]:
        current = file_manifest(self.root, self.excludes)
        diff = diff_manifests(self.baseline, current)
        return {
            "manifest": manifest_digest(current),
            "created": diff["created"],
            "modified": diff["modified"],
            "deleted": diff["deleted"],
        }
