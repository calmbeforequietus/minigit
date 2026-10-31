from pathlib import Path

import pytest

from minigit.selection import FileSelection


def make_file(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("test", encoding="utf-8")
    return path


def test_add_file_prevents_duplicates_and_tracks_count(tmp_path: Path) -> None:
    file_path = make_file(tmp_path / "folder" / "example.txt")
    selection = FileSelection()

    assert selection.add_file(file_path) is True
    assert selection.add_file(file_path) is False
    assert selection.count == 1
    assert selection.files == (file_path.resolve(),)
    assert selection.upload_targets == ((file_path.resolve(), "example.txt"),)


def test_add_directory_recurses_and_excludes_git_and_node_modules(
    tmp_path: Path,
) -> None:
    first = make_file(tmp_path / "src" / "main.py")
    second = make_file(tmp_path / "assets" / "icon.svg")
    make_file(tmp_path / ".git" / "objects" / "ignored")
    make_file(tmp_path / "node_modules" / "package" / "ignored.js")
    make_file(tmp_path / "src" / "node_modules" / "nested" / "ignored.js")
    selection = FileSelection()

    assert selection.add_directory(tmp_path) == 2
    assert set(selection.files) == {first.resolve(), second.resolve()}
    assert selection.count == 2
    assert dict((source, target) for source, target in selection.upload_targets) == {
        first.resolve(): "src/main.py",
        second.resolve(): "assets/icon.svg",
    }


def test_individual_files_preserve_hierarchy_from_shared_selected_root(
    tmp_path: Path,
) -> None:
    first = make_file(tmp_path / "project" / "src" / "Config.json")
    second = make_file(tmp_path / "project" / "tests" / "config.json")
    selection = FileSelection()

    selection.add_file(first)
    selection.add_file(second)

    assert selection.target_collisions == {}
    assert dict(selection.upload_targets) == {
        first.resolve(): "src/Config.json",
        second.resolve(): "tests/config.json",
    }


def test_folder_paths_stay_rooted_to_folder_when_individual_files_are_added(
    tmp_path: Path,
) -> None:
    folder_file = make_file(tmp_path / "folder" / "assets" / "icon.svg")
    individual = make_file(tmp_path / "other" / "readme.md")
    selection = FileSelection()

    selection.add_directory(tmp_path / "folder")
    selection.add_file(individual)

    assert dict(selection.upload_targets) == {
        folder_file.resolve(): "assets/icon.svg",
        individual.resolve(): "readme.md",
    }


def test_add_directory_does_not_duplicate_existing_files(tmp_path: Path) -> None:
    file_path = make_file(tmp_path / "example.txt")
    selection = FileSelection()
    selection.add_file(file_path)

    assert selection.add_directory(tmp_path) == 0
    assert selection.count == 1


def test_remove_file_updates_selection_and_count(tmp_path: Path) -> None:
    file_path = make_file(tmp_path / "example.txt")
    selection = FileSelection()
    selection.add_file(file_path)

    assert selection.remove_file(file_path) is True
    assert selection.remove_file(file_path) is False
    assert selection.files == ()
    assert selection.upload_targets == ()
    assert selection.count == 0


def test_invalid_paths_raise_clear_errors(tmp_path: Path) -> None:
    missing = tmp_path / "missing.txt"
    selection = FileSelection()

    with pytest.raises(FileNotFoundError):
        selection.add_file(missing)
    with pytest.raises(ValueError, match="Not a directory"):
        selection.add_directory(make_file(tmp_path / "file.txt"))
