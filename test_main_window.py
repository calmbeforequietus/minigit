from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

import minigit.main_window as main_window
from minigit.github_auth import PublicPushPreview


class FakeTokenStore:
    def load(self) -> None:
        return None

    def load_authorization(self) -> tuple[None, bool]:
        return None, False

    def delete(self) -> None:
        return None


class FakeAuth:
    instances: list["FakeAuth"] = []

    client_id = "public-client"

    def __init__(self) -> None:
        self.token_store = FakeTokenStore()
        self.write_calls: list[tuple] = []
        self.fail_commit: str | None = None
        self.__class__.instances.append(self)

    def list_repository_branches(self, repository: str) -> list[dict[str, str]]:
        assert repository == "octocat/repo"
        return [{"name": "main", "commit_sha": "commit-sha"}]

    def get_repository_tree(self, repository: str, commit_sha: str) -> list[str]:
        assert repository == "octocat/repo"
        assert commit_sha == "commit-sha"
        return ["README.md", "src/main.py"]

    def download_public_file(self, repository: str, branch: str, path: str) -> bytes:
        assert repository == "octocat/repo"
        assert branch == "main"
        return f"downloaded:{path}".encode()

    def prepare_public_push(
        self, repository: str, branch: str, target_paths: list[str]
    ) -> PublicPushPreview:
        return PublicPushPreview(
            head_sha="head-sha",
            tree_sha="tree-sha",
            existing_paths=("README.md",),
            target_paths=tuple(target_paths),
            overwrite_paths=("README.md",) if "README.md" in target_paths else (),
        )

    def commit_and_push_public_files(
        self, repository, branch, preview, files, message
    ) -> str:
        if self.fail_commit is not None:
            raise ValueError(self.fail_commit)
        self.write_calls.append((repository, branch, preview, files, message))
        return "confirmed-commit-sha"


def test_upload_preview_displays_targets_and_overwrite_warnings(
    monkeypatch, tmp_path
) -> None:
    app = QApplication.instance() or QApplication([])
    monkeypatch.setattr(main_window, "GitHubDeviceAuth", FakeAuth)
    window = main_window.MainWindow()
    window.show()

    first = tmp_path / "one" / "config.json"
    second = tmp_path / "two" / "CONFIG.json"
    first.parent.mkdir()
    second.parent.mkdir()
    first.write_text("first", encoding="utf-8")
    second.write_text("second", encoding="utf-8")
    window.selection.add_directory(first.parent)
    window.selection.add_directory(second.parent)
    window._refresh_list()

    assert window.file_list.count() == 2
    assert "config.json" in window.file_list.item(0).text()
    assert "target-path collision" in window.upload_collision_label.text()

    window.repo_file_list.addItem("config.json")
    window._update_upload_conflicts()
    assert "already exist" in window.upload_collision_label.text()
    assert "push must confirm every overwrite" in window.upload_collision_label.text()
    window.close()


def test_adding_local_files_does_not_change_connected_github_state(
    monkeypatch, tmp_path
) -> None:
    app = QApplication.instance() or QApplication([])
    monkeypatch.setattr(main_window, "GitHubDeviceAuth", FakeAuth)
    window = main_window.MainWindow()
    window._authenticated_username = "octocat"
    window.github_status_label.setText("Connected as @octocat.")
    window.push_status_label.setText(
        "Connected. Select a public repository and reauthorize to push."
    )
    first = tmp_path / "project" / "src" / "app.py"
    second = tmp_path / "project" / "tests" / "test_app.py"
    first.parent.mkdir(parents=True)
    second.parent.mkdir(parents=True)
    first.write_text("app", encoding="utf-8")
    second.write_text("tests", encoding="utf-8")
    monkeypatch.setattr(
        main_window.QFileDialog,
        "getOpenFileNames",
        lambda *_args: ([str(first), str(second)], ""),
    )

    window._add_files()

    assert window._authenticated_username == "octocat"
    assert window.github_status_label.text() == "Connected as @octocat."
    assert "Disconnected" not in window.push_status_label.text()
    assert [target for _source, target in window.selection.upload_targets] == [
        "src/app.py",
        "tests/test_app.py",
    ]
    window.close()


def test_repository_branch_selection_loads_tree_and_detects_overwrites(
    monkeypatch, tmp_path
) -> None:
    from PySide6.QtCore import QEventLoop, QTimer

    app = QApplication.instance() or QApplication([])
    monkeypatch.setattr(main_window, "GitHubDeviceAuth", FakeAuth)
    window = main_window.MainWindow()
    window.show()
    window._authenticated_username = "octocat"
    window.repositories_combo.addItem("octocat/repo", "octocat/repo")

    local_readme = tmp_path / "README.md"
    local_readme.write_text("local", encoding="utf-8")
    window.selection.add_file(local_readme)
    window._refresh_list()

    event_loop = QEventLoop()

    def check_loaded() -> None:
        if (
            window._content_workers == set()
            and window.branch_combo.currentText() == "main"
            and window.repo_file_list.count() == 2
        ):
            event_loop.quit()
        else:
            QTimer.singleShot(10, check_loaded)

    QTimer.singleShot(0, check_loaded)
    QTimer.singleShot(5000, event_loop.quit)
    event_loop.exec()

    assert window.branch_combo.currentText() == "main"
    assert [window.repo_file_list.item(i).text() for i in range(2)] == [
        "README.md",
        "src/main.py",
    ]
    assert "already exist" in window.upload_collision_label.text()
    assert "push must confirm every overwrite" in window.upload_collision_label.text()
    window.close()


def test_authorization_code_opens_browser_once_and_can_be_copied(monkeypatch) -> None:
    app = QApplication.instance() or QApplication([])
    monkeypatch.setattr(main_window, "GitHubDeviceAuth", FakeAuth)
    opened_urls: list[str] = []
    results = iter([False, True])

    def open_url(url) -> bool:
        opened_urls.append(url.toString())
        return next(results)

    monkeypatch.setattr(main_window.QDesktopServices, "openUrl", open_url)
    window = main_window.MainWindow()
    window.show()

    window._show_authorization_code(
        "ABCD-EFGH", "https://github.com/login/device", 900
    )
    app.processEvents()

    assert opened_urls == ["https://github.com/login/device"]
    assert window.github_open_button.isVisible()
    assert window.github_copy_button.isVisible()
    assert "Could not open your browser" in window.github_status_label.text()

    window._show_authorization_code(
        "ABCD-EFGH", "https://github.com/login/device", 900
    )
    assert len(opened_urls) == 1

    window.github_copy_button.click()
    assert app.clipboard().text() == "ABCD-EFGH"
    assert window.github_status_label.text() == "GitHub authorization code copied."

    window.github_open_button.click()
    assert len(opened_urls) == 2
    assert window.github_status_label.text() == (
        "GitHub authorization page opened in your browser."
    )
    window.close()


def test_window_layout_gives_repository_and_file_panels_more_space(monkeypatch) -> None:
    from PySide6.QtWidgets import QMenuBar

    app = QApplication.instance() or QApplication([])
    monkeypatch.setattr(main_window, "GitHubDeviceAuth", FakeAuth)
    window = main_window.MainWindow()
    window.show()
    app.processEvents()

    assert window.width() == 1080
    assert window.height() == 700
    assert window.minimumWidth() >= 900
    assert window.minimumHeight() == 600
    assert window.github_group.isVisible()
    assert window.repositories_group.isVisible()
    assert window.github_group.y() >= 20
    assert window.repositories_group.y() > (
        window.github_group.y() + window.github_group.height()
    )
    assert window.file_list.isVisible()
    assert window.repo_file_list.isVisible()
    assert window.branch_combo.geometry().y() == window.repositories_combo.geometry().y()
    assert window.branch_combo.geometry().x() > window.repositories_combo.geometry().x()
    assert window.add_files_button.geometry().x() > window.repo_file_list.geometry().x()
    assert window.add_folder_button.isVisible()
    assert window.commit_message_input.isVisible()
    assert window.commit_button.isVisible()
    assert window.commit_button.text() == "Commit & Push"
    assert not hasattr(window, "push_button")
    assert window.count_label.isVisible()
    assert "qlineargradient" not in window.styleSheet()
    assert "#111827" in window.styleSheet()
    assert "#3b82f6" in window.styleSheet()
    assert "QLabel, QGroupBox::title" in window.styleSheet()
    assert window.fullscreen_action.shortcut().toString() == "F11"
    assert not hasattr(window, "fullscreen_button")
    assert not window.findChildren(QMenuBar)
    assert "margin-top: 18px;" in window.styleSheet()
    assert "padding: 14px 12px 10px 12px;" in window.styleSheet()

    window.close()


def test_fullscreen_toggle_restores_window_geometry(monkeypatch) -> None:
    app = QApplication.instance() or QApplication([])
    monkeypatch.setattr(main_window, "GitHubDeviceAuth", FakeAuth)
    window = main_window.MainWindow()
    window.show()
    app.processEvents()
    original_size = window.size()

    window.fullscreen_action.trigger()
    app.processEvents()
    assert window.isFullScreen()
    assert window.fullscreen_action.isChecked()

    window.fullscreen_action.trigger()
    app.processEvents()
    assert not window.isFullScreen()
    assert not window.fullscreen_action.isChecked()
    assert window.size() == original_size
    window.close()


def test_action_disabled_state_explains_missing_write_authorization(monkeypatch) -> None:
    app = QApplication.instance() or QApplication([])
    monkeypatch.setattr(main_window, "GitHubDeviceAuth", FakeAuth)
    window = main_window.MainWindow()
    window._authenticated_username = "octocat"

    window._update_auth_controls()
    window._refresh_action_controls()

    assert not window.commit_button.isEnabled()
    assert "Public write authorization is missing" in window.commit_button.toolTip()
    assert not window.github_write_access_button.isHidden()
    assert "Enable public push" in window.push_status_label.text()
    assert "None" not in window.push_status_label.text()
    window.close()


def test_saved_public_write_authorization_is_restored_on_startup(monkeypatch) -> None:
    from PySide6.QtCore import QEventLoop, QTimer

    class PersistedGrantAuth(FakeAuth):
        def __init__(self) -> None:
            super().__init__()
            self.token_store = PersistedStore()

        def get_username(self, _token: str) -> str:
            return "octocat"

        def list_all_public_repositories(self, _username: str) -> list[dict]:
            return []

    class PersistedStore(FakeTokenStore):
        def load_authorization(self) -> tuple[str, bool]:
            return "keyring-token", True

    app = QApplication.instance() or QApplication([])
    monkeypatch.setattr(main_window, "GitHubDeviceAuth", PersistedGrantAuth)
    window = main_window.MainWindow()
    window.show()
    event_loop = QEventLoop()

    def wait_for_restore() -> None:
        if window._auth_worker is None and window._repository_worker is None:
            event_loop.quit()
        else:
            QTimer.singleShot(10, wait_for_restore)

    QTimer.singleShot(0, wait_for_restore)
    QTimer.singleShot(5000, event_loop.quit)
    event_loop.exec()

    assert window._authenticated_username == "octocat"
    assert window._write_scope_authorized
    assert window.github_write_access_button.isHidden()
    window.close()


def test_commit_enables_when_all_prerequisites_are_satisfied(monkeypatch, tmp_path) -> None:
    app = QApplication.instance() or QApplication([])
    monkeypatch.setattr(main_window, "GitHubDeviceAuth", FakeAuth)
    window = main_window.MainWindow()
    window._authenticated_username = "octocat"
    window._write_scope_authorized = True
    window._selected_repository = "octocat/repo"
    window.repositories_combo.addItem("octocat/repo", "octocat/repo")
    window.branch_combo.addItem("main", "head-sha")
    source = tmp_path / "README.md"
    source.write_text("text", encoding="utf-8")
    window.selection.add_file(source)
    window.commit_message_input.setText("Update readme")

    window._update_controls()

    assert window.commit_button.isEnabled()
    assert "Ready to commit and push" in window.push_status_label.text()
    assert "None" not in window.push_status_label.text()
    window.close()


def test_commit_push_failure_preserves_error_and_reports_no_success(
    monkeypatch, tmp_path
) -> None:
    from PySide6.QtCore import QEventLoop, QTimer
    from PySide6.QtWidgets import QMessageBox

    app = QApplication.instance() or QApplication([])
    monkeypatch.setattr(main_window, "GitHubDeviceAuth", FakeAuth)
    window = main_window.MainWindow()
    window.show()
    window._authenticated_username = "octocat"
    window._write_scope_authorized = True
    window._selected_repository = "octocat/repo"
    window.repositories_combo.addItem("octocat/repo", "octocat/repo")
    window.branch_combo.addItem("main", "head-sha")
    source = tmp_path / "README.md"
    source.write_text("text", encoding="utf-8")
    window.selection.add_file(source)
    window.commit_message_input.setText("Update readme")
    window.github_auth.fail_commit = "commit API rejected the request"
    monkeypatch.setattr(
        main_window.QMessageBox,
        "warning",
        lambda *_args: QMessageBox.StandardButton.Yes,
    )

    window._begin_commit()
    event_loop = QEventLoop()

    def wait_for_workers() -> None:
        if window._push_preflight_worker is None and window._write_worker is None:
            event_loop.quit()
        else:
            QTimer.singleShot(10, wait_for_workers)

    QTimer.singleShot(0, wait_for_workers)
    QTimer.singleShot(5000, event_loop.quit)
    event_loop.exec()

    assert "Commit & Push failed" in window.push_status_label.text()
    assert "commit API rejected the request" in window.push_status_label.text()
    assert "GitHub confirmed" not in window.push_status_label.text()
    assert window.github_auth.write_calls == []
    window.close()


def test_commit_push_requires_confirmation_even_without_overwrites(monkeypatch) -> None:
    from PySide6.QtWidgets import QMessageBox

    app = QApplication.instance() or QApplication([])
    monkeypatch.setattr(main_window, "GitHubDeviceAuth", FakeAuth)
    window = main_window.MainWindow()
    window._selected_repository = "octocat/repo"
    window.branch_combo.addItem("main", "head-sha")
    worker = object()
    window._push_preflight_worker = worker
    preview = PublicPushPreview(
        head_sha="head-sha",
        tree_sha="tree-sha",
        existing_paths=(),
        target_paths=("new.txt",),
        overwrite_paths=(),
    )
    prompts: list[str] = []
    monkeypatch.setattr(
        main_window.QMessageBox,
        "warning",
        lambda *_args: prompts.append(_args[2]) or QMessageBox.StandardButton.No,
    )

    window._commit_preflight_ready(
        worker, "octocat/repo", "main", preview, {"new.txt": b"content"}, "Add file"
    )

    assert len(prompts) == 1
    assert "No existing repository files will be overwritten." in prompts[0]
    assert "Commit and push these files?" in prompts[0]
    assert window.github_auth.write_calls == []
    window._push_preflight_worker = None
    window.close()


def test_selected_public_files_download_with_explicit_overwrite_confirmation(
    monkeypatch, tmp_path
) -> None:
    from PySide6.QtCore import QEventLoop, QTimer
    from PySide6.QtWidgets import QMessageBox

    app = QApplication.instance() or QApplication([])
    monkeypatch.setattr(main_window, "GitHubDeviceAuth", FakeAuth)
    monkeypatch.setattr(
        main_window.QFileDialog,
        "getExistingDirectory",
        lambda *_args: str(tmp_path),
    )
    decisions = iter(
        [
            QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.Yes,
        ]
    )
    monkeypatch.setattr(
        main_window.QMessageBox, "question", lambda *_args: next(decisions)
    )

    existing = tmp_path / "src" / "main.py"
    existing.parent.mkdir()
    existing.write_bytes(b"keep until confirmed")

    window = main_window.MainWindow()
    window.show()
    window._selected_repository = "octocat/repo"
    window._selected_branch_sha = "commit-sha"
    window.repositories_combo.addItem("octocat/repo", "octocat/repo")
    window.branch_combo.addItem("main", "commit-sha")
    window.repo_file_list.addItems(["README.md", "src/main.py"])
    window.repo_file_list.item(0).setSelected(True)
    window.repo_file_list.item(1).setSelected(True)
    window._update_download_controls()

    assert window.download_selected_button.isEnabled()
    window.download_selected_button.click()
    assert existing.read_bytes() == b"keep until confirmed"
    assert "cancelled" in window.repo_files_status_label.text()

    window.download_selected_button.click()
    event_loop = QEventLoop()

    def check_finished() -> None:
        if window._download_worker is None:
            event_loop.quit()
        else:
            QTimer.singleShot(10, check_finished)

    QTimer.singleShot(0, check_finished)
    QTimer.singleShot(5000, event_loop.quit)
    event_loop.exec()

    assert (tmp_path / "README.md").read_bytes() == b"downloaded:README.md"
    assert existing.read_bytes() == b"downloaded:src/main.py"
    assert "Downloaded 2 file(s)" in window.repo_files_status_label.text()
    window.close()


def test_single_file_download_prompts_with_original_filename(
    monkeypatch, tmp_path
) -> None:
    from PySide6.QtCore import QEventLoop, QTimer

    app = QApplication.instance() or QApplication([])
    monkeypatch.setattr(main_window, "GitHubDeviceAuth", FakeAuth)
    save_dialog_calls: list[tuple] = []
    target = tmp_path / "main.py"

    def save_file(*args):
        save_dialog_calls.append(args)
        return str(target), "All files (*)"

    monkeypatch.setattr(main_window.QFileDialog, "getSaveFileName", save_file)
    window = main_window.MainWindow()
    window.show()
    window._selected_repository = "octocat/repo"
    window._selected_branch_sha = "commit-sha"
    window.branch_combo.addItem("main", "commit-sha")
    window.repo_file_list.addItem("src/main.py")
    window.repo_file_list.item(0).setSelected(True)
    window._update_download_controls()

    window._download_selected_files()
    assert save_dialog_calls[0][2] == "main.py"

    event_loop = QEventLoop()

    def check_finished() -> None:
        if window._download_worker is None:
            event_loop.quit()
        else:
            QTimer.singleShot(10, check_finished)

    QTimer.singleShot(0, check_finished)
    QTimer.singleShot(5000, event_loop.quit)
    event_loop.exec()

    assert target.read_bytes() == b"downloaded:src/main.py"
    assert "Downloaded 1 file(s)" in window.repo_files_status_label.text()
    window.close()


def test_commit_and_push_are_one_confirmed_action(
    monkeypatch, tmp_path
) -> None:
    from PySide6.QtCore import QEventLoop, QTimer
    from PySide6.QtWidgets import QMessageBox

    app = QApplication.instance() or QApplication([])
    monkeypatch.setattr(main_window, "GitHubDeviceAuth", FakeAuth)
    window = main_window.MainWindow()
    window.show()
    window._authenticated_username = "octocat"
    window._write_scope_authorized = True
    window._selected_repository = "octocat/repo"
    window.repositories_combo.blockSignals(True)
    window.repositories_combo.addItem("octocat/repo", "octocat/repo")
    window.branch_combo.addItem("main", "head-sha")
    window.repositories_combo.blockSignals(False)
    window._selected_branch_sha = "head-sha"
    source = tmp_path / "README.md"
    source.write_text("updated docs", encoding="utf-8")
    window.selection.add_file(source)
    window._refresh_list()
    window.commit_message_input.setText("Update readme")
    window._update_auth_controls()

    prompts: list[str] = []
    answers = iter([QMessageBox.StandardButton.No, QMessageBox.StandardButton.Yes])

    def confirm(*args):
        prompts.append(args[2])
        return next(answers)

    monkeypatch.setattr(main_window.QMessageBox, "warning", confirm)

    assert window.commit_button.isEnabled()
    assert window.commit_button.text() == "Commit & Push"
    window._begin_commit()
    event_loop = QEventLoop()

    def wait_for_write() -> None:
        if window._push_preflight_worker is None and window._write_worker is None:
            event_loop.quit()
        else:
            QTimer.singleShot(10, wait_for_write)

    QTimer.singleShot(0, wait_for_write)
    QTimer.singleShot(5000, event_loop.quit)
    event_loop.exec()

    auth = window.github_auth
    assert auth is not None
    assert len(prompts) == 1
    assert "octocat/repo" in prompts[0]
    assert "main" in prompts[0]
    assert "Update readme" in prompts[0]
    assert "README.md" in prompts[0]
    assert "Existing repository files that will be overwritten" in prompts[0]
    assert auth.write_calls == []
    assert "cancelled" in window.push_status_label.text().casefold()

    window._begin_commit()
    event_loop = QEventLoop()
    QTimer.singleShot(0, wait_for_write)
    QTimer.singleShot(5000, event_loop.quit)
    event_loop.exec()

    assert len(prompts) == 2
    assert len(auth.write_calls) == 1
    assert auth.write_calls[0][0:2] == ("octocat/repo", "main")
    assert auth.write_calls[0][3] == {"README.md": b"updated docs"}
    assert auth.write_calls[0][4] == "Update readme"
    assert "GitHub confirmed commit and branch update" in window.push_status_label.text()
    assert window.commit_button.isEnabled()
    window.close()
