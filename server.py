"""MCP server exposing git/GitHub tools scoped to a sandbox playground repository.

Design choice: the target repository path is NOT a tool parameter the model
can set. It comes from an environment variable (PLAYGROUND_REPO_PATH), set
once in the MCP client config (e.g. Claude Desktop's config file). This means
the model can request an action, but it can never redirect these tools at a
different repository — the blast radius is fixed by the human operator, not
by the agent.

Tool granularity: write operations are grouped by *intent*, not by 1:1
mirroring of git subcommands (write_file, commit_changes, push) — staging
alone has no independent use case in an agent-driven flow, so `add` is
folded into `commit_changes`. `push` stays a separate step on purpose: a
local commit and a published/shared commit are genuinely different states.
"""

import os
import subprocess
import sys
from pathlib import Path

import httpx
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("github-sandbox")

PLAYGROUND_REPO = Path(os.environ["PLAYGROUND_REPO_PATH"]).resolve()

# Optional: only needed for the GitHub API tools (pull requests, issues) —
# git itself has no notion of these, they're GitHub-specific, so plain git
# commands can't create them. Like PLAYGROUND_REPO_PATH, this comes from the
# server's env config, never from a tool parameter.
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN")

# On Windows, spawning a console app (git.exe) from a GUI/sandboxed parent
# can behave oddly regarding console allocation. CREATE_NO_WINDOW tells
# Windows not to allocate/attach a console for the child at all — the fix
# that resolved a real silent-hang bug encountered while building this.
_POPEN_KWARGS = {}
if sys.platform == "win32":
    _POPEN_KWARGS["creationflags"] = subprocess.CREATE_NO_WINDOW


def _git(*args: str, timeout: int = 15) -> str:
    """Run a git command inside the sandbox playground repository and return
    its output. Shared by every tool below so the Windows hardening and
    error handling only live in one place."""
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=PLAYGROUND_REPO,
            capture_output=True,
            text=True,
            timeout=timeout,
            stdin=subprocess.DEVNULL,  # never inherit the MCP transport's stdin pipe
            **_POPEN_KWARGS,
        )
    except subprocess.TimeoutExpired:
        return f"ERROR: command 'git {' '.join(args)}' timed out after {timeout}s."
    except Exception as exc:
        return f"UNEXPECTED ERROR: {exc!r}"
    return result.stdout or result.stderr or "(no output)"


def _safe_path(relative: str) -> Path:
    """Resolve `relative` against the playground repo and refuse anything
    that would escape it (e.g. "../../elsewhere/evil.txt").

    Unlike PLAYGROUND_REPO itself, `relative` IS a model-controlled tool
    parameter — this is the guard against path traversal for that specific
    input, the first tool parameter in this server that touches the
    filesystem directly.
    """
    target = (PLAYGROUND_REPO / relative).resolve()
    if target != PLAYGROUND_REPO and PLAYGROUND_REPO not in target.parents:
        raise ValueError(f"path escapes the playground repository: {relative!r}")
    return target


def _repo_slug() -> str:
    """Derive 'owner/repo' from the playground's git remote 'origin' URL —
    handles both HTTPS (https://github.com/owner/repo.git) and SSH
    (git@github.com:owner/repo.git) remote forms."""
    url = _git("remote", "get-url", "origin").strip()
    if url.startswith("git@github.com:"):
        path = url.split("git@github.com:", 1)[1]
    elif "github.com/" in url:
        path = url.split("github.com/", 1)[1]
    else:
        raise ValueError(f"remote 'origin' not recognized as GitHub: {url!r}")
    return path[:-4] if path.endswith(".git") else path


def _github_api(method: str, path: str, **kwargs) -> str:
    """Call the GitHub REST API scoped to the playground repo and return the
    raw response body. Shared by every PR/issue tool below."""
    if not GITHUB_TOKEN:
        return "ERROR: GITHUB_TOKEN not configured (MCP server environment variable)."
    try:
        slug = _repo_slug()
    except ValueError as exc:
        return f"ERROR: {exc}"
    try:
        response = httpx.request(
            method,
            f"https://api.github.com/repos/{slug}{path}",
            headers={
                "Authorization": f"Bearer {GITHUB_TOKEN}",
                "Accept": "application/vnd.github+json",
            },
            timeout=15,
            **kwargs,
        )
    except Exception as exc:
        return f"NETWORK ERROR: {exc!r}"
    if response.status_code >= 400:
        return f"GitHub API ERROR ({response.status_code}): {response.text[:500]}"
    return response.text


@mcp.tool()
def ping() -> str:
    """Diagnostic tool: returns immediately, touches no subprocess. Used to
    isolate whether a hang comes from the MCP transport itself or from the
    subprocess call inside other tools."""
    return "pong"


@mcp.tool()
def git_status() -> str:
    """Return the current `git status` of the sandbox playground repository."""
    return _git("status")


@mcp.tool()
def write_file(path: str, content: str) -> str:
    """Create or overwrite a text file inside the sandbox playground
    repository. `path` is relative to the repository root, e.g.
    "notes/todo.txt". Does not commit or push — call commit_changes and
    push separately to publish the change."""
    try:
        target = _safe_path(path)
    except ValueError as exc:
        return f"ERROR: {exc}"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    return f"File written: {path}"


@mcp.tool()
def commit_changes(message: str) -> str:
    """Stage all pending changes and commit them in the sandbox playground
    repository. Does not push — call push separately to publish."""
    add_result = _git("add", "-A")
    if add_result.startswith(("ERROR:", "UNEXPECTED ERROR:")):
        return add_result
    return _git("commit", "-m", message)


@mcp.tool()
def push() -> str:
    """Push committed changes in the sandbox playground repository to
    GitHub. Automatically sets the upstream tracking branch the first time
    a newly created branch is pushed. Note: can fail if git needs
    interactive credentials that aren't cached (SSH key / HTTPS credential
    helper)."""
    result = _git("push", timeout=30)
    if "has no upstream branch" in result:
        branch = _git("branch", "--show-current").strip()
        if branch:
            result = _git("push", "--set-upstream", "origin", branch, timeout=30)
    return result


@mcp.tool()
def pull() -> str:
    """Fetch and fast-forward the current branch from GitHub. Fails loudly
    (a plain git error, never a silent merge commit) if the local branch
    has diverged from its remote counterpart.

    Call this on "main" before creating a new branch from it whenever a
    pull request may have been merged since the last sync:
    merge_pull_request updates GitHub directly through the REST API and
    never touches this local clone, so a stale local "main" can cause a
    brand new branch to conflict with the real main without any tool
    reporting an error until the pull request is opened."""
    return _git("pull", "--ff-only", timeout=30)


def _validate_branch_name(name: str) -> str | None:
    """Return an error message if `name` is unsafe/invalid, else None.

    `name` is model-controlled. Rejecting anything starting with "-" closes
    off git option injection (e.g. a branch "name" crafted to look like a
    git flag, such as "--upload-pack=...") — subprocess with a list of args
    already prevents shell injection, this is a different, git-specific
    risk on top of that.
    """
    if not name or name.startswith("-"):
        return f"ERROR: invalid branch name: {name!r}"
    return None


@mcp.tool()
def list_branches() -> str:
    """List all local branches in the sandbox playground repository."""
    return _git("branch", "--list")


@mcp.tool()
def create_branch(name: str) -> str:
    """Create a new branch (without switching to it) in the sandbox
    playground repository."""
    error = _validate_branch_name(name)
    if error:
        return error
    return _git("branch", name)


@mcp.tool()
def switch_branch(name: str) -> str:
    """Switch the sandbox playground repository to an existing branch."""
    error = _validate_branch_name(name)
    if error:
        return error
    return _git("checkout", name)


@mcp.tool()
def create_pull_request(title: str, head: str, base: str = "main", body: str = "") -> str:
    """Open a pull request on GitHub, from branch `head` into `base`
    (default "main"), in the sandbox playground repository. Requires
    GITHUB_TOKEN to be configured on the server."""
    return _github_api(
        "POST", "/pulls", json={"title": title, "head": head, "base": base, "body": body}
    )


@mcp.tool()
def list_pull_requests() -> str:
    """List open pull requests in the sandbox playground repository."""
    return _github_api("GET", "/pulls")


@mcp.tool()
def get_pull_request(number: int) -> str:
    """Get the current status of a pull request by its number, including
    whether it can be merged without conflicts. Check this before calling
    merge_pull_request: the response's `mergeable` field is true/false/null
    (null means GitHub is still computing it — retry shortly), and
    `mergeable_state` gives more detail (e.g. "clean", "dirty", "blocked")."""
    return _github_api("GET", f"/pulls/{number}")


@mcp.tool()
def create_issue(title: str, body: str = "") -> str:
    """Create an issue on GitHub in the sandbox playground repository."""
    return _github_api("POST", "/issues", json={"title": title, "body": body})


@mcp.tool()
def list_issues() -> str:
    """List open issues in the sandbox playground repository."""
    return _github_api("GET", "/issues")


def _current_branch() -> str:
    return _git("branch", "--show-current").strip()


@mcp.tool()
def force_push() -> str:
    """Force-push the current branch to GitHub, overwriting the remote
    history for that branch. DESTRUCTIVE — refuses to run on 'main' to
    protect the default branch from an accidental history rewrite."""
    branch = _current_branch()
    if branch == "main":
        return "ERROR: force-push refused on 'main' (branch protected by this server)."
    return _git("push", "--force", timeout=30)


@mcp.tool()
def delete_branch(name: str) -> str:
    """Permanently delete a branch, both locally and on GitHub. DESTRUCTIVE
    — refuses to delete 'main'."""
    error = _validate_branch_name(name)
    if error:
        return error
    if name == "main":
        return "ERROR: deletion of 'main' refused (branch protected by this server)."
    local_result = _git("branch", "-D", name)
    remote_result = _git("push", "origin", "--delete", name, timeout=30)
    return f"Local: {local_result}\nRemote: {remote_result}"


@mcp.tool()
def close_pull_request(number: int) -> str:
    """Close a pull request on GitHub without merging it, by its number.
    DESTRUCTIVE (discards the PR without integrating its changes)."""
    return _github_api("PATCH", f"/pulls/{number}", json={"state": "closed"})


@mcp.tool()
def merge_pull_request(number: int, merge_method: str = "merge") -> str:
    """Merge a pull request into its base branch, by its number. DESTRUCTIVE
    (integrates the PR's changes and closes it; not trivially reversible).
    Call get_pull_request first to confirm `mergeable` is true — GitHub
    refuses the merge otherwise. `merge_method` is one of "merge" (default,
    a merge commit), "squash", or "rebase"."""
    if merge_method not in ("merge", "squash", "rebase"):
        return f"ERROR: invalid merge_method: {merge_method!r} (expected: merge, squash, or rebase)."
    return _github_api(
        "PUT", f"/pulls/{number}/merge", json={"merge_method": merge_method}
    )


def _read_citation_file(relative_path: str) -> str:
    """Read a text file from the sandbox playground repo for exposure as an
    MCP Resource. Reuses the same path-safety guard as write_file, even
    though these paths are hardcoded below (not model-controlled) — belt
    and suspenders costs nothing here."""
    try:
        target = _safe_path(relative_path)
    except ValueError as exc:
        return f"ERROR: {exc}"
    if not target.exists():
        return f"ERROR: file not found: {relative_path!r}"
    return target.read_text(encoding="utf-8")


# Resources are declared as static, individually-named URIs rather than a
# single "citations://{person}" template. A template would need its own
# lookup/validation logic and wouldn't show up as browsable items in a
# client's UI (only as a pattern to fill in) — for a first, small, known
# set of files, three concrete resources are more discoverable and no more
# code than a generic templated version would be.
@mcp.resource(
    "citations://chirac",
    name="Jacques Chirac quotes",
    description="Current content of the Jacques Chirac quotes file in the playground repository.",
)
def citations_chirac() -> str:
    return _read_citation_file("citations_chirac.md")


@mcp.resource(
    "citations://pompidou",
    name="Georges Pompidou quotes",
    description="Current content of the Georges Pompidou quotes file in the playground repository.",
)
def citations_pompidou() -> str:
    return _read_citation_file("citations_pompidou.md")


@mcp.resource(
    "citations://degaulle",
    name="Charles de Gaulle quotes",
    description="Current content of the Charles de Gaulle quotes file in the playground repository.",
)
def citations_degaulle() -> str:
    return _read_citation_file("citations/degaulle.md")


# A Prompt is a human-triggered, parameterized template — unlike a Tool
# (the model decides to call it) or a Skill (the model decides to load it).
# This one hardcodes, as plain instruction text, the same branch-sync
# discipline as the github-sandbox-branch-sync Skill: same rule, two
# different trigger mechanisms.
@mcp.prompt()
def open_standard_pr(branch_name: str, description: str) -> str:
    """Seed a message with the full standard sequence to prepare and open a
    pull request on the sandbox playground repo, starting from a synced
    main."""
    return (
        f"Prepare a new pull request on the github-sandbox playground repo "
        f"for: {description}\n\n"
        f"Follow this exact sequence:\n"
        f"1. switch_branch(\"main\")\n"
        f"2. pull()\n"
        f"3. create_branch(\"{branch_name}\")\n"
        f"4. switch_branch(\"{branch_name}\")\n"
        f"5. Make the change described above (write_file as needed)\n"
        f"6. commit_changes with a clear message describing the change\n"
        f"7. push()\n"
        f"8. create_pull_request with an appropriate title and this "
        f"description in the body: {description}"
    )


if __name__ == "__main__":
    mcp.run()
