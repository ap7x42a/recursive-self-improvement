from __future__ import annotations

import errno
import os
import shutil
import subprocess
from pathlib import Path
from typing import Iterable


class SnapshotError(RuntimeError):
    pass


def _top_component(relative: Path) -> str:
    return relative.parts[0] if relative.parts else ""


def _is_excluded(relative: Path, excludes: set[str]) -> bool:
    if not relative.parts:
        return False
    posix = relative.as_posix()
    return _top_component(relative) in excludes or posix in excludes


def _ignore(excludes: set[str]):
    def callback(directory: str, names: list[str]) -> set[str]:
        base = Path(directory)
        ignored: set[str] = set()
        for name in names:
            if name in excludes:
                ignored.add(name)
        return ignored
    return callback


def _copy_reflink(source: str, destination: str) -> str:
    # GNU cp reflink support gives cheap copy-on-write snapshots where available.
    completed = subprocess.run(
        ["cp", "--reflink=auto", "--preserve=all", source, destination],
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode == 0:
        return destination
    return shutil.copy2(source, destination)


def clone_tree(source: Path, destination: Path, *, mode: str, excludes: Iterable[str]) -> None:
    source = source.resolve()
    if destination.exists() or destination.is_symlink():
        if destination.is_dir() and not destination.is_symlink():
            shutil.rmtree(destination)
        else:
            destination.unlink()
    destination.parent.mkdir(parents=True, exist_ok=True)
    if mode == "none":
        destination.mkdir(parents=True, exist_ok=True)
        return

    excluded = set(excludes)
    if mode == "reflink":
        destination.mkdir(parents=True, exist_ok=True)
        completed = subprocess.run(
            ["cp", "-a", "--reflink=auto", str(source) + "/.", str(destination)],
            capture_output=True,
            text=True,
            check=False,
        )
        if completed.returncode == 0:
            for item in excluded:
                target = destination / item
                if target.is_dir() and not target.is_symlink():
                    shutil.rmtree(target, ignore_errors=True)
                elif target.exists() or target.is_symlink():
                    target.unlink()
            return
        shutil.rmtree(destination, ignore_errors=True)

    shutil.copytree(
        source,
        destination,
        symlinks=True,
        copy_function=shutil.copy2,
        ignore=_ignore(excluded),
    )


def mirror_tree(source: Path, destination: Path, *, excludes: Iterable[str]) -> None:
    """Make destination match source while preserving excluded top-level paths."""
    source = source.resolve()
    destination.mkdir(parents=True, exist_ok=True)
    excluded = set(excludes)

    for existing in list(destination.iterdir()):
        if existing.name in excluded:
            continue
        counterpart = source / existing.name
        if not counterpart.exists() and not counterpart.is_symlink():
            if existing.is_dir() and not existing.is_symlink():
                shutil.rmtree(existing)
            else:
                existing.unlink()

    for child in source.iterdir():
        if child.name in excluded:
            continue
        target = destination / child.name
        if target.exists() or target.is_symlink():
            if target.is_dir() and not target.is_symlink():
                shutil.rmtree(target)
            else:
                target.unlink()
        if child.is_symlink():
            target.symlink_to(os.readlink(child), target_is_directory=child.is_dir())
        elif child.is_dir():
            shutil.copytree(child, target, symlinks=True, copy_function=_copy_reflink)
        else:
            _copy_reflink(str(child), str(target))


def restore_tree(snapshot: Path, workspace: Path, *, excludes: Iterable[str]) -> None:
    mirror_tree(snapshot, workspace, excludes=excludes)
