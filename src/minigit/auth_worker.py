from __future__ import annotations

from pathlib import Path, PurePosixPath
from threading import Event

from PySide6.QtCore import QThread, Signal

from minigit.github_auth import (
    MAX_PUSH_FILE_BYTES,
    MAX_PUSH_TOTAL_BYTES,
    GitHubAuthError,
    GitHubDeviceAuth,
    PublicPushPreview,
)


class AuthWorker(QThread):
    authorization_ready = Signal(str, str, int)
    status_changed = Signal(str)
    authenticated = Signal(str)
    failed = Signal(str)

    def __init__(
        self,
        auth: GitHubDeviceAuth,
        *,
        restore_token: str | None = None,
        restore_public_repo_scope: bool = False,
    ) -> None:
        super().__init__()
        self._auth = auth
        self._restore_token = restore_token
        self._restore_public_repo_scope = restore_public_repo_scope
        self._cancelled = Event()

    def cancel(self) -> None:
        self._cancelled.set()

    @property
    def was_cancelled(self) -> bool:
        return self._cancelled.is_set()

    @property
    def is_restoring_connection(self) -> bool:
        return self._restore_token is not None

    @property
    def has_public_repo_scope(self) -> bool:
        return (
            self._restore_public_repo_scope
            if self.is_restoring_connection
            else True
        )

    def run(self) -> None:
        try:
            if self._restore_token is not None:
                username = self._auth.get_username(self._restore_token)
                self.authenticated.emit(username)
                return

            authorization = self._auth.request_device_authorization()
            self.authorization_ready.emit(
                authorization.user_code,
                authorization.verification_uri,
                authorization.expires_in,
            )
            token = self._auth.poll_for_token(
                authorization,
                on_status=self.status_changed.emit,
                is_cancelled=self._cancelled.is_set,
            )
            username = self._auth.get_username(token)
            self._auth.token_store.save(token, public_repo=True)
            self.authenticated.emit(username)
        except GitHubAuthError as error:
            if not self._cancelled.is_set():
                self.failed.emit(str(error))
        except Exception:
            if not self._cancelled.is_set():
                self.failed.emit("GitHub sign-in failed unexpectedly. Please try again.")


class RepositoryWorker(QThread):
    repositories_loaded = Signal(object)
    failed = Signal(str)

    def __init__(self, auth: GitHubDeviceAuth, username: str) -> None:
        super().__init__()
        self._auth = auth
        self._username = username

    def run(self) -> None:
        try:
            repositories = self._auth.list_all_public_repositories(self._username)
            self.repositories_loaded.emit(repositories)
        except GitHubAuthError as error:
            self.failed.emit(str(error))
        except Exception:
            self.failed.emit("Could not load public repositories. Please try again.")


class BranchWorker(QThread):
    branches_loaded = Signal(object)
    failed = Signal(str)

    def __init__(self, auth: GitHubDeviceAuth, repository: str) -> None:
        super().__init__()
        self._auth = auth
        self._repository = repository

    def run(self) -> None:
        try:
            branches = self._auth.list_repository_branches(self._repository)
            self.branches_loaded.emit(branches)
        except GitHubAuthError as error:
            self.failed.emit(str(error))
        except Exception:
            self.failed.emit("Could not load repository branches. Please try again.")


class TreeWorker(QThread):
    tree_loaded = Signal(object)
    failed = Signal(str)

    def __init__(
        self,
        auth: GitHubDeviceAuth,
        repository: str,
        branch_commit_sha: str,
    ) -> None:
        super().__init__()
        self._auth = auth
        self._repository = repository
        self._branch_commit_sha = branch_commit_sha

    def run(self) -> None:
        try:
            paths = self._auth.get_repository_tree(
                self._repository, self._branch_commit_sha
            )
            self.tree_loaded.emit(paths)
        except GitHubAuthError as error:
            self.failed.emit(str(error))
        except Exception:
            self.failed.emit("Could not load repository files. Please try again.")


class DownloadWorker(QThread):
    completed = Signal(object, object)

    def __init__(
        self,
        auth: GitHubDeviceAuth,
        repository: str,
        branch: str,
        paths: list[str],
        destination: Path,
        confirmed_overwrites: set[str],
        *,
        single_file_target: Path | None = None,
    ) -> None:
        super().__init__()
        self._auth = auth
        self._repository = repository
        self._branch = branch
        self._paths = paths
        self._destination = destination
        self._confirmed_overwrites = confirmed_overwrites
        self._single_file_target = single_file_target

    def run(self) -> None:
        downloaded: list[str] = []
        errors: list[str] = []
        try:
            root = self._destination.resolve(strict=True)
            if not root.is_dir():
                raise OSError("The selected download destination is not a folder.")
            if self._single_file_target is not None and len(self._paths) != 1:
                raise ValueError("A single-file destination requires exactly one file.")
            for remote_path in self._paths:
                try:
                    parts = PurePosixPath(remote_path).parts
                    if (
                        not remote_path
                        or remote_path.startswith("/")
                        or "\\" in remote_path
                        or any(part in {"", ".", ".."} for part in parts)
                    ):
                        raise ValueError("GitHub returned an unsafe file path.")
                    target = (
                        self._single_file_target
                        if self._single_file_target is not None
                        else root.joinpath(*parts)
                    )
                    resolved_target = target.resolve(strict=False)
                    resolved_target.relative_to(root)
                    if resolved_target.exists() and resolved_target.is_dir():
                        raise OSError("A folder already exists at this file path.")
                    if (
                        resolved_target.exists()
                        and str(resolved_target) not in self._confirmed_overwrites
                    ):
                        raise FileExistsError(
                            "A local file appeared after overwrite confirmation; it was not replaced."
                        )

                    contents = self._auth.download_public_file(
                        self._repository, self._branch, remote_path
                    )
                    target.parent.mkdir(parents=True, exist_ok=True)
                    resolved_target = target.resolve(strict=False)
                    resolved_target.relative_to(root)
                    if (
                        resolved_target.exists()
                        and str(resolved_target) not in self._confirmed_overwrites
                    ):
                        raise FileExistsError(
                            "A local file appeared after overwrite confirmation; it was not replaced."
                        )
                    resolved_target.write_bytes(contents)
                    downloaded.append(remote_path)
                except (OSError, ValueError, GitHubAuthError) as error:
                    errors.append(f"{remote_path}: {error}")
        except (OSError, ValueError) as error:
            errors.append(str(error))
        self.completed.emit(downloaded, errors)


class PushPreflightWorker(QThread):
    prepared = Signal(object, object)
    failed = Signal(str)

    def __init__(
        self,
        auth: GitHubDeviceAuth,
        repository: str,
        branch: str,
        upload_targets: tuple[tuple[Path, str], ...],
    ) -> None:
        super().__init__()
        self._auth = auth
        self._repository = repository
        self._branch = branch
        self._upload_targets = upload_targets

    def run(self) -> None:
        try:
            files: dict[str, bytes] = {}
            total_size = 0
            for source, target in self._upload_targets:
                size = source.stat().st_size
                if size > MAX_PUSH_FILE_BYTES:
                    raise GitHubAuthError(
                        f"{source.name} exceeds MiniGit's "
                        f"{MAX_PUSH_FILE_BYTES // (1024 * 1024)} MiB per-file push limit."
                    )
                total_size += size
                if total_size > MAX_PUSH_TOTAL_BYTES:
                    raise GitHubAuthError(
                        "Selected files exceed MiniGit's "
                        f"{MAX_PUSH_TOTAL_BYTES // (1024 * 1024)} MiB total push limit."
                    )
                files[target] = source.read_bytes()
            preview = self._auth.prepare_public_push(
                self._repository, self._branch, list(files)
            )
            self.prepared.emit(preview, files)
        except (GitHubAuthError, OSError, ValueError) as error:
            self.failed.emit(str(error))
        except Exception:
            self.failed.emit("Could not prepare the public repository push.")


class CommitPushWorker(QThread):
    completed = Signal(str)
    failed = Signal(str)

    def __init__(
        self,
        auth: GitHubDeviceAuth,
        repository: str,
        branch: str,
        preview: PublicPushPreview,
        files: dict[str, bytes],
        message: str,
    ) -> None:
        super().__init__()
        self._auth = auth
        self._repository = repository
        self._branch = branch
        self._preview = preview
        self._files = files
        self._message = message

    def run(self) -> None:
        try:
            commit_sha = self._auth.commit_and_push_public_files(
                self._repository,
                self._branch,
                self._preview,
                self._files,
                self._message,
            )
            self.completed.emit(commit_sha)
        except (GitHubAuthError, OSError, ValueError) as error:
            self.failed.emit(str(error))
        except Exception:
            self.failed.emit(
                "GitHub did not confirm the commit and branch update. "
                "Refresh the branch before retrying."
            )
