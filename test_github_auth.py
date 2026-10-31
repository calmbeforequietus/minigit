from __future__ import annotations

import json
import io
import urllib.error
from collections.abc import Mapping
from typing import Any

import pytest

from minigit.auth_worker import AuthWorker
from minigit.github_auth import (
    ACCESS_TOKEN_URL,
    CLIENT_ID_ENV,
    DEFAULT_CLIENT_ID,
    DEVICE_CODE_URL,
    KEYRING_ACCOUNT,
    KEYRING_SERVICE,
    USER_URL,
    AuthorizationDenied,
    AuthorizationExpired,
    CredentialStoreError,
    DeviceAuthorization,
    GitHubAuthError,
    GitHubDeviceAuth,
    KeyringTokenStore,
    _http_json,
)


class FakeKeyring:
    def __init__(self) -> None:
        self.values: dict[tuple[str, str], str] = {}

    def get_password(self, service: str, account: str) -> str | None:
        return self.values.get((service, account))

    def set_password(self, service: str, account: str, value: str) -> None:
        self.values[(service, account)] = value

    def delete_password(self, service: str, account: str) -> None:
        self.values.pop((service, account), None)


def response(data: dict[str, Any]) -> dict[str, Any]:
    return data


def device_authorization(**overrides: Any) -> DeviceAuthorization:
    values: dict[str, Any] = {
        "device_code": "device-secret",
        "user_code": "ABCD-EFGH",
        "verification_uri": "https://github.com/login/device",
        "expires_in": 100,
        "interval": 5,
    }
    values.update(overrides)
    return DeviceAuthorization(**values)


def test_device_request_uses_configured_client_id_and_public_repo_scope() -> None:
    calls: list[tuple[str, str, Mapping[str, str], bytes | None]] = []
    auth = GitHubDeviceAuth(
        "example-client-id",
        transport=lambda *args: calls.append(args) or response(
            {
                "device_code": "device-secret",
                "user_code": "ABCD-EFGH",
                "verification_uri": "https://github.com/login/device",
                "expires_in": 900,
            }
        ),
        token_store=KeyringTokenStore(FakeKeyring()),
    )

    result = auth.request_device_authorization()

    assert result.interval == 5
    method, url, headers, body = calls[0]
    assert (method, url) == ("POST", DEVICE_CODE_URL)
    assert headers["Accept"] == "application/json"
    assert json.loads(body or b"") == {
        "client_id": "example-client-id",
        "scope": "read:user public_repo",
    }


def test_missing_client_id_is_a_clear_configuration_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(CLIENT_ID_ENV, raising=False)
    auth = GitHubDeviceAuth("", token_store=KeyringTokenStore(FakeKeyring()))

    with pytest.raises(GitHubAuthError, match=CLIENT_ID_ENV):
        auth.request_device_authorization()


def test_public_client_id_default_can_be_overridden(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(CLIENT_ID_ENV, raising=False)
    store = KeyringTokenStore(FakeKeyring())
    assert GitHubDeviceAuth(token_store=store).client_id == DEFAULT_CLIENT_ID

    monkeypatch.setenv(CLIENT_ID_ENV, "environment-client-id")
    assert GitHubDeviceAuth(token_store=store).client_id == "environment-client-id"


def test_polling_handles_pending_slow_down_and_token_success() -> None:
    replies = iter(
        [
            {"error": "authorization_pending"},
            {"error": "slow_down"},
            {"access_token": "fake-access-token"},
        ]
    )
    now = [0.0]
    waits: list[float] = []
    statuses: list[str] = []
    calls: list[tuple[str, str, Mapping[str, str], bytes | None]] = []

    def transport(*args: Any) -> dict[str, Any]:
        calls.append(args)
        return next(replies)

    def sleep(duration: float) -> None:
        waits.append(duration)
        now[0] += duration

    auth = GitHubDeviceAuth(
        "client",
        transport=transport,
        token_store=KeyringTokenStore(FakeKeyring()),
        sleep=sleep,
        clock=lambda: now[0],
    )

    token = auth.poll_for_token(
        device_authorization(),
        on_status=statuses.append,
    )

    assert token == "fake-access-token"
    assert waits == [5, 5, 10]
    assert len(calls) == 3
    assert statuses == [
        "Waiting for approval in your browser...",
        "GitHub asked MiniGit to poll more slowly.",
    ]
    assert all(call[1] == ACCESS_TOKEN_URL for call in calls)
    assert json.loads(calls[0][3] or b"")["grant_type"] == (
        "urn:ietf:params:oauth:grant-type:device_code"
    )


@pytest.mark.parametrize(
    ("error", "exception"),
    [
        ("access_denied", AuthorizationDenied),
        ("expired_token", AuthorizationExpired),
    ],
)
def test_polling_surfaces_denial_and_expiry(error: str, exception: type[Exception]) -> None:
    auth = GitHubDeviceAuth(
        "client",
        transport=lambda *_args: {"error": error},
        token_store=KeyringTokenStore(FakeKeyring()),
        sleep=lambda _duration: None,
    )

    with pytest.raises(exception):
        auth.poll_for_token(device_authorization())


def test_polling_stops_at_code_expiry_without_requesting_a_token() -> None:
    now = [0.0]
    requests: list[str] = []

    def sleep(duration: float) -> None:
        now[0] += duration

    auth = GitHubDeviceAuth(
        "client",
        transport=lambda *_args: requests.append("request") or {},
        token_store=KeyringTokenStore(FakeKeyring()),
        sleep=sleep,
        clock=lambda: now[0],
    )

    with pytest.raises(AuthorizationExpired):
        auth.poll_for_token(device_authorization(expires_in=2, interval=2))
    assert requests == []


def test_polling_honors_cancellation() -> None:
    auth = GitHubDeviceAuth(
        "client",
        transport=lambda *_args: pytest.fail("must not request a token after cancellation"),
        token_store=KeyringTokenStore(FakeKeyring()),
    )

    with pytest.raises(GitHubAuthError, match="cancelled"):
        auth.poll_for_token(device_authorization(), is_cancelled=lambda: True)


def test_user_lookup_and_keyring_token_save_and_removal() -> None:
    keyring = FakeKeyring()
    store = KeyringTokenStore(keyring)
    replies = iter(
        [
            {
                "device_code": "device-secret",
                "user_code": "ABCD-EFGH",
                "verification_uri": "https://github.com/login/device",
                "expires_in": 900,
                "interval": 1,
            },
            {"access_token": "fake-access-token"},
            {"login": "octocat"},
        ]
    )
    calls: list[tuple[str, str, Mapping[str, str], bytes | None]] = []

    def transport(
        method: str, url: str, headers: Mapping[str, str], body: bytes | None
    ) -> dict[str, Any]:
        calls.append((method, url, headers, body))
        return next(replies)

    auth = GitHubDeviceAuth(
        "client",
        transport=transport,
        token_store=store,
        sleep=lambda _duration: None,
    )
    names: list[str] = []
    errors: list[str] = []
    worker = AuthWorker(auth)
    worker.authenticated.connect(names.append)
    worker.failed.connect(errors.append)

    # Exercise the worker synchronously while keeping HTTP fully mocked.
    worker.run()

    assert names == ["octocat"]
    assert errors == []
    assert store.load() == "fake-access-token"
    restored_token, has_write_scope = KeyringTokenStore(keyring).load_authorization()
    assert restored_token == "fake-access-token"
    assert has_write_scope
    assert keyring.values
    assert calls[-1][0:2] == ("GET", USER_URL)
    assert calls[-1][2]["Authorization"] == "Bearer fake-access-token"
    store.delete()
    assert store.load() is None
    assert not keyring.values


def test_legacy_keyring_token_requires_reauthorization_only_once() -> None:
    keyring = FakeKeyring()
    keyring.set_password(KEYRING_SERVICE, KEYRING_ACCOUNT, "legacy-read-user-token")
    store = KeyringTokenStore(keyring)

    assert store.load_authorization() == ("legacy-read-user-token", False)
    store.save("expanded-scope-token", public_repo=True)
    assert KeyringTokenStore(keyring).load_authorization() == (
        "expanded-scope-token",
        True,
    )


def test_token_is_not_saved_if_account_lookup_fails() -> None:
    keyring = FakeKeyring()
    store = KeyringTokenStore(keyring)
    replies = iter(
        [
            {
                "device_code": "device-secret",
                "user_code": "ABCD-EFGH",
                "verification_uri": "https://github.com/login/device",
                "expires_in": 900,
                "interval": 1,
            },
            {"access_token": "fake-access-token"},
            {"message": "Bad credentials"},
        ]
    )
    auth = GitHubDeviceAuth(
        "client",
        transport=lambda *_args: next(replies),
        token_store=store,
        sleep=lambda _duration: None,
    )
    errors: list[str] = []
    worker = AuthWorker(auth)
    worker.failed.connect(errors.append)
    worker.run()

    assert errors == ["GitHub API error: Bad credentials"]
    assert keyring.values == {}


def test_failed_reauthorization_does_not_replace_saved_token() -> None:
    keyring = FakeKeyring()
    store = KeyringTokenStore(keyring)
    store.save("existing-read-user-token")

    def transport(
        method: str, url: str, _headers: Mapping[str, str], _body: bytes | None
    ) -> dict[str, Any]:
        if method == "POST" and url == DEVICE_CODE_URL:
            return {
                "device_code": "device-secret",
                "user_code": "ABCD-EFGH",
                "verification_uri": "https://github.com/login/device",
                "expires_in": 900,
                "interval": 1,
            }
        if method == "POST" and url == ACCESS_TOKEN_URL:
            return {"access_token": "new-write-token"}
        if method == "GET" and url == USER_URL:
            return {"message": "account verification failed"}
        pytest.fail(f"Unexpected auth request: {method} {url}")

    auth = GitHubDeviceAuth(
        "client",
        transport=transport,
        token_store=store,
        sleep=lambda _delay: None,
    )
    errors: list[str] = []
    worker = AuthWorker(auth)
    worker.failed.connect(errors.append)

    worker.run()

    assert store.load() == "existing-read-user-token"
    assert errors == ["GitHub API error: account verification failed"]


def test_user_lookup_reports_github_api_errors() -> None:
    auth = GitHubDeviceAuth(
        "client",
        transport=lambda *_args: {"message": "Bad credentials"},
        token_store=KeyringTokenStore(FakeKeyring()),
    )

    with pytest.raises(GitHubAuthError, match="Bad credentials"):
        auth.get_username("fake-access-token")


def test_authorized_commit_response_message_is_not_mistaken_for_api_error() -> None:
    commit_message = "Remove heading syntax from README description"
    response = {
        "sha": "commit-sha",
        "message": commit_message,
        "tree": {"sha": "tree-sha"},
        "parents": [],
    }
    auth = GitHubDeviceAuth(
        "client",
        transport=lambda *_args: response,
        token_store=KeyringTokenStore(FakeKeyring()),
    )

    result = auth._authorized_json(
        "GET",
        "https://api.github.com/repos/octocat/repo/git/commits/commit-sha",
        "fake-write-token",
    )

    assert result is response
    assert result["message"] == commit_message


def test_http_api_error_message_is_distinct_from_success_payload_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    error_body = json.dumps(
        {"message": "Validation Failed", "errors": [{"message": "invalid request"}]}
    ).encode()

    def rejected(*_args: Any, **_kwargs: Any) -> None:
        raise urllib.error.HTTPError(
            "https://api.github.com/repos/octocat/repo/git/commits",
            422,
            "Unprocessable Entity",
            None,
            io.BytesIO(error_body),
        )

    monkeypatch.setattr("urllib.request.urlopen", rejected)

    with pytest.raises(
        GitHubAuthError, match="HTTP 422: Validation Failed"
    ):
        _http_json(
            "POST",
            "https://api.github.com/repos/octocat/repo/git/commits",
            {"Content-Type": "application/json"},
            b'{"message":"Update README"}',
        )


def test_invalid_success_response_is_not_reported_as_api_message() -> None:
    with pytest.raises(
        GitHubAuthError, match="GitHub returned an invalid created commit response"
    ):
        GitHubDeviceAuth._raise_for_api_error(
            {"message": "Remove heading syntax from README description"},
            "created commit",
        )


def test_public_repository_listing_only_returns_public_owned_repositories() -> None:
    calls: list[tuple[str, str, Mapping[str, str], bytes | None]] = []

    def transport(
        method: str, url: str, headers: Mapping[str, str], body: bytes | None
    ) -> list[dict[str, Any]]:
        calls.append((method, url, headers, body))
        return [
            {"full_name": "octocat/hello-world", "html_url": "https://github.com/octocat/hello-world", "private": False},
            {"full_name": "octocat/private", "html_url": "https://github.com/octocat/private", "private": True},
        ]

    auth = GitHubDeviceAuth(
        "client",
        transport=transport,
        token_store=KeyringTokenStore(FakeKeyring()),
    )

    repositories = auth.list_public_repositories("octocat")

    assert [repository["full_name"] for repository in repositories] == [
        "octocat/hello-world"
    ]
    method, url, headers, body = calls[0]
    assert method == "GET"
    assert url.startswith("https://api.github.com/users/octocat/repos?")
    assert "type=owner" in url
    assert "per_page=100&page=1" in url
    assert "Authorization" not in headers
    assert body is None


def test_public_repository_listing_paginates_all_results() -> None:
    urls: list[str] = []

    def transport(
        _method: str, url: str, _headers: Mapping[str, str], _body: bytes | None
    ) -> list[dict[str, Any]]:
        urls.append(url)
        if "&page=1" in url:
            return [
                {
                    "full_name": f"octocat/repo-{index}",
                    "html_url": f"https://github.com/octocat/repo-{index}",
                    "private": False,
                }
                for index in range(100)
            ]
        return [
            {
                "full_name": "octocat/final",
                "html_url": "https://github.com/octocat/final",
                "private": False,
            }
        ]

    auth = GitHubDeviceAuth(
        "client",
        transport=transport,
        token_store=KeyringTokenStore(FakeKeyring()),
    )

    repositories = auth.list_all_public_repositories("octocat")

    assert len(repositories) == 101
    assert "page=1" in urls[0]
    assert "page=2" in urls[1]


@pytest.mark.parametrize(("page", "per_page"), [(0, 100), (1, 101)])
def test_public_repository_listing_validates_pagination(
    page: int, per_page: int
) -> None:
    auth = GitHubDeviceAuth(
        "client", token_store=KeyringTokenStore(FakeKeyring())
    )
    with pytest.raises(ValueError):
        auth.list_public_repositories("octocat", page=page, per_page=per_page)


def test_branch_listing_uses_public_endpoint_and_validates_branch_data() -> None:
    calls: list[tuple[str, str, Mapping[str, str], bytes | None]] = []

    def transport(
        method: str, url: str, headers: Mapping[str, str], body: bytes | None
    ) -> list[dict[str, Any]]:
        calls.append((method, url, headers, body))
        return [
            {"name": "main", "commit": {"sha": "commit-sha"}},
            {"name": "release/1.0", "commit": {"sha": "release-sha"}},
        ]

    auth = GitHubDeviceAuth(
        "client", transport=transport, token_store=KeyringTokenStore(FakeKeyring())
    )

    branches = auth.list_repository_branches("octocat/hello-world")

    assert branches == [
        {"name": "main", "commit_sha": "commit-sha"},
        {"name": "release/1.0", "commit_sha": "release-sha"},
    ]
    method, url, headers, body = calls[0]
    assert method == "GET"
    assert url == "https://api.github.com/repos/octocat/hello-world/branches?per_page=100&page=1"
    assert "Authorization" not in headers
    assert body is None


def test_branch_listing_paginates_and_bounds_large_branch_sets() -> None:
    calls: list[str] = []

    def transport(
        _method: str, url: str, _headers: Mapping[str, str], _body: bytes | None
    ) -> list[dict[str, Any]]:
        calls.append(url)
        if "&page=1" in url:
            return [
                {"name": f"branch-{index}", "commit": {"sha": f"sha-{index}"}}
                for index in range(100)
            ]
        return [{"name": "tail", "commit": {"sha": "tail-sha"}}]

    auth = GitHubDeviceAuth(
        "client", transport=transport, token_store=KeyringTokenStore(FakeKeyring())
    )
    assert len(auth.list_repository_branches("octocat/repo")) == 101
    assert "page=2" in calls[1]


def test_repository_tree_uses_public_commit_and_tree_endpoints() -> None:
    calls: list[tuple[str, str, Mapping[str, str], bytes | None]] = []

    def transport(
        method: str, url: str, headers: Mapping[str, str], body: bytes | None
    ) -> dict[str, Any]:
        calls.append((method, url, headers, body))
        if "/git/commits/" in url:
            return {"tree": {"sha": "tree-sha"}}
        return {
            "truncated": False,
            "tree": [
                {"path": "src/main.py", "type": "blob"},
                {"path": "src", "type": "tree"},
                {"path": "README.md", "type": "blob"},
            ],
        }

    auth = GitHubDeviceAuth(
        "client", transport=transport, token_store=KeyringTokenStore(FakeKeyring())
    )

    assert auth.get_repository_tree("octocat/repo", "commit-sha") == [
        "README.md",
        "src/main.py",
    ]
    assert "git/commits/commit-sha" in calls[0][1]
    assert "git/trees/tree-sha?recursive=1" in calls[1][1]
    assert all("Authorization" not in call[2] for call in calls)


def test_repository_tree_rejects_truncated_trees() -> None:
    replies = iter(
        [
            {"tree": {"sha": "tree-sha"}},
            {"truncated": True, "tree": []},
        ]
    )
    auth = GitHubDeviceAuth(
        "client",
        transport=lambda *_args: next(replies),
        token_store=KeyringTokenStore(FakeKeyring()),
    )

    with pytest.raises(GitHubAuthError, match="truncated"):
        auth.get_repository_tree("octocat/repo", "commit-sha")


def test_public_file_download_uses_unauthenticated_raw_endpoint() -> None:
    calls: list[tuple[str, Mapping[str, str]]] = []

    def binary_transport(url: str, headers: Mapping[str, str]) -> bytes:
        calls.append((url, headers))
        return b"\x00\xfffile contents"

    auth = GitHubDeviceAuth(
        "client",
        binary_transport=binary_transport,
        token_store=KeyringTokenStore(FakeKeyring()),
    )

    assert auth.download_public_file(
        "octocat/hello-world", "feature/readme", "src/main file.py"
    ) == b"\x00\xfffile contents"
    url, headers = calls[0]
    assert url == (
        "https://raw.githubusercontent.com/octocat/hello-world/"
        "feature/readme/src/main%20file.py"
    )
    assert headers["Accept"] == "application/octet-stream"
    assert "Authorization" not in headers


@pytest.mark.parametrize("path", ["../secret", "/rooted", "a\\b", "a//b"])
def test_public_file_download_rejects_unsafe_paths(path: str) -> None:
    auth = GitHubDeviceAuth(
        "client",
        binary_transport=lambda *_args: pytest.fail("unsafe path reached transport"),
        token_store=KeyringTokenStore(FakeKeyring()),
    )
    with pytest.raises(ValueError):
        auth.download_public_file("octocat/repo", "main", path)


def test_commit_and_push_creates_commit_then_updates_branch_non_force() -> None:
    keyring = FakeKeyring()
    store = KeyringTokenStore(keyring)
    store.save("fake-write-token")
    calls: list[tuple[str, str, Mapping[str, str], bytes | None]] = []
    blob_index = [0]
    repo_url = "https://api.github.com/repos/octocat/repo"

    ref_reads = [0]

    def transport(
        method: str, url: str, headers: Mapping[str, str], body: bytes | None
    ) -> dict[str, Any]:
        calls.append((method, url, headers, body))
        payload = json.loads(body) if body else {}
        if url == USER_URL:
            return {"login": "octocat"}
        if url == repo_url:
            return {
                "private": False,
                "owner": {"login": "octocat"},
                "permissions": {"push": True},
            }
        if url == f"{repo_url}/git/ref/heads/main":
            ref_reads[0] += 1
            return {
                "ref": "refs/heads/main",
                "object": {"sha": "head-sha", "type": "commit"},
            }
        if url == f"{repo_url}/git/commits/head-sha":
            return {
                "message": "Remove heading syntax from README description",
                "tree": {"sha": "base-tree"},
            }
        if url == f"{repo_url}/git/trees/base-tree?recursive=1":
            return {
                "truncated": False,
                "tree": [{"path": "README.md", "type": "blob"}],
            }
        if method == "POST" and url == f"{repo_url}/git/blobs":
            assert payload["encoding"] == "base64"
            blob_index[0] += 1
            return {"sha": f"blob-{blob_index[0]}"}
        if method == "POST" and url == f"{repo_url}/git/trees":
            assert payload["base_tree"] == "base-tree"
            assert [item["path"] for item in payload["tree"]] == [
                "README.md",
                "src/main.py",
            ]
            return {"sha": "new-tree"}
        if method == "POST" and url == f"{repo_url}/git/commits":
            assert payload == {
                "message": "Improve docs",
                "tree": "new-tree",
                "parents": ["head-sha"],
            }
            return {"sha": "new-commit"}
        if method == "PATCH" and url == f"{repo_url}/git/refs/heads/main":
            assert payload == {"sha": "new-commit", "force": False}
            return {
                "ref": "refs/heads/main",
                "object": {"sha": "new-commit", "type": "commit"},
            }
        pytest.fail(f"Unexpected GitHub request: {method} {url}")

    auth = GitHubDeviceAuth(
        "client",
        transport=transport,
        token_store=store,
    )
    preview = auth.prepare_public_push(
        "octocat/repo", "main", ["README.md", "src/main.py"]
    )
    assert preview.overwrite_paths == ("README.md",)
    assert preview.target_paths == ("README.md", "src/main.py")

    commit_sha = auth.commit_and_push_public_files(
        "octocat/repo",
        "main",
        preview,
        {"README.md": b"updated", "src/main.py": b"new"},
        "Improve docs",
    )

    assert commit_sha == "new-commit"
    assert any(call[0] == "POST" and call[1] == f"{repo_url}/git/commits" for call in calls)
    assert any(call[0] == "PATCH" for call in calls)
    authenticated_calls = [call for call in calls if call[0] != "GET" or call[1] != f"{repo_url}/git/trees/base-tree?recursive=1"]
    assert authenticated_calls
    assert all(
        call[2].get("Authorization") == "Bearer fake-write-token"
        for call in authenticated_calls
    )
    patch_call = next(call for call in calls if call[0] == "PATCH")
    assert json.loads(patch_call[3] or b"")["force"] is False


def test_public_push_rejects_private_or_unowned_repository() -> None:
    store = KeyringTokenStore(FakeKeyring())
    store.save("fake-write-token")
    replies = iter(
        [
            {"login": "octocat"},
            {
                "private": True,
                "owner": {"login": "octocat"},
                "permissions": {"push": True},
            },
        ]
    )
    auth = GitHubDeviceAuth(
        "client",
        transport=lambda *_args: next(replies),
        token_store=store,
    )
    with pytest.raises(GitHubAuthError, match="public repositories owned"):
        auth.prepare_public_push("octocat/private", "main", ["README.md"])


def test_public_push_aborts_if_branch_moves_after_confirmation() -> None:
    store = KeyringTokenStore(FakeKeyring())
    store.save("fake-write-token")
    ref_reads = [0]
    repo_url = "https://api.github.com/repos/octocat/repo"

    def transport(
        method: str, url: str, _headers: Mapping[str, str], _body: bytes | None
    ) -> dict[str, Any]:
        if url == USER_URL:
            return {"login": "octocat"}
        if url == repo_url:
            return {
                "private": False,
                "owner": {"login": "octocat"},
                "permissions": {"push": True},
            }
        if url == f"{repo_url}/git/ref/heads/main":
            ref_reads[0] += 1
            sha = "head-sha" if ref_reads[0] == 1 else "moved-head"
            return {"ref": "refs/heads/main", "object": {"sha": sha}}
        if url == f"{repo_url}/git/commits/head-sha":
            return {
                "message": "Remove heading syntax from README description",
                "tree": {"sha": "base-tree"},
            }
        if url == f"{repo_url}/git/trees/base-tree?recursive=1":
            return {"truncated": False, "tree": []}
        pytest.fail(f"Unexpected API call after branch changed: {method} {url}")

    auth = GitHubDeviceAuth(
        "client",
        transport=transport,
        token_store=store,
    )
    preview = auth.prepare_public_push("octocat/repo", "main", ["new.txt"])
    with pytest.raises(GitHubAuthError, match="branch changed"):
        auth.create_pending_public_commit(
            "octocat/repo",
            "main",
            preview,
            {"new.txt": b"data"},
            "Add new file",
        )


def test_combined_commit_push_aborts_if_branch_moves_before_ref_update() -> None:
    store = KeyringTokenStore(FakeKeyring())
    store.save("fake-write-token")
    ref_reads = [0]
    writes: list[str] = []
    repo_url = "https://api.github.com/repos/octocat/repo"

    def transport(
        method: str, url: str, _headers: Mapping[str, str], body: bytes | None
    ) -> dict[str, Any]:
        if url == USER_URL:
            return {"login": "octocat"}
        if url == repo_url:
            return {
                "private": False,
                "owner": {"login": "octocat"},
                "permissions": {"push": True},
            }
        if url == f"{repo_url}/git/ref/heads/main":
            ref_reads[0] += 1
            sha = "moved-head" if ref_reads[0] == 3 else "head-sha"
            return {"ref": "refs/heads/main", "object": {"sha": sha}}
        if url == f"{repo_url}/git/commits/head-sha":
            return {"tree": {"sha": "base-tree"}}
        if url == f"{repo_url}/git/trees/base-tree?recursive=1":
            return {"truncated": False, "tree": []}
        if method == "POST" and url == f"{repo_url}/git/blobs":
            writes.append(method)
            return {"sha": "blob-sha"}
        if method == "POST" and url == f"{repo_url}/git/trees":
            writes.append(method)
            return {"sha": "new-tree"}
        if method == "POST" and url == f"{repo_url}/git/commits":
            writes.append(method)
            return {"sha": "new-commit"}
        if method == "PATCH" and url == f"{repo_url}/git/refs/heads/main":
            writes.append(method)
            pytest.fail("A raced branch ref must not be updated.")
        pytest.fail(f"Unexpected API call: {method} {url}")

    auth = GitHubDeviceAuth(
        "client",
        transport=transport,
        token_store=store,
    )
    preview = auth.prepare_public_push("octocat/repo", "main", ["new.txt"])

    with pytest.raises(GitHubAuthError, match="branch changed"):
        auth.commit_and_push_public_files(
            "octocat/repo",
            "main",
            preview,
            {"new.txt": b"data"},
            "Add new file",
        )

    assert writes == ["POST", "POST", "POST"]


def test_keyring_errors_are_reported_without_echoing_token() -> None:
    class BrokenKeyring:
        @staticmethod
        def set_password(_service: str, _account: str, _token: str) -> None:
            raise RuntimeError("fake-access-token")

    with pytest.raises(CredentialStoreError) as error:
        KeyringTokenStore(BrokenKeyring()).save("fake-access-token")
    assert "fake-access-token" not in str(error.value)


def test_http_transport_surfaces_network_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    def unavailable(*_args: Any, **_kwargs: Any) -> None:
        raise urllib.error.URLError("offline")

    monkeypatch.setattr("urllib.request.urlopen", unavailable)

    with pytest.raises(GitHubAuthError, match="Could not connect to GitHub"):
        _http_json("GET", USER_URL, {}, None)
