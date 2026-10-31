# MiniGit

MiniGit is a dark-themed PySide6 desktop app for selecting files and
downloading from or committing to public GitHub repositories. It supports
selecting individual files or recursively adding a folder, excluding `.git`
and `node_modules`, removing selected files, and showing the current selection
count. Full paths are shown
so files with the same name remain distinguishable.

GitHub sign-in uses OAuth Device Flow. MiniGit displays the one-time code,
copies it on request, and opens GitHub in the browser; the resulting access
token is stored only through the operating-system keyring. Once connected, it
lists public repositories owned by the account and lets you select one. MiniGit
loads the selected repository's branches and current file tree, and shows the
remote tree beside the local files selected for a commit. You can multi-select
remote files and download them, preserving repository paths under a chosen
local folder; MiniGit asks before replacing existing local files. This is a
read-only download, not a Git pull operation. For public repositories you own,
MiniGit can create a commit and update the selected branch after an explicit
confirmation. Private and collaborator-only repositories, cloning, and other
Git operations are not supported. MiniGit does not write tokens to config,
files, or logs.

## Run

```powershell
python -m pip install -e ".[test]"
python -m minigit
```

## Configure GitHub sign-in

1. Register a GitHub OAuth App in your GitHub account or organization settings.
2. Enable **Device Flow** in that app's settings. MiniGit requests
   `read:user public_repo`: `read:user` identifies the account, while
   `public_repo` enables commits to public repositories. Repository discovery
   continues to use the unauthenticated public repositories endpoint.
3. MiniGit is preconfigured with the supplied public OAuth client ID. No client
   secret is needed. To use a different OAuth App, optionally override the
   client ID in the environment before starting MiniGit:

   ```powershell
   $env:MINIGIT_GITHUB_CLIENT_ID = "YOUR_OAUTH_APP_CLIENT_ID"
   python -m minigit
   ```

The client ID is public OAuth application configuration, not a credential,
and OAuth Device Flow does not use a client secret. `public_repo` is a
write-capable scope for public repositories the account can access; MiniGit's
UI and push API additionally enforce that the repository is public, owned by
the connected account, and pushable. MiniGit does not request the broader
private-repository `repo` scope. Existing connections must select
**Enable public push** and approve the updated scope; the new token replaces
the prior token only after successful sign-in and remains in the OS keyring.
MiniGit remembers whether that authorization included `public_repo` alongside
the token in the same keyring entry, so a successful write authorization stays
enabled after restarting the app. Older keyring entries are treated as
read-only and need this one-time reauthorization.
The optional `MINIGIT_GITHUB_CLIENT_ID` override applies to the terminal
session running MiniGit. Use **Disconnect** to remove the locally saved token.
To revoke authorization at GitHub, remove MiniGit from **Settings >
Applications > Authorized OAuth Apps** in your GitHub account.

## Repository comparison and target paths

The branch picker displays the chosen branch's current tracked file tree
alongside selected local files. Files selected together individually retain
their hierarchy relative to the common parent directory (a single file uses
its basename); files from a folder retain paths relative to that selected
folder. When individual files are added in separate selections, MiniGit
recalculates their shared parent so same-named files in separate subfolders
remain distinguishable. MiniGit flags case-insensitive local target-path
collisions rather than silently flattening ambiguous files. The preview also
identifies files that already exist in the selected branch and file/directory
path conflicts. Before pushing, MiniGit checks the latest branch head and
complete file tree. It blocks local duplicate targets, case-only ambiguities,
and file/directory conflicts. The confirmation lists every selected target
and every existing repository file that will be overwritten. No repository
changes occur unless the user confirms.

To download repository files, select one or more entries in **Current
repository files** and choose **Download Selected**. For one file, a save-file
dialog opens with the repository's original basename as the suggested name.
For multiple files, choose a destination folder and repository subfolders are
recreated beneath it. If any target files already exist, MiniGit lists them
and requires explicit confirmation before replacing them. Invalid or unsafe
repository paths are rejected.

To commit and push selected local files, enter a non-empty commit message and
choose **Commit & Push**. Before any write, MiniGit shows the repository,
branch, message, selected target paths, and exact existing paths that would be
overwritten; it requires confirmation even when no overwrites are detected.
MiniGit creates the commit and updates the branch without force. It rechecks
the branch head around commit creation and aborts if the branch moved, rather
than overwriting newer commits. Success is reported only after GitHub confirms
the branch update. Files are limited to 50 MiB each and 100 MiB total per
commit.

Press **F11** to toggle full screen and **Escape** to leave. MiniGit restores
the previous window size when exiting. The interface uses a simple dark theme
with blue accents and opens at 1080 × 700 to fit typical displays better. The
connected-account panel shows
**Enable public push** when the saved connection lacks the required
write-capable authorization; approving it replaces the saved keyring token
only after sign-in succeeds.

## Test

```powershell
python -m pytest
```
