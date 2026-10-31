from __future__ import annotations

import os
from pathlib import Path

EXCLUDED_DIRECTORIES = frozenset({".git", "node_modules"})


class FileSelection:
    """Ordered, duplicate-free collection of files selected for a push."""

    def __init__(self) -> None:
        self._files: dict[Path, Path] = {}
        self._targets: dict[Path, str] = {}
        self._directory_files: set[Path] = set()

    @property
    def files(self) -> tuple[Path, ...]:
        return tuple(self._files.values())

    @property
    def count(self) -> int:
        return len(self._files)

    @property
    def upload_targets(self) -> tuple[tuple[Path, str], ...]:
        """Return source files and their proposed repository-relative paths."""
        return tuple((source, self._targets[source]) for source in self._files)

    @property
    def target_collisions(self) -> dict[str, tuple[Path, ...]]:
        targets: dict[str, list[Path]] = {}
        for source, target in self.upload_targets:
            targets.setdefault(target.casefold(), []).append(source)
        return {
            self._targets[sources[0]]: tuple(sources)
            for sources in targets.values()
            if len(sources) > 1
        }

    def add_file(self, path: str | Path) -> bool:
        """Add an existing file and preserve paths relative to a shared selected root."""
        file_path = Path(path).expanduser().resolve(strict=True)
        if not file_path.is_file():
            raise ValueError(f"Not a file: {path}")
        if file_path in self._files:
            return False
        self._files[file_path] = file_path
        if file_path not in self._directory_files:
            self._refresh_individual_targets()
        return True

    def add_directory(self, path: str | Path) -> int:
        """Add files under a directory, skipping excluded directories."""
        directory = Path(path).expanduser().resolve(strict=True)
        if not directory.is_dir():
            raise ValueError(f"Not a directory: {path}")

        discovered: list[tuple[Path, str]] = []

        def raise_walk_error(error: OSError) -> None:
            raise error

        for root, directories, filenames in os.walk(
            directory, topdown=True, onerror=raise_walk_error, followlinks=False
        ):
            directories[:] = [
                name
                for name in directories
                if name.casefold() not in EXCLUDED_DIRECTORIES
            ]
            for filename in filenames:
                walked_path = Path(root) / filename
                file_path = walked_path.resolve(strict=True)
                if file_path.is_file():
                    relative_target = walked_path.relative_to(directory).as_posix()
                    discovered.append((file_path, relative_target))

        new_files = dict(discovered)
        added = 0
        for source, target in new_files.items():
            if source not in self._files:
                self._files[source] = source
                added += 1
            self._directory_files.add(source)
            self._targets[source] = target
        return added

    def remove_file(self, path: str | Path) -> bool:
        """Remove a previously selected file; return whether it was present."""
        file_path = Path(path).expanduser().resolve(strict=False)
        removed = self._files.pop(file_path, None) is not None
        self._targets.pop(file_path, None)
        self._directory_files.discard(file_path)
        self._refresh_individual_targets()
        return removed

    def _refresh_individual_targets(self) -> None:
        individual_files = [
            source for source in self._files if source not in self._directory_files
        ]
        if not individual_files:
            return
        try:
            common_root = Path(os.path.commonpath([str(path.parent) for path in individual_files]))
        except ValueError:
            common_root = None
        for source in individual_files:
            if common_root is None:
                self._targets[source] = source.name
                continue
            self._targets[source] = source.relative_to(common_root).as_posix()
