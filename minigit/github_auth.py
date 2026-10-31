from __future__ import annotations

import base64
import json
import os
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

DEVICE_CODE_URL = "https://github.com/login/device/code"
ACCESS_TOKEN_URL = "https://github.com/login/oauth/access_token"
USER_URL = "https://api.github.com/user"
CLIENT_ID_ENV = "MINIGIT_GITHUB_CLIENT_ID"
DEFAULT_CLIENT_ID = "Ov23li34IhavyA1IQWRJ"
KEYRING_SERVICE = "MiniGit GitHub OAuth"
KEYRING_ACCOUNT = "access-token"
API_PAGE_SIZE = 100
MAX_API_PAGES = 20
MAX_TREE_ENTRIES = 100_000
MAX_PUSH_FILE_BYTES = 50 * 1024 * 1024
MAX_PUSH_TOTAL_BYTES = 100 * 1024 * 1024

Transport = Callable[[str, str, Mapping[str, str], bytes | None], Any]
BinaryTransport = Callable[[str, Mapping[str, str]], bytes]


class GitHubAuthError(Exception):
    """A safe, user-presentable GitHub authentication error."""


class AuthorizationPending(GitHubAuthError):
    """The user has not yet approved the device authorization request."""


class AuthorizationDenied(GitHubAuthError):
    """The user denied the device authorization request."""


class AuthorizationExpired(GitHubAuthError):
    """The device authorization code expired before approval."""


class AuthorizationCancelled(GitHubAuthError):
    """The user cancelled the local authorization flow."""


class CredentialStoreError(GitHubAuthError):
    """The operating-system credential store could not be used."""


@dataclass(frozen=True)
class DeviceAuthorization:
    device_code: str
    user_code: str
    verification_uri: str
    expires_in: int
    interval: int


@dataclass(frozen=True)
class PublicPushPreview:
    head_sha: str
    tree_sha: str
    existing_paths: tuple[str, ...]
    target_paths: tuple[str, ...]
    overwrite_paths: tuple[str, ...]


def _http_json(
    method: str,
    url: str,
    headers: Mapping[str, str],
    body: bytes | None,
) -> dict[str, Any] | list[Any]:
    request = urllib.request.Request(url, data=body, headers=dict(headers), method=method)
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            response_body = response.read()
    except urllib.error.HTTPError as error:
        response_body = error.read()
        try:
            payload = json.loads(response_body)
        except (json.JSONDecodeError, UnicodeDecodeError):
            payload = {}
        message = (
            payload.get("message") or payload.get("error_description")
            if isinstance(payload, dict)
            else None
        )
        if message:
            raise GitHubAuthError(f"GitHub returned an HTTP {error.code}: {message}") from None
        raise GitHubAuthError(f"GitHub returned an HTTP {error.code}.") from None
    except (urllib.error.URLError, TimeoutError, OSError) as error:
        reason = getattr(error, "reason", None)
        detail = str(reason or "connection failed")
        raise GitHubAuthError(f"Could not connect to GitHub: {detail}") from None

    try:
        payload = json.loads(response_body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise GitHubAuthError("GitHub returned an invalid response.") from None
    if not isinstance(payload, (dict, list)):
        raise GitHubAuthError("GitHub returned an invalid response.")
    return payload


def _http_bytes(url: str, headers: Mapping[str, str]) -> bytes:
    request = urllib.request.Request(url, headers=dict(headers), method="GET")
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.read()
    except urllib.error.HTTPError as error:
        try:
            payload = json.loads(error.read())
        except (json.JSONDecodeError, UnicodeDecodeError):
            payload = {}
        message = payload.get("message") if isinstance(payload, dict) else None
        if message:
            raise GitHubAuthError(
                f"GitHub returned an HTTP {error.code}: {message}"
            ) from None
        raise GitHubAuthError(f"GitHub returned an HTTP {error.code}.") from None
    except (urllib.error.URLError, TimeoutError, OSError) as error:
        reason = getattr(error, "reason", None)
        detail = str(reason or "connection failed")
        raise GitHubAuthError(f"Could not download from GitHub: {detail}") from None


class KeyringTokenStore:
    """Stores the OAuth token and granted write-scope state in the OS keyring."""

    def __init__(self, keyring_module: Any | None = None) -> None:
        if keyring_module is None:
            try:
                import keyring as keyring_module
            except ImportError as error:
                raise CredentialStoreError(
                    "The OS keyring package is unavailable; reinstall MiniGit dependencies."
                ) from error
        self._keyring = keyring_module

    def load(self) -> str | None:
        try:
            value = self._keyring.get_password(KEYRING_SERVICE, KEYRING_ACCOUNT)
        except Exception:
            raise CredentialStoreError(
                "Could not read the GitHub credential from the OS keyring."
            ) from None
        if value is None:
            return None
        try:
            stored = json.loads(value)
        except (TypeError, json.JSONDecodeError):
            return value
        if isinstance(stored, dict) and stored.get("version") == 1:
            token = stored.get("token")
            return token if isinstance(token, str) and token else None
        return value

    def load_authorization(self) -> tuple[str | None, bool]:
        try:
            value = self._keyring.get_password(KEYRING_SERVICE, KEYRING_ACCOUNT)
        except Exception:
            raise CredentialStoreError(
                "Could not read the GitHub credential from the OS keyring."
            ) from None
        if value is None:
            return None, False
        try:
            stored = json.loads(value)
        except (TypeError, json.JSONDecodeError):
            return value, False
        if isinstance(stored, dict) and stored.get("version") == 1:
            token = stored.get("token")
            if isinstance(token, str) and token:
                return token, stored.get("public_repo") is True
        return value, False

    def save(self, token: str, *, public_repo: bool = False) -> None:
        try:
            value = json.dumps(
                {"version": 1, "token": token, "public_repo": public_repo}
            )
            self._keyring.set_password(KEYRING_SERVICE, KEYRING_ACCOUNT, value)
        except Exception:
            raise CredentialStoreError(
                "Could not save the GitHub credential to the OS keyring."
            ) from None

    def delete(self) -> None:
        try:
            self._keyring.delete_password(KEYRING_SERVICE, KEYRING_ACCOUNT)
        except Exception as error:
            if error.__class__.__name__ in {"PasswordDeleteError", "PasswordNotFoundError"}:
                return
            raise CredentialStoreError(
                "Could not remove the GitHub credential from the OS keyring."
            ) from None


class GitHubDeviceAuth:
    def __init__(
        self,
        client_id: str | None = None,
        *,
        transport: Transport = _http_json,
        binary_transport: BinaryTransport = _http_bytes,
        token_store: KeyringTokenStore | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        configured_client_id = (
            client_id
            if client_id is not None
            else os.environ.get(CLIENT_ID_ENV, DEFAULT_CLIENT_ID)
        )
        self.client_id = configured_client_id.strip()
        self._transport = transport
        self._binary_transport = binary_transport
        self.token_store = token_store or KeyringTokenStore()
        self._sleep = sleep
        self._clock = clock

    def request_device_authorization(self) -> DeviceAuthorization:
        if not self.client_id:
            raise GitHubAuthError(
                f"Set the GitHub OAuth app client ID in {CLIENT_ID_ENV} before connecting."
            )
        response = self._transport(
            "POST",
            DEVICE_CODE_URL,
            {"Accept": "application/json", "Content-Type": "application/json"},
            json.dumps(
                {"client_id": self.client_id, "scope": "read:user public_repo"}
            ).encode("utf-8"),
        )
        if not isinstance(response, dict):
            raise GitHubAuthError("GitHub returned an invalid device authorization response.")
        self._raise_for_oauth_error(response)
        try:
            authorization = DeviceAuthorization(
                device_code=str(response["device_code"]),
                user_code=str(response["user_code"]),
                verification_uri=str(response["verification_uri"]),
                expires_in=int(response["expires_in"]),
                interval=int(response.get("interval", 5)),
            )
        except (KeyError, TypeError, ValueError):
            raise GitHubAuthError("GitHub returned incomplete device authorization data.") from None
        if (
            not authorization.device_code
            or not authorization.user_code
            or not authorization.verification_uri.startswith("https://")
            or authorization.expires_in <= 0
            or authorization.interval < 0
        ):
            raise GitHubAuthError("GitHub returned invalid device authorization data.")
        return authorization

    def poll_for_token(
        self,
        authorization: DeviceAuthorization,
        *,
        on_status: Callable[[str], None] = lambda _message: None,
        is_cancelled: Callable[[], bool] = lambda: False,
    ) -> str:
        deadline = self._clock() + authorization.expires_in
        interval = max(authorization.interval, 1)
        while True:
            if is_cancelled():
                raise AuthorizationCancelled("GitHub sign-in was cancelled.")
            if self._clock() >= deadline:
                raise AuthorizationExpired("The GitHub sign-in code expired. Please try again.")

            remaining = deadline - self._clock()
            self._sleep(min(interval, remaining))
            if is_cancelled():
                raise AuthorizationCancelled("GitHub sign-in was cancelled.")
            if self._clock() >= deadline:
                raise AuthorizationExpired("The GitHub sign-in code expired. Please try again.")

            response = self._transport(
                "POST",
                ACCESS_TOKEN_URL,
                {"Accept": "application/json", "Content-Type": "application/json"},
                json.dumps(
                    {
                        "client_id": self.client_id,
                        "device_code": authorization.device_code,
                        "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                    }
                ).encode("utf-8"),
            )
            if not isinstance(response, dict):
                raise GitHubAuthError("GitHub returned an invalid token response.")
            error = response.get("error")
            if error == "authorization_pending":
                on_status("Waiting for approval in your browser...")
                continue
            if error == "slow_down":
                interval += 5
                on_status("GitHub asked MiniGit to poll more slowly.")
                continue
            if error == "access_denied":
                raise AuthorizationDenied("GitHub sign-in was denied.")
            if error == "expired_token":
                raise AuthorizationExpired("The GitHub sign-in code expired. Please try again.")
            if error:
                description = response.get("error_description")
                raise GitHubAuthError(
                    f"GitHub authorization failed: {description or error}."
                )
            token = response.get("access_token")
            if not isinstance(token, str) or not token:
                raise GitHubAuthError("GitHub returned no access token.")
            return token

    def get_username(self, token: str) -> str:
        response = self._transport(
            "GET",
            USER_URL,
            {
                "Accept": "application/vnd.github+json",
                "Authorization": "Bearer " + token,
                "X-GitHub-Api-Version": "2022-11-28",
            },
            None,
        )
        if not isinstance(response, dict):
            raise GitHubAuthError("GitHub returned an invalid account response.")
        message = response.get("message")
        if isinstance(message, str) and message:
            raise GitHubAuthError(f"GitHub API error: {message}")
        username = response.get("login")
        if not isinstance(username, str) or not username:
            raise GitHubAuthError("GitHub did not return an account name.")
        return username

    def list_public_repositories(
        self, username: str, *, page: int = 1, per_page: int = 100
    ) -> list[dict[str, Any]]:
        if page < 1 or not 1 <= per_page <= 100:
            raise ValueError("Repository page must be positive and page size must be 1-100.")
        if not username or "/" in username:
            raise ValueError("GitHub username is invalid.")
        response = self._transport(
            "GET",
            f"https://api.github.com/users/{quote(username, safe='')}/repos?type=owner"
            f"&sort=full_name&direction=asc&per_page={per_page}&page={page}",
            {
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            None,
        )
        if isinstance(response, dict):
            message = response.get("message")
            if isinstance(message, str) and message:
                raise GitHubAuthError(f"GitHub API error: {message}")
            repositories = response.get("repositories")
        else:
            repositories = response
        if not isinstance(repositories, list):
            raise GitHubAuthError("GitHub returned an invalid public repository list.")
        results: list[dict[str, Any]] = []
        for repository in repositories:
            if not isinstance(repository, dict):
                raise GitHubAuthError("GitHub returned an invalid public repository entry.")
            if repository.get("private") is not False:
                continue
            name = repository.get("full_name")
            url = repository.get("html_url")
            if not isinstance(name, str) or not name or not isinstance(url, str):
                raise GitHubAuthError("GitHub returned an incomplete public repository entry.")
            results.append(repository)
        return results

    def list_all_public_repositories(self, username: str) -> list[dict[str, Any]]:
        repositories: list[dict[str, Any]] = []
        page = 1
        while True:
            current_page = self.list_public_repositories(username, page=page)
            repositories.extend(current_page)
            if len(current_page) < API_PAGE_SIZE:
                return repositories
            page += 1
            if page > MAX_API_PAGES:
                raise GitHubAuthError(
                    "This account has too many public repositories to list safely."
                )

    def list_repository_branches(
        self, repository: str
    ) -> list[dict[str, str]]:
        owner, name = self._split_repository(repository)
        branches: list[dict[str, str]] = []
        for page in range(1, MAX_API_PAGES + 1):
            response = self._transport(
                "GET",
                f"https://api.github.com/repos/{quote(owner, safe='')}/{quote(name, safe='')}"
                f"/branches?per_page={API_PAGE_SIZE}&page={page}",
                {
                    "Accept": "application/vnd.github+json",
                    "X-GitHub-Api-Version": "2022-11-28",
                },
                None,
            )
            if not isinstance(response, list):
                self._raise_for_api_error(response, "branch list")
            page_branches: list[dict[str, str]] = []
            for branch in response:
                if not isinstance(branch, dict):
                    raise GitHubAuthError("GitHub returned an invalid branch entry.")
                branch_name = branch.get("name")
                commit = branch.get("commit")
                sha = commit.get("sha") if isinstance(commit, dict) else None
                if not isinstance(branch_name, str) or not branch_name:
                    raise GitHubAuthError("GitHub returned a branch without a name.")
                if not isinstance(sha, str) or not sha:
                    raise GitHubAuthError(
                        f"GitHub returned no commit for branch {branch_name}."
                    )
                page_branches.append({"name": branch_name, "commit_sha": sha})
            branches.extend(page_branches)
            if len(page_branches) < API_PAGE_SIZE:
                return branches
        raise GitHubAuthError(
            "This repository has too many branches to list safely (limit: 2,000)."
        )

    def get_repository_tree(
        self, repository: str, branch_commit_sha: str
    ) -> list[str]:
        owner, name = self._split_repository(repository)
        if not branch_commit_sha or "/" in branch_commit_sha:
            raise ValueError("Git commit SHA is invalid.")
        commit = self._transport(
            "GET",
            f"https://api.github.com/repos/{quote(owner, safe='')}/{quote(name, safe='')}"
            f"/git/commits/{quote(branch_commit_sha, safe='')}",
            {
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            None,
        )
        if not isinstance(commit, dict):
            self._raise_for_api_error(commit, "branch commit")
        tree = commit.get("tree")
        tree_sha = tree.get("sha") if isinstance(tree, dict) else None
        if not isinstance(tree_sha, str) or not tree_sha:
            raise GitHubAuthError("GitHub returned no tree for the selected branch.")

        response = self._transport(
            "GET",
            f"https://api.github.com/repos/{quote(owner, safe='')}/{quote(name, safe='')}"
            f"/git/trees/{quote(tree_sha, safe='')}?recursive=1",
            {
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            None,
        )
        if not isinstance(response, dict):
            self._raise_for_api_error(response, "repository tree")
        if response.get("truncated") is True:
            raise GitHubAuthError(
                "GitHub truncated this repository tree; MiniGit cannot show a complete file list."
            )
        entries = response.get("tree")
        if not isinstance(entries, list):
            raise GitHubAuthError("GitHub returned an invalid repository tree.")
        if len(entries) > MAX_TREE_ENTRIES:
            raise GitHubAuthError(
                "This repository contains too many tree entries to display safely."
            )
        paths: list[str] = []
        for entry in entries:
            if not isinstance(entry, dict):
                raise GitHubAuthError("GitHub returned an invalid repository tree entry.")
            if entry.get("type") == "blob":
                path = entry.get("path")
                if not isinstance(path, str) or not path:
                    raise GitHubAuthError("GitHub returned a file without a path.")
                paths.append(path)
        return sorted(paths, key=str.casefold)

    def download_public_file(
        self, repository: str, branch: str, path: str
    ) -> bytes:
        owner, name = self._split_repository(repository)
        parts = path.split("/")
        if (
            not branch
            or not path
            or path.startswith("/")
            or "\\" in path
            or any(part in {"", ".", ".."} for part in parts)
        ):
            raise ValueError("Repository file path or branch is invalid.")
        url = (
            f"https://raw.githubusercontent.com/{quote(owner, safe='')}/"
            f"{quote(name, safe='')}/{quote(branch, safe='/')}/"
            f"{quote(path, safe='/')}"
        )
        return self._binary_transport(
            url,
            {"User-Agent": "MiniGit", "Accept": "application/octet-stream"},
        )

    def prepare_public_push(
        self, repository: str, branch: str, target_paths: list[str]
    ) -> PublicPushPreview:
        token = self._require_saved_token()
        username = self.get_username(token)
        owner, name = self._split_repository(repository)
        base_url = f"https://api.github.com/repos/{quote(owner, safe='')}/{quote(name, safe='')}"
        metadata = self._authorized_json("GET", base_url, token)
        if not isinstance(metadata, dict):
            raise GitHubAuthError("GitHub returned invalid repository details.")
        repo_owner = metadata.get("owner")
        permissions = metadata.get("permissions")
        if (
            metadata.get("private") is not False
            or not isinstance(repo_owner, dict)
            or str(repo_owner.get("login", "")).casefold() != username.casefold()
            or not isinstance(permissions, dict)
            or permissions.get("push") is not True
        ):
            raise GitHubAuthError(
                "MiniGit can only push to public repositories owned by your connected account."
            )

        if not branch or "\x00" in branch:
            raise ValueError("Selected branch name is invalid.")
        ref_name = f"heads/{branch}"
        ref_url = f"{base_url}/git/ref/{quote(ref_name, safe='/')}"
        ref = self._authorized_json("GET", ref_url, token)
        if not isinstance(ref, dict) or ref.get("ref") != f"refs/{ref_name}":
            self._raise_for_api_error(ref, "branch reference")
        ref_object = ref.get("object")
        head_sha = ref_object.get("sha") if isinstance(ref_object, dict) else None
        if not isinstance(head_sha, str) or not head_sha:
            raise GitHubAuthError("GitHub returned no commit for the selected branch.")

        commit = self._authorized_json(
            "GET", f"{base_url}/git/commits/{quote(head_sha, safe='')}", token
        )
        if not isinstance(commit, dict):
            self._raise_for_api_error(commit, "branch commit")
        tree = commit.get("tree")
        tree_sha = tree.get("sha") if isinstance(tree, dict) else None
        if not isinstance(tree_sha, str) or not tree_sha:
            raise GitHubAuthError("GitHub returned no tree for the selected branch.")
        tree_response = self._transport(
            "GET",
            f"{base_url}/git/trees/{quote(tree_sha, safe='')}?recursive=1",
            {
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            None,
        )
        existing_paths = self._tree_file_paths(tree_response)
        normalized_targets: set[str] = set()
        for path in target_paths:
            self._validate_repository_path(path)
            normalized = path.casefold()
            if normalized in normalized_targets:
                raise GitHubAuthError(
                    f"Multiple selected files map to the same repository path: {path}."
                )
            normalized_targets.add(normalized)
        normalized_list = tuple(normalized_targets)
        if any(
            left.startswith(right + "/") or right.startswith(left + "/")
            for index, left in enumerate(normalized_list)
            for right in normalized_list[index + 1 :]
        ):
            raise GitHubAuthError(
                "Selected files contain a file/directory target-path conflict."
            )
        existing_by_casefold = {path.casefold(): path for path in existing_paths}
        overwrite_paths: list[str] = []
        existing_folds = tuple(existing_by_casefold)
        for path in target_paths:
            normalized = path.casefold()
            existing_match = existing_by_casefold.get(normalized)
            if existing_match is not None:
                if existing_match != path:
                    raise GitHubAuthError(
                        f"Case-only path collision between {path} and {existing_match}; "
                        "rename one path before pushing."
                    )
                overwrite_paths.append(path)
                continue
            if any(
                existing.startswith(normalized + "/")
                or normalized.startswith(existing + "/")
                for existing in existing_folds
            ):
                raise GitHubAuthError(
                    f"Repository file/directory path conflict for {path}; resolve it before pushing."
                )
        return PublicPushPreview(
            head_sha=head_sha,
            tree_sha=tree_sha,
            existing_paths=tuple(existing_paths),
            target_paths=tuple(target_paths),
            overwrite_paths=tuple(overwrite_paths),
        )

    def create_pending_public_commit(
        self,
        repository: str,
        branch: str,
        preview: PublicPushPreview,
        files: Mapping[str, bytes],
        message: str,
    ) -> str:
        token = self._require_saved_token()
        username = self.get_username(token)
        owner, name = self._split_repository(repository)
        base_url = f"https://api.github.com/repos/{quote(owner, safe='')}/{quote(name, safe='')}"
        metadata = self._authorized_json("GET", base_url, token)
        repo_owner = metadata.get("owner") if isinstance(metadata, dict) else None
        permissions = metadata.get("permissions") if isinstance(metadata, dict) else None
        if (
            not isinstance(metadata, dict)
            or metadata.get("private") is not False
            or not isinstance(repo_owner, dict)
            or str(repo_owner.get("login", "")).casefold() != username.casefold()
            or not isinstance(permissions, dict)
            or permissions.get("push") is not True
        ):
            raise GitHubAuthError(
                "MiniGit can only push to public repositories owned by your connected account."
            )
        if not message.strip():
            raise ValueError("Commit message cannot be empty.")
        if not files or tuple(files) != preview.target_paths:
            raise ValueError("The staged files no longer match the reviewed push preview.")
        self._verify_branch_head(base_url, branch, preview.head_sha, token)
        total_size = 0
        tree_entries: list[dict[str, str]] = []
        for path, content in files.items():
            self._validate_repository_path(path)
            if not isinstance(content, bytes):
                raise ValueError(f"Staged file data is invalid for {path}.")
            if len(content) > MAX_PUSH_FILE_BYTES:
                raise GitHubAuthError(
                    f"{path} exceeds MiniGit's {MAX_PUSH_FILE_BYTES // (1024 * 1024)} MiB per-file push limit."
                )
            total_size += len(content)
            if total_size > MAX_PUSH_TOTAL_BYTES:
                raise GitHubAuthError(
                    f"Selected files exceed MiniGit's {MAX_PUSH_TOTAL_BYTES // (1024 * 1024)} MiB total push limit."
                )
            blob = self._authorized_json(
                "POST",
                f"{base_url}/git/blobs",
                token,
                {
                    "content": base64.b64encode(content).decode("ascii"),
                    "encoding": "base64",
                },
            )
            blob_sha = blob.get("sha") if isinstance(blob, dict) else None
            if not isinstance(blob_sha, str) or not blob_sha:
                self._raise_for_api_error(blob, "created file blob")
            tree_entries.append(
                {"path": path, "mode": "100644", "type": "blob", "sha": blob_sha}
            )

        tree = self._authorized_json(
            "POST",
            f"{base_url}/git/trees",
            token,
            {"base_tree": preview.tree_sha, "tree": tree_entries},
        )
        tree_sha = tree.get("sha") if isinstance(tree, dict) else None
        if not isinstance(tree_sha, str) or not tree_sha:
            self._raise_for_api_error(tree, "created tree")
        commit = self._authorized_json(
            "POST",
            f"{base_url}/git/commits",
            token,
            {
                "message": message.strip(),
                "tree": tree_sha,
                "parents": [preview.head_sha],
            },
        )
        commit_sha = commit.get("sha") if isinstance(commit, dict) else None
        if not isinstance(commit_sha, str) or not commit_sha:
            self._raise_for_api_error(commit, "created commit")
        return commit_sha

    def push_pending_public_commit(
        self,
        repository: str,
        branch: str,
        preview: PublicPushPreview,
        commit_sha: str,
    ) -> str:
        token = self._require_saved_token()
        username = self.get_username(token)
        owner, name = self._split_repository(repository)
        base_url = f"https://api.github.com/repos/{quote(owner, safe='')}/{quote(name, safe='')}"
        metadata = self._authorized_json("GET", base_url, token)
        repo_owner = metadata.get("owner") if isinstance(metadata, dict) else None
        permissions = metadata.get("permissions") if isinstance(metadata, dict) else None
        if (
            not isinstance(metadata, dict)
            or metadata.get("private") is not False
            or not isinstance(repo_owner, dict)
            or str(repo_owner.get("login", "")).casefold() != username.casefold()
            or not isinstance(permissions, dict)
            or permissions.get("push") is not True
        ):
            raise GitHubAuthError(
                "MiniGit can only push to public repositories owned by your connected account."
            )
        if not commit_sha:
            raise ValueError("There is no pending commit to push.")
        self._verify_branch_head(base_url, branch, preview.head_sha, token)
        try:
            update = self._authorized_json(
                "PATCH",
                f"{base_url}/git/refs/{quote(f'heads/{branch}', safe='/')}",
                token,
                {"sha": commit_sha, "force": False},
            )
        except GitHubAuthError as error:
            if "could not connect" in str(error).casefold() or "http 5" in str(error).casefold():
                raise GitHubAuthError(
                    "GitHub did not confirm the branch update. Refresh the branch "
                    "before retrying because the commit may have been applied."
                ) from None
            raise
        updated_object = update.get("object") if isinstance(update, dict) else None
        if not isinstance(updated_object, dict) or updated_object.get("sha") != commit_sha:
            self._raise_for_api_error(update, "updated branch")
        return commit_sha

    def commit_and_push_public_files(
        self,
        repository: str,
        branch: str,
        preview: PublicPushPreview,
        files: Mapping[str, bytes],
        message: str,
    ) -> str:
        commit_sha = self.create_pending_public_commit(
            repository, branch, preview, files, message
        )
        return self.push_pending_public_commit(
            repository, branch, preview, commit_sha
        )

    def _verify_branch_head(
        self, base_url: str, branch: str, expected_head: str, token: str
    ) -> None:
        current_ref = self._authorized_json(
            "GET",
            f"{base_url}/git/ref/{quote(f'heads/{branch}', safe='/')}",
            token,
        )
        if (
            not isinstance(current_ref, dict)
            or current_ref.get("ref") != f"refs/heads/{branch}"
        ):
            self._raise_for_api_error(current_ref, "branch reference")
        ref_object = current_ref.get("object")
        current_head = ref_object.get("sha") if isinstance(ref_object, dict) else None
        if current_head != expected_head:
            raise GitHubAuthError(
                "The branch changed after the preview. Refresh it and review the operation again."
            )

    def _require_saved_token(self) -> str:
        token = self.token_store.load()
        if not token:
            raise GitHubAuthError(
                "Connect GitHub and approve public repository access before pushing."
            )
        return token

    def _authorized_json(
        self,
        method: str,
        url: str,
        token: str,
        payload: Mapping[str, Any] | None = None,
    ) -> dict[str, Any] | list[Any]:
        headers = {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        body = None
        if payload is not None:
            headers["Content-Type"] = "application/json"
            body = json.dumps(payload).encode("utf-8")
        response = self._transport(method, url, headers, body)
        if not isinstance(response, (dict, list)):
            raise GitHubAuthError("GitHub returned an invalid API response.")
        return response

    @staticmethod
    def _validate_repository_path(path: str) -> None:
        parts = path.split("/")
        if (
            not path
            or path.startswith("/")
            or "\\" in path
            or "\x00" in path
            or any(part in {"", ".", ".."} for part in parts)
        ):
            raise ValueError(f"Unsafe repository path: {path!r}")

    @staticmethod
    def _tree_file_paths(response: Any) -> list[str]:
        if not isinstance(response, dict):
            GitHubDeviceAuth._raise_for_api_error(response, "repository tree")
        if response.get("truncated") is True:
            raise GitHubAuthError(
                "GitHub truncated this repository tree; MiniGit cannot safely prepare a push."
            )
        entries = response.get("tree")
        if not isinstance(entries, list):
            raise GitHubAuthError("GitHub returned an invalid repository tree.")
        if len(entries) > MAX_TREE_ENTRIES:
            raise GitHubAuthError(
                "This repository contains too many tree entries to inspect safely."
            )
        paths: list[str] = []
        for entry in entries:
            if not isinstance(entry, dict):
                raise GitHubAuthError("GitHub returned an invalid repository tree entry.")
            if entry.get("type") == "blob":
                path = entry.get("path")
                if not isinstance(path, str) or not path:
                    raise GitHubAuthError("GitHub returned a file without a path.")
                paths.append(path)
        return sorted(paths, key=str.casefold)

    @staticmethod
    def _split_repository(repository: str) -> tuple[str, str]:
        parts = repository.split("/")
        if len(parts) != 2 or not all(parts):
            raise ValueError("Public repository must use owner/name format.")
        return parts[0], parts[1]

    @staticmethod
    def _raise_for_api_error(response: Any, resource: str) -> None:
        raise GitHubAuthError(f"GitHub returned an invalid {resource} response.")

    @staticmethod
    def _raise_for_oauth_error(response: Mapping[str, Any]) -> None:
        error = response.get("error")
        if error:
            description = response.get("error_description")
            raise GitHubAuthError(f"GitHub authorization failed: {description or error}.")
