from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QAction, QCloseEvent, QDesktopServices, QKeyEvent
from PySide6.QtWidgets import (
    QApplication,
    QAbstractItemView,
    QComboBox,
    QFileDialog,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from minigit.auth_worker import (
    AuthWorker,
    BranchWorker,
    CommitPushWorker,
    DownloadWorker,
    PushPreflightWorker,
    RepositoryWorker,
    TreeWorker,
)
from minigit.github_auth import CLIENT_ID_ENV, GitHubAuthError, GitHubDeviceAuth
from minigit.selection import FileSelection


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("MiniGit")
        self.resize(1080, 700)
        self.setMinimumSize(900, 600)
        self._pre_fullscreen_geometry: bytes | None = None
        self._pre_fullscreen_state = Qt.WindowState.WindowNoState
        self._pre_fullscreen_size: tuple[int, int] | None = None
        self._pre_fullscreen_position: tuple[int, int] | None = None
        self.selection = FileSelection()
        self._auth_worker: AuthWorker | None = None
        self._repository_worker: RepositoryWorker | None = None
        self._repository_request_id = 0
        self._repository_reload_pending = False
        self._selected_repository: str | None = None
        self._selected_branch_sha: str | None = None
        self._content_request_id = 0
        self._content_workers: set[BranchWorker | TreeWorker] = set()
        self._download_worker: DownloadWorker | None = None
        self._push_preflight_worker: PushPreflightWorker | None = None
        self._write_worker: CommitPushWorker | None = None
        self._write_scope_authorized = False
        self._approval_url: str | None = None
        self._authorization_code: str | None = None
        self._browser_open_attempted = False
        self._authenticated_username: str | None = None
        self._has_stored_token = False
        self._auth_status_message = ""
        stored_token: str | None = None
        stored_public_repo_scope = False
        try:
            self.github_auth: GitHubDeviceAuth | None = GitHubDeviceAuth()
            stored_token, stored_public_repo_scope = (
                self.github_auth.token_store.load_authorization()
            )
            self._has_stored_token = stored_token is not None
        except GitHubAuthError as error:
            self.github_auth = None
            self._auth_status_message = str(error)

        self.file_list = QListWidget()
        self.file_list.setSelectionMode(QListWidget.SelectionMode.ExtendedSelection)
        self.file_list.setAlternatingRowColors(True)
        self.file_list.itemSelectionChanged.connect(self._update_controls)

        self.count_label = QLabel("0 files selected")
        self.remove_button = QPushButton("Remove Selected")
        self.remove_button.clicked.connect(self._remove_selected)

        self.add_files_button = QPushButton("Add Files")
        self.add_files_button.clicked.connect(self._add_files)
        self.add_folder_button = QPushButton("Add Folder")
        self.add_folder_button.clicked.connect(self._add_folder)

        self.commit_message_input = QLineEdit()
        self.commit_message_input.setPlaceholderText("Commit message / comment")
        self.commit_message_input.textChanged.connect(self._update_controls)
        self.commit_button = QPushButton("Commit & Push")
        self.commit_button.clicked.connect(self._begin_commit)
        self.commit_button.setEnabled(False)
        self.push_status_label = QLabel("Connect GitHub to commit and push selected files.")
        self.push_status_label.setWordWrap(True)

        self.github_group = QGroupBox("GitHub account")
        self.github_status_label = QLabel()
        self.github_status_label.setWordWrap(True)
        self.github_code_label = QLabel()
        self.github_code_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        self.github_code_label.hide()
        self.github_open_button = QPushButton("Open GitHub to authorize")
        self.github_open_button.clicked.connect(self._open_authorization_page)
        self.github_open_button.hide()
        self.github_copy_button = QPushButton("Copy code")
        self.github_copy_button.clicked.connect(self._copy_authorization_code)
        self.github_copy_button.hide()
        self.github_connect_button = QPushButton("Connect GitHub")
        self.github_connect_button.clicked.connect(self._connect_github)
        self.github_cancel_button = QPushButton("Cancel sign-in")
        self.github_cancel_button.clicked.connect(self._cancel_github_auth)
        self.github_cancel_button.hide()
        self.github_disconnect_button = QPushButton("Disconnect")
        self.github_disconnect_button.clicked.connect(self._disconnect_github)
        self.github_disconnect_button.hide()
        self.github_write_access_button = QPushButton("Enable public push")
        self.github_write_access_button.setToolTip(
            "Reauthorize MiniGit with public_repo access. The saved token is "
            "replaced only after GitHub approves the new authorization."
        )
        self.github_write_access_button.clicked.connect(self._connect_github)
        self.github_write_access_button.hide()

        auth_controls = QHBoxLayout()
        auth_controls.addWidget(self.github_connect_button)
        auth_controls.addWidget(self.github_open_button)
        auth_controls.addWidget(self.github_copy_button)
        auth_controls.addWidget(self.github_cancel_button)
        auth_controls.addWidget(self.github_write_access_button)
        auth_controls.addWidget(self.github_disconnect_button)
        auth_controls.insertWidget(0, self.github_status_label, 1)
        auth_layout = QVBoxLayout()
        auth_layout.setContentsMargins(10, 8, 10, 8)
        auth_layout.addWidget(self.github_code_label)
        auth_layout.addLayout(auth_controls)
        self.github_group.setLayout(auth_layout)

        self.repositories_group = QGroupBox("Public repositories")
        self.repositories_status_label = QLabel("Connect GitHub to load public repositories.")
        self.repositories_status_label.setWordWrap(True)
        self.repositories_combo = QComboBox()
        self.repositories_combo.setEnabled(False)
        self.repositories_combo.currentIndexChanged.connect(
            self._repository_selection_changed
        )
        self.repositories_refresh_button = QPushButton("Refresh")
        self.repositories_refresh_button.clicked.connect(self._load_public_repositories)
        self.repositories_refresh_button.setEnabled(False)
        repository_layout = QHBoxLayout()
        repository_layout.setSpacing(8)
        repository_layout.addWidget(QLabel("Repository"))
        repository_layout.addWidget(self.repositories_combo, 1)
        repository_layout.addWidget(self.repositories_refresh_button)
        repositories_layout = QVBoxLayout()
        self.branch_status_label = QLabel("Select a repository to load branches.")
        self.branch_status_label.setWordWrap(True)
        self.branch_combo = QComboBox()
        self.branch_combo.setEnabled(False)
        self.branch_combo.currentIndexChanged.connect(self._branch_selection_changed)
        self.branch_refresh_button = QPushButton("Refresh branches")
        self.branch_refresh_button.setEnabled(False)
        self.branch_refresh_button.clicked.connect(self._load_repository_branches)
        branch_layout = QHBoxLayout()
        branch_layout.setSpacing(8)
        branch_layout.addWidget(QLabel("Branch"))
        branch_layout.addWidget(self.branch_combo, 1)
        branch_layout.addWidget(self.branch_refresh_button)
        repository_layout.addLayout(branch_layout)
        repositories_layout.addLayout(repository_layout)
        status_layout = QHBoxLayout()
        status_layout.addWidget(self.repositories_status_label, 1)
        status_layout.addWidget(self.branch_status_label, 1)
        repositories_layout.addLayout(status_layout)
        repositories_layout.setContentsMargins(10, 8, 10, 8)
        self.repositories_group.setLayout(repositories_layout)

        self.repo_file_list = QListWidget()
        self.repo_file_list.setAlternatingRowColors(True)
        self.repo_file_list.setSelectionMode(
            QAbstractItemView.SelectionMode.ExtendedSelection
        )
        self.repo_file_list.itemSelectionChanged.connect(
            self._update_download_controls
        )
        self.repo_files_status_label = QLabel("Select a branch to view repository files.")
        self.repo_files_status_label.setWordWrap(True)
        self.download_selected_button = QPushButton("Download Selected")
        self.download_selected_button.clicked.connect(self._download_selected_files)
        self.download_selected_button.setEnabled(False)
        self.local_files_status_label = QLabel()
        self.upload_collision_label = QLabel("No target-path collisions detected.")
        self.upload_collision_label.setWordWrap(True)
        file_panels = QHBoxLayout()
        repo_file_panel = QVBoxLayout()
        remote_controls = QHBoxLayout()
        remote_controls.addWidget(QLabel("Current repository files"))
        remote_controls.addStretch()
        remote_controls.addWidget(self.download_selected_button)
        repo_file_panel.addLayout(remote_controls)
        repo_file_panel.addWidget(self.repo_files_status_label)
        repo_file_panel.addWidget(self.repo_file_list)
        upload_file_panel = QVBoxLayout()
        local_controls = QHBoxLayout()
        local_controls.addWidget(QLabel("Local files to commit and push"))
        local_controls.addStretch()
        local_controls.addWidget(self.add_files_button)
        local_controls.addWidget(self.add_folder_button)
        local_controls.addWidget(self.remove_button)
        upload_file_panel.addLayout(local_controls)
        upload_file_panel.addWidget(self.local_files_status_label)
        upload_file_panel.addWidget(self.upload_collision_label)
        upload_file_panel.addWidget(self.file_list)
        file_panels.addLayout(repo_file_panel, 1)
        file_panels.addLayout(upload_file_panel, 1)
        file_panels.setSpacing(14)

        footer = QHBoxLayout()
        footer.addWidget(self.count_label)
        footer.addStretch()
        footer.addWidget(self.commit_message_input, 1)
        footer.addWidget(self.commit_button)

        layout = QVBoxLayout()
        layout.setContentsMargins(12, 24, 12, 12)
        layout.setSpacing(8)
        layout.addWidget(self.github_group)
        layout.addWidget(self.repositories_group)
        layout.addLayout(file_panels, 1)
        layout.addWidget(self.push_status_label)
        layout.addLayout(footer)

        container = QWidget()
        container.setLayout(layout)
        self.setCentralWidget(container)
        self.fullscreen_action = QAction("Full Screen", self)
        self.fullscreen_action.setCheckable(True)
        self.fullscreen_action.setShortcut("F11")
        self.fullscreen_action.setShortcutContext(
            Qt.ShortcutContext.WindowShortcut
        )
        self.fullscreen_action.toggled.connect(self._set_fullscreen)
        self.addAction(self.fullscreen_action)
        self.setStyleSheet(
            """
            QMainWindow, QWidget {
                background-color: #111827;
                color: #e5e7eb;
                font-family: "Segoe UI";
                font-size: 13px;
            }
            QLabel, QGroupBox::title {
                background: transparent;
            }
            QGroupBox {
                background-color: #1f2937;
                border: 1px solid #374151;
                border-radius: 8px;
                margin-top: 18px;
                padding: 14px 12px 10px 12px;
                font-weight: 600;
            }
            QGroupBox::title {
                subcontrol-origin: margin;
                subcontrol-position: top left;
                top: 0px;
                left: 12px;
                padding: 0 7px;
                color: #dbeafe;
            }
            QListWidget {
                background-color: #111827;
                alternate-background-color: #172131;
                border: 1px solid #374151;
                border-radius: 6px;
                padding: 4px;
            }
            QListWidget::item {
                padding: 5px;
                border-radius: 3px;
            }
            QListWidget::item:selected {
                background-color: #2563eb;
                color: #ffffff;
            }
            QPushButton {
                background-color: #263449;
                color: #e5e7eb;
                border: 1px solid #41516a;
                border-radius: 6px;
                padding: 7px 12px;
            }
            QPushButton:hover:enabled {
                background-color: #334766;
                border-color: #3b82f6;
            }
            QPushButton:pressed:enabled {
                background-color: #1d4ed8;
                padding-top: 8px;
                padding-bottom: 6px;
            }
            QPushButton:focus, QComboBox:focus, QLineEdit:focus, QListWidget:focus {
                border: 2px solid #3b82f6;
            }
            QPushButton:disabled {
                color: #8b95a5;
                background-color: #202a39;
                border-color: #344052;
            }
            QComboBox, QLineEdit {
                background-color: #111827;
                color: #e5e7eb;
                border: 1px solid #41516a;
                border-radius: 5px;
                padding: 6px 8px;
                selection-background-color: #2563eb;
                selection-color: #ffffff;
            }
            QComboBox:disabled {
                color: #8792a2;
                background-color: #202a39;
            }
            QStatusBar {
                background-color: #1f2937;
                color: #aab4c3;
            }
            """
        )
        self._update_controls()
        self._update_auth_controls()
        if self._has_stored_token and self.github_auth is not None:
            self.github_status_label.setText("Checking the saved GitHub connection...")
            self._start_auth_worker(
                AuthWorker(
                    self.github_auth,
                    restore_token=stored_token,
                    restore_public_repo_scope=stored_public_repo_scope,
                )
            )
        elif self._auth_status_message:
            self.github_status_label.setText(self._auth_status_message)
        else:
            self.github_status_label.setText("Not connected to GitHub.")
        self._update_auth_controls()
        if self._authenticated_username is None:
            self.push_status_label.setText("Connect GitHub to push selected files.")
        self._update_controls()

    def _add_files(self) -> None:
        paths, _ = QFileDialog.getOpenFileNames(self, "Select files")
        added = 0
        errors: list[str] = []
        for path in paths:
            try:
                added += self.selection.add_file(path)
            except (OSError, ValueError) as error:
                errors.append(f"{path}: {error}")
        self._refresh_list()
        if errors:
            self._show_errors("Some files could not be added", errors)
        elif paths and added == 0:
            self.statusBar().showMessage("No new files were added.", 4000)

    def _add_folder(self) -> None:
        path = QFileDialog.getExistingDirectory(self, "Select a folder")
        if not path:
            return
        try:
            added = self.selection.add_directory(path)
        except (OSError, ValueError) as error:
            self._show_errors("Could not add folder", [str(error)])
            return
        self._refresh_list()
        self.statusBar().showMessage(f"Added {added} new files.", 4000)

    def _remove_selected(self) -> None:
        selected_items = self.file_list.selectedItems()
        for item in selected_items:
            path = Path(item.data(Qt.ItemDataRole.UserRole))
            self.selection.remove_file(path)
            self.file_list.takeItem(self.file_list.row(item))
        self._update_controls()

    def _refresh_list(self) -> None:
        self.file_list.clear()
        for path, target in self.selection.upload_targets:
            item = QListWidgetItem(f"{target}  <-  {path}")
            item.setData(Qt.ItemDataRole.UserRole, str(path))
            item.setToolTip(f"Repository target: {target}\nLocal source: {path}")
            self.file_list.addItem(item)
        self._update_controls()

    def _update_controls(self) -> None:
        count = self.selection.count
        noun = "file" if count == 1 else "files"
        self.count_label.setText(f"{count} {noun} selected")
        self.remove_button.setEnabled(bool(self.file_list.selectedItems()))
        self.local_files_status_label.setText(
            f"{count} local {'file' if count == 1 else 'files'} selected."
        )
        self._update_upload_conflicts()
        self._refresh_action_controls()

    def _commit_disabled_reason(self) -> str | None:
        if self.github_auth is None:
            return "GitHub service is unavailable."
        if self._authenticated_username is None:
            return "Not connected to GitHub."
        if not self._write_scope_authorized:
            return "Public write authorization is missing; select Enable public push."
        if self._selected_repository is None:
            return "No public repository selected."
        if self.branch_combo.currentIndex() < 0:
            return "No target branch selected."
        if self.selection.count == 0:
            return "No local files selected."
        if not self.commit_message_input.text().strip():
            return "Commit message is empty."
        if self.selection.target_collisions:
            return "Resolve duplicate local target paths."
        if self._auth_worker is not None:
            return "GitHub authorization is in progress."
        if (
            self._push_preflight_worker is not None
            or self._write_worker is not None
        ):
            return "Commit & Push is already in progress."
        return None

    def _refresh_action_controls(self) -> None:
        commit_reason = self._commit_disabled_reason()
        self.commit_button.setEnabled(commit_reason is None)
        self.commit_button.setToolTip(
            commit_reason
            or "Create a commit and update the selected public branch after confirmation."
        )
        if self._write_worker is not None:
            self.push_status_label.setText(
                "Creating commit and updating the branch; checking the branch head."
            )
        elif self._push_preflight_worker is None:
            if self._authenticated_username and not self._write_scope_authorized:
                self.push_status_label.setText(
                    "Connected, but public write authorization is required. Select "
                    "Enable public push to reauthorize; the saved token changes "
                    "only after successful approval."
                )
            elif commit_reason is None:
                self.push_status_label.setText(
                    "Ready to commit and push after reviewing the overwrite confirmation."
                )
            else:
                self.push_status_label.setText(
                    f"Commit & Push unavailable: {commit_reason}"
                )

    def _update_download_controls(self) -> None:
        self.download_selected_button.setEnabled(
            bool(self.repo_file_list.selectedItems())
            and self._selected_repository is not None
            and self.branch_combo.currentIndex() >= 0
            and self._download_worker is None
        )

    def _download_selected_files(self) -> None:
        if (
            self.github_auth is None
            or self._selected_repository is None
            or self.branch_combo.currentIndex() < 0
        ):
            return
        paths = [item.text() for item in self.repo_file_list.selectedItems()]
        if not paths:
            return
        single_file_target: Path | None = None
        if len(paths) == 1:
            remote_path = paths[0]
            filename = remote_path.rsplit("/", 1)[-1]
            if not filename or filename in {".", ".."} or "\\" in filename:
                self._show_errors(
                    "Invalid repository file path",
                    [f"Cannot save this repository path safely: {remote_path}"],
                )
                return
            save_path, _ = QFileDialog.getSaveFileName(
                self,
                "Save repository file",
                filename,
                "All files (*)",
            )
            if not save_path:
                return
            single_file_target = Path(save_path)
            destination = single_file_target.parent
        else:
            directory = QFileDialog.getExistingDirectory(
                self, "Choose download destination"
            )
            if not directory:
                return
            destination = Path(directory)
        try:
            root = destination.resolve(strict=True)
            targets: list[tuple[str, Path]] = []
            for remote_path in paths:
                parts = remote_path.split("/")
                if (
                    not remote_path
                    or remote_path.startswith("/")
                    or "\\" in remote_path
                    or any(part in {"", ".", ".."} for part in parts)
                ):
                    raise ValueError(f"Unsafe repository file path: {remote_path}")
                target = (
                    single_file_target.resolve(strict=False)
                    if single_file_target is not None
                    else root.joinpath(*parts).resolve(strict=False)
                )
                target.relative_to(root)
                if target.exists() and target.is_dir():
                    raise ValueError(
                        f"A folder already exists at the target file path: {remote_path}"
                    )
                targets.append((remote_path, target))
        except (OSError, ValueError) as error:
            self._show_errors("Invalid download destination", [str(error)])
            return

        overwrites = {
            str(target) for _remote_path, target in targets if target.exists()
        }
        if overwrites:
            overwrite_names = [
                remote_path
                for remote_path, target in targets
                if str(target) in overwrites
            ]
            answer = QMessageBox.question(
                self,
                "Confirm local file overwrite",
                "These local files already exist and will be replaced:\n\n"
                + "\n".join(overwrite_names)
                + "\n\nContinue?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                self.repo_files_status_label.setText(
                    "Download cancelled; no local files were changed."
                )
                return

        worker = DownloadWorker(
            self.github_auth,
            self._selected_repository,
            self.branch_combo.currentText(),
            paths,
            root,
            overwrites,
            single_file_target=single_file_target,
        )
        self._download_worker = worker
        worker.completed.connect(
            lambda downloaded, errors: self._download_completed(
                worker, downloaded, errors
            )
        )
        worker.finished.connect(lambda: self._download_worker_finished(worker))
        self.repo_files_status_label.setText(
            f"Downloading {len(paths)} selected file(s)..."
        )
        self._update_download_controls()
        worker.start()

    def _download_completed(
        self,
        worker: DownloadWorker,
        downloaded: list[str],
        errors: list[str],
    ) -> None:
        if self._download_worker is not worker:
            return
        if errors:
            self.repo_files_status_label.setText(
                f"Downloaded {len(downloaded)} file(s); {len(errors)} failed."
            )
            self._show_errors("Some files could not be downloaded", errors)
        else:
            self.repo_files_status_label.setText(
                f"Downloaded {len(downloaded)} file(s) to the selected folder."
            )

    def _download_worker_finished(self, worker: DownloadWorker) -> None:
        if self._download_worker is worker:
            self._download_worker = None
            self._update_download_controls()
            worker.deleteLater()

    def _update_upload_conflicts(self) -> None:
        targets = self.selection.upload_targets
        duplicate_targets = self.selection.target_collisions
        remote_paths = {
            self.repo_file_list.item(index).text().casefold()
            for index in range(self.repo_file_list.count())
        }
        existing: list[str] = []
        structural: list[str] = []
        for _source, target in targets:
            normalized = target.casefold()
            if normalized in remote_paths:
                existing.append(target)
            elif any(
                path.startswith(normalized + "/")
                or normalized.startswith(path + "/")
                for path in remote_paths
            ):
                structural.append(target)

        parts: list[str] = []
        if duplicate_targets:
            parts.append(
                f"{len(duplicate_targets)} local target-path collision(s) need resolution"
            )
        if existing:
            parts.append(
                f"{len(set(existing))} selected target(s) already exist and would overwrite files"
            )
        if structural:
            parts.append(
                f"{len(set(structural))} selected target(s) conflict with repository file/directory paths"
            )
        self.upload_collision_label.setText(
            "; ".join(parts) + ". A push must confirm every overwrite."
            if parts
            else "No target-path collisions detected. A push will confirm any overwrite."
        )

    def _begin_commit(self) -> None:
        reason = self._commit_disabled_reason()
        if reason is not None:
            self.push_status_label.setText(f"Commit & Push unavailable: {reason}")
            self._refresh_action_controls()
            return
        if self.github_auth is None:
            self.push_status_label.setText(
                "Commit & Push unavailable: GitHub service is unavailable."
            )
            return
        repository = self._selected_repository
        branch = self.branch_combo.currentText()
        message = self.commit_message_input.text().strip()
        self.push_status_label.setText(f"Reviewing {repository}:{branch} before commit & push...")
        worker = PushPreflightWorker(
            self.github_auth, repository, branch, self.selection.upload_targets
        )
        self._push_preflight_worker = worker
        worker.prepared.connect(
            lambda preview, files: self._commit_preflight_ready(
                worker, repository, branch, preview, files, message
            )
        )
        worker.failed.connect(
            lambda error: self._commit_preflight_failed(worker, error)
        )
        worker.finished.connect(
            lambda: self._commit_preflight_finished(worker)
        )
        self._refresh_action_controls()
        worker.start()

    def _commit_preflight_ready(
        self, worker, repository: str, branch: str, preview, files, message: str
    ) -> None:
        if self._push_preflight_worker is not worker:
            return
        if (
            self._selected_repository != repository
            or self.branch_combo.currentText() != branch
        ):
            self.push_status_label.setText(
                "Commit & Push cancelled because the selected repository or branch changed."
            )
            return
        if preview.overwrite_paths:
            overwrite_detail = "\n".join(
                f"  - {path}" for path in preview.overwrite_paths
            )
        else:
            overwrite_detail = "  None"
        target_detail = "\n".join(f"  - {path}" for path in preview.target_paths)
        overwrite_summary = (
            "Existing repository files that will be overwritten:\n"
            f"{overwrite_detail}"
            if preview.overwrite_paths
            else "No existing repository files will be overwritten."
        )
        answer = QMessageBox.warning(
            self,
            "Confirm commit and push",
            f"MiniGit will commit the selected files to {repository} on branch {branch}.\n\n"
            f"Commit message:\n{message}\n\n"
            f"Files to add or update:\n{target_detail}\n\n"
            f"{overwrite_summary}\n\nCommit and push these files?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            self.push_status_label.setText(
                "Commit & Push cancelled; no repository changes were made."
            )
            return
        if self.github_auth is None or not self._write_scope_authorized:
            self.push_status_label.setText(
                "Commit & Push cancelled because public write authorization is unavailable."
            )
            return
        write_worker = CommitPushWorker(
            self.github_auth,
            repository,
            branch,
            preview,
            files,
            message,
        )
        self._write_worker = write_worker
        write_worker.completed.connect(
            lambda commit_sha: self._write_completed(write_worker, commit_sha)
        )
        write_worker.failed.connect(
            lambda error: self._write_failed(write_worker, error)
        )
        write_worker.finished.connect(
            lambda: self._write_worker_finished(write_worker)
        )
        self.push_status_label.setText(
            "Creating commit and updating the branch after confirmation..."
        )
        self._refresh_action_controls()
        write_worker.start()

    def _commit_preflight_failed(self, worker, error: str) -> None:
        if self._push_preflight_worker is worker:
            self.push_status_label.setText(
                f"Commit & Push failed: {self._push_error_text(error)}"
            )

    def _commit_preflight_finished(self, worker) -> None:
        if self._push_preflight_worker is worker:
            status = self.push_status_label.text()
            self._push_preflight_worker = None
            self._update_controls()
            self._update_auth_controls()
            if status.startswith("Commit & Push cancelled") or status.startswith(
                "Commit & Push failed"
            ):
                self.push_status_label.setText(status)
            worker.deleteLater()

    def _write_completed(self, worker: CommitPushWorker, commit_sha: str) -> None:
        if self._write_worker is not worker:
            return
        self.push_status_label.setText(
            f"GitHub confirmed commit and branch update: {commit_sha[:12]}."
        )
        self._load_repository_branches()

    def _write_failed(self, worker: CommitPushWorker, error: str) -> None:
        if self._write_worker is worker:
            self.push_status_label.setText(
                f"Commit & Push failed: {self._push_error_text(error)}"
            )

    def _write_worker_finished(self, worker: CommitPushWorker) -> None:
        if self._write_worker is worker:
            status = self.push_status_label.text()
            self._write_worker = None
            self._refresh_action_controls()
            if status.startswith("GitHub confirmed") or status.startswith("Commit & Push failed"):
                self.push_status_label.setText(status)
            worker.deleteLater()

    @staticmethod
    def _push_error_text(error: str) -> str:
        lowered = error.casefold()
        if "403" in lowered or "resource not accessible" in lowered or "insufficient" in lowered:
            return (
                f"{error} Reauthorize with Enable public push to grant the "
                "public_repo scope. No successful push was confirmed."
            )
        if "branch changed" in lowered or "reference update failed" in lowered:
            return f"{error} Refresh the branch and review the push again."
        return error

    def _show_errors(self, title: str, errors: list[str]) -> None:
        QMessageBox.warning(self, title, "\n".join(errors))

    def _connect_github(self) -> None:
        if self.github_auth is None:
            self.github_status_label.setText(self._auth_status_message)
            return
        if not self.github_auth.client_id:
            self.github_status_label.setText(
                f"Set {CLIENT_ID_ENV} to your GitHub OAuth app client ID, then restart MiniGit."
            )
            return
        self.github_code_label.clear()
        self.github_open_button.hide()
        self.github_copy_button.hide()
        self._approval_url = None
        self._authorization_code = None
        self._browser_open_attempted = False
        self.github_status_label.setText(
            "Requesting GitHub approval for public repository writes..."
            if self._authenticated_username is not None
            else "Requesting a GitHub sign-in code..."
        )
        self._start_auth_worker(AuthWorker(self.github_auth))

    def _start_auth_worker(self, worker: AuthWorker) -> None:
        self._auth_worker = worker
        worker.authorization_ready.connect(self._show_authorization_code)
        worker.status_changed.connect(self.github_status_label.setText)
        worker.authenticated.connect(
            lambda username: self._github_authenticated(
                username, reauthorized=worker.has_public_repo_scope
            )
        )
        worker.failed.connect(self._github_auth_failed)
        worker.finished.connect(lambda: self._auth_worker_finished(worker))
        self._update_auth_controls()
        worker.start()

    def _show_authorization_code(
        self, user_code: str, verification_uri: str, expires_in: int
    ) -> None:
        self._approval_url = verification_uri
        self._authorization_code = user_code
        self.github_code_label.setText(
            f"On GitHub, enter this code: {user_code}\n"
            f"The code expires in about {max(1, expires_in // 60)} minute(s)."
        )
        self.github_code_label.show()
        self.github_open_button.show()
        self.github_copy_button.show()
        self.github_status_label.setText(
            "Approve MiniGit in your browser. Waiting for GitHub approval..."
        )
        if self._browser_open_attempted:
            return
        self._browser_open_attempted = True
        try:
            opened = QDesktopServices.openUrl(QUrl(verification_uri))
        except Exception:
            opened = False
        if not opened:
            self.github_status_label.setText(
                "Could not open your browser automatically. Use Open GitHub to authorize."
            )

    def _open_authorization_page(self) -> None:
        if self._approval_url is not None:
            try:
                opened = QDesktopServices.openUrl(QUrl(self._approval_url))
            except Exception:
                opened = False
            if not opened:
                self.github_status_label.setText(
                    "Could not open the browser. Copy the URL and open it manually."
                )
            else:
                self.github_status_label.setText(
                    "GitHub authorization page opened in your browser."
                )

    def _copy_authorization_code(self) -> None:
        if self._authorization_code:
            QApplication.clipboard().setText(self._authorization_code)
            self.github_status_label.setText("GitHub authorization code copied.")

    def _github_authenticated(self, username: str, *, reauthorized: bool) -> None:
        self._authenticated_username = username
        self._write_scope_authorized = reauthorized
        self._has_stored_token = True
        self._auth_status_message = ""
        self.github_status_label.setText(
            f"Connected as @{username}; public repository access enabled."
            if reauthorized
            else (
                f"Connected as @{username}, but public write authorization is missing. "
                "Select Enable public push to reauthorize."
            )
        )
        self.push_status_label.setText(
            "Public push access enabled."
            if reauthorized
            else (
                "Connected, but public write authorization is required. Select "
                "Enable public push to reauthorize; the saved token changes "
                "only after successful approval."
            )
        )
        self._refresh_action_controls()
        self._update_controls()
        self.github_code_label.hide()
        self.github_open_button.hide()
        self.github_copy_button.hide()
        self._authorization_code = None
        self._approval_url = None
        self._load_public_repositories()

    def _github_auth_failed(self, message: str) -> None:
        self._auth_status_message = message
        self.github_status_label.setText(
            f"Reauthorization failed: {message}"
            if self._authenticated_username is not None
            else message
        )
        self.github_code_label.hide()
        self.github_open_button.hide()
        self.github_copy_button.hide()
        self._authorization_code = None
        self._approval_url = None

    def _disconnect_github(self) -> None:
        if self.github_auth is None:
            return
        try:
            self.github_auth.token_store.delete()
        except GitHubAuthError as error:
            self.github_status_label.setText(str(error))
            return
        self._authenticated_username = None
        self._write_scope_authorized = False
        self._has_stored_token = False
        self._auth_status_message = ""
        self.github_code_label.clear()
        self.github_code_label.hide()
        self.github_open_button.hide()
        self.github_copy_button.hide()
        self._authorization_code = None
        self._approval_url = None
        self._repository_request_id += 1
        self._content_request_id += 1
        self._repository_reload_pending = False
        self._selected_repository = None
        self._selected_branch_sha = None
        self.repositories_combo.clear()
        self.repositories_combo.setEnabled(False)
        self.branch_combo.clear()
        self.branch_combo.setEnabled(False)
        self.branch_refresh_button.setEnabled(False)
        self.branch_status_label.setText("Connect GitHub to load repositories.")
        self.repo_file_list.clear()
        self.repo_files_status_label.setText("Connect GitHub to view repository files.")
        self.repositories_status_label.setText("Connect GitHub to load public repositories.")
        self.github_status_label.setText("Disconnected from GitHub.")
        self.push_status_label.setText("Disconnected from GitHub.")
        self._update_auth_controls()

    def _cancel_github_auth(self) -> None:
        if self._auth_worker is not None:
            self._auth_worker.cancel()
            self.github_status_label.setText("Cancelling GitHub sign-in...")

    def _auth_worker_finished(self, worker: AuthWorker) -> None:
        if self._auth_worker is worker:
            self._auth_worker = None
            if worker.was_cancelled:
                self.github_status_label.setText("GitHub sign-in was cancelled.")
                self.github_code_label.hide()
                self.github_open_button.hide()
                self.github_copy_button.hide()
                self._authorization_code = None
                self._approval_url = None
            self._update_auth_controls()
            worker.deleteLater()

    def _update_auth_controls(self) -> None:
        connected = self._authenticated_username is not None
        busy = self._auth_worker is not None
        push_busy = (
            self._push_preflight_worker is not None or self._write_worker is not None
        )
        self.github_connect_button.setVisible(not connected)
        self.github_disconnect_button.setVisible(connected or self._has_stored_token)
        self.github_write_access_button.setVisible(
            connected and not self._write_scope_authorized
        )
        self.github_cancel_button.setVisible(busy)
        self.github_connect_button.setEnabled(not busy and self.github_auth is not None)
        self.github_disconnect_button.setEnabled(
            not busy and not push_busy and self.github_auth is not None
        )
        self.github_write_access_button.setEnabled(
            not busy
            and not push_busy
            and self.github_auth is not None
        )
        self.repositories_refresh_button.setEnabled(
            connected
            and self._repository_worker is None
            and self.github_auth is not None
        )
        self.repositories_combo.setEnabled(
            connected and self.repositories_combo.count() > 0
        )
        self._update_controls()

    def _load_public_repositories(self) -> None:
        if self.github_auth is None or self._authenticated_username is None:
            return
        if self._repository_worker is not None:
            self._repository_reload_pending = True
            return
        self._repository_request_id += 1
        request_id = self._repository_request_id
        worker = RepositoryWorker(self.github_auth, self._authenticated_username)
        self._repository_worker = worker
        self.repositories_status_label.setText("Loading public repositories owned by this account...")
        self.repositories_refresh_button.setEnabled(False)
        worker.repositories_loaded.connect(
            lambda repositories: self._public_repositories_loaded(
                worker, request_id, repositories
            )
        )
        worker.failed.connect(
            lambda message: self._public_repositories_failed(worker, request_id, message)
        )
        worker.finished.connect(lambda: self._repository_worker_finished(worker))
        worker.start()

    def _public_repositories_loaded(
        self, worker: RepositoryWorker, request_id: int, repositories: list[dict]
    ) -> None:
        if self._repository_worker is not worker or self._repository_request_id != request_id:
            return
        selected_name = self.repositories_combo.currentData()
        self.repositories_combo.blockSignals(True)
        self.repositories_combo.clear()
        for repository in repositories:
            full_name = repository["full_name"]
            self.repositories_combo.addItem(full_name, full_name)
        if selected_name:
            index = self.repositories_combo.findData(selected_name)
            if index >= 0:
                self.repositories_combo.setCurrentIndex(index)
        self.repositories_combo.blockSignals(False)
        count = len(repositories)
        self.repositories_status_label.setText(
            f"{count} public {'repository' if count == 1 else 'repositories'} available."
            if count
            else "No public repositories found."
        )
        self.repositories_combo.setEnabled(count > 0)
        if count:
            self._repository_selection_changed(self.repositories_combo.currentIndex())
        else:
            self._repository_selection_changed(-1)

    def _repository_selection_changed(self, index: int) -> None:
        repository = self.repositories_combo.itemData(index) if index >= 0 else None
        if repository == self._selected_repository:
            return
        self._selected_repository = repository
        self._selected_branch_sha = None
        self._content_request_id += 1
        self.branch_combo.blockSignals(True)
        self.branch_combo.clear()
        self.branch_combo.blockSignals(False)
        self.branch_combo.setEnabled(False)
        self.branch_refresh_button.setEnabled(bool(repository))
        self.repo_file_list.clear()
        self.repo_files_status_label.setText(
            "Select a branch to view repository files."
            if repository
            else "Select a repository to view its files."
        )
        self._update_upload_conflicts()
        if repository:
            self._load_repository_branches()

    def _load_repository_branches(self) -> None:
        if self.github_auth is None or self._selected_repository is None:
            return
        self._content_request_id += 1
        request_id = self._content_request_id
        self.branch_status_label.setText("Loading repository branches...")
        self.branch_combo.setEnabled(False)
        self.branch_refresh_button.setEnabled(False)
        worker = BranchWorker(self.github_auth, self._selected_repository)
        self._content_workers.add(worker)
        worker.branches_loaded.connect(
            lambda branches: self._repository_branches_loaded(
                worker, request_id, branches
            )
        )
        worker.failed.connect(
            lambda message: self._repository_branches_failed(
                worker, request_id, message
            )
        )
        worker.finished.connect(lambda: self._content_worker_finished(worker))
        worker.start()

    def _repository_branches_loaded(
        self, worker: BranchWorker, request_id: int, branches: list[dict[str, str]]
    ) -> None:
        if (
            worker not in self._content_workers
            or request_id != self._content_request_id
        ):
            return
        self.branch_combo.blockSignals(True)
        self.branch_combo.clear()
        for branch in branches:
            self.branch_combo.addItem(branch["name"], branch["commit_sha"])
        self.branch_combo.blockSignals(False)
        self.branch_combo.setEnabled(bool(branches))
        self.branch_refresh_button.setEnabled(True)
        self.branch_status_label.setText(
            f"{len(branches)} branch(es) available."
            if branches
            else "This repository has no branches."
        )
        if branches:
            self._branch_selection_changed(self.branch_combo.currentIndex())

    def _repository_branches_failed(
        self, worker: BranchWorker, request_id: int, message: str
    ) -> None:
        if (
            worker in self._content_workers
            and request_id == self._content_request_id
        ):
            self.branch_status_label.setText(message)
            self.branch_refresh_button.setEnabled(True)

    def _branch_selection_changed(self, index: int) -> None:
        branch_sha = self.branch_combo.itemData(index) if index >= 0 else None
        if branch_sha == self._selected_branch_sha:
            return
        self._selected_branch_sha = branch_sha
        self._content_request_id += 1
        request_id = self._content_request_id
        self.repo_file_list.clear()
        self._update_upload_conflicts()
        if branch_sha is None or self.github_auth is None or self._selected_repository is None:
            self.repo_files_status_label.setText("Select a branch to view repository files.")
            return
        self.repo_files_status_label.setText("Loading repository file tree...")
        worker = TreeWorker(
            self.github_auth, self._selected_repository, branch_sha
        )
        self._content_workers.add(worker)
        worker.tree_loaded.connect(
            lambda paths: self._repository_tree_loaded(worker, request_id, paths)
        )
        worker.failed.connect(
            lambda message: self._repository_tree_failed(worker, request_id, message)
        )
        worker.finished.connect(lambda: self._content_worker_finished(worker))
        worker.start()

    def _repository_tree_loaded(
        self, worker: TreeWorker, request_id: int, paths: list[str]
    ) -> None:
        if (
            worker not in self._content_workers
            or request_id != self._content_request_id
        ):
            return
        self.repo_file_list.clear()
        self.repo_file_list.addItems(paths)
        self.repo_files_status_label.setText(
            f"{len(paths)} file(s) in {self.branch_combo.currentText()}."
            if paths
            else "This branch contains no files."
        )
        self._update_upload_conflicts()

    def _repository_tree_failed(
        self, worker: TreeWorker, request_id: int, message: str
    ) -> None:
        if (
            worker in self._content_workers
            and request_id == self._content_request_id
        ):
            self.repo_files_status_label.setText(message)

    def _content_worker_finished(self, worker: BranchWorker | TreeWorker) -> None:
        self._content_workers.discard(worker)
        worker.deleteLater()

    def _public_repositories_failed(
        self, worker: RepositoryWorker, request_id: int, message: str
    ) -> None:
        if self._repository_worker is not worker or self._repository_request_id != request_id:
            return
        if "403" in message:
            message += " GitHub may be rate-limiting public repository requests; try again later."
        self.repositories_status_label.setText(message)

    def _repository_worker_finished(self, worker: RepositoryWorker) -> None:
        if self._repository_worker is worker:
            self._repository_worker = None
            reload_pending = self._repository_reload_pending
            self._repository_reload_pending = False
            self._update_auth_controls()
            worker.deleteLater()
            if reload_pending and self._authenticated_username is not None:
                self._load_public_repositories()

    def _set_fullscreen(self, enabled: bool) -> None:
        if enabled == self.isFullScreen():
            return
        if enabled:
            self._pre_fullscreen_geometry = self.saveGeometry()
            self._pre_fullscreen_state = self.windowState()
            self._pre_fullscreen_size = (self.width(), self.height())
            self._pre_fullscreen_position = (self.x(), self.y())
            self.fullscreen_action.setText("Exit Full Screen")
            self.showFullScreen()
            return

        self.showNormal()
        if self._pre_fullscreen_geometry is not None:
            self.restoreGeometry(self._pre_fullscreen_geometry)
            self._pre_fullscreen_geometry = None
        if self._pre_fullscreen_size is not None:
            self.resize(*self._pre_fullscreen_size)
            self._pre_fullscreen_size = None
        if self._pre_fullscreen_position is not None:
            self.move(*self._pre_fullscreen_position)
            self._pre_fullscreen_position = None
        if self._pre_fullscreen_state & Qt.WindowState.WindowMaximized:
            self.showMaximized()
        self.fullscreen_action.setText("Full Screen")

    def keyPressEvent(self, event: QKeyEvent) -> None:
        if self.isFullScreen() and event.key() == Qt.Key.Key_Escape:
            self.fullscreen_action.setChecked(False)
            event.accept()
            return
        super().keyPressEvent(event)

    def closeEvent(self, event: QCloseEvent) -> None:
        if self._auth_worker is not None:
            self._auth_worker.cancel()
            self._auth_worker.wait()
        if self._repository_worker is not None:
            self._repository_worker.wait()
        for worker in tuple(self._content_workers):
            worker.wait()
        if self._download_worker is not None:
            self._download_worker.wait()
        if self._push_preflight_worker is not None:
            self._push_preflight_worker.wait()
        if self._write_worker is not None:
            self._write_worker.wait()
        event.accept()
