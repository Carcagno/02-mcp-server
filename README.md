# 02-mcp-server

An MCP (Model Context Protocol) server that exposes a full git/GitHub workflow — read, write, and destructive operations — as tools an LLM agent can call. Built and tested against Claude Desktop, using the Python `FastMCP` SDK over stdio.

The server operates exclusively against a **sandbox repository**: destructive capabilities (force-push, branch deletion, merging pull requests) are deliberately included rather than restricted, with the safety boundary enforced by isolating the blast radius to a disposable repository instead of limiting what the tools can do.

Beyond tools, the project covers all three MCP primitives end-to-end:

- **Tools** — the model decides when to call them.
- **Resources** — a human attaches them; three static resources expose text files from the sandbox repository.
- **A Prompt** — a human triggers it; `open_standard_pr(branch_name, description)` seeds a ready-made message for opening a pull request.

A companion **Agent Skill** (`skills/github-sandbox-branch-sync`) packages a procedural rule the model did not reliably follow on its own (see below).

## Demonstrated agentic skills

- **Tool design around intent, not 1:1 command mirroring.** `commit_changes` folds `git add` and `git commit` together (staging alone has no independent use case for an agent); `push` stays a separate tool, because a local commit and a published one are genuinely different states worth letting the agent distinguish.
- **Sandboxing as the primary safety mechanism.** The target repository path (`PLAYGROUND_REPO_PATH`) is server configuration, never a tool parameter — the model can request an action but can never redirect these tools at a different repository. Destructive tools stay in the toolset; the blast radius is what's fixed.
- **Input validation on model-controlled parameters.** Path-traversal protection on file writes (`write_file`); rejection of branch names shaped like git flags (e.g. `--upload-pack=...`) to close off option injection, layered on top of `subprocess`'s own protection against shell injection.
- **Hard guardrails on irreversible actions.** `main` is protected against force-push and deletion in code, not only in documentation — every destructive tool checks the current branch before acting.
- **Debugging a live agent integration.** A tool worked in isolation but hung silently once wired into Claude Desktop. Diagnosed with a side-effect-free `ping` tool to isolate "the MCP transport is broken" from "this specific tool is blocked," tracing the real cause to how Windows handles a console subprocess spawned from a GUI-launched parent.
- **Evaluating agent behavior instead of trusting it.** Compared the same multi-tool task across two models: one incorrectly claimed a tool didn't exist (disproven by calling it directly) while the other correctly diagnosed a real gap and stopped to ask rather than improvise a workaround.
- **Closing a reliability gap with a Skill, not just documentation.** An available `pull` tool was not being called spontaneously before branching from a possibly-stale `main`, reproduced identically across three separate runs. Rather than rely on prompting alone, the missing procedure was packaged as `github-sandbox-branch-sync`, a Skill the model loads and follows.
- **Verifying before trusting an integration.** Every new primitive (Resources, Prompt) was validated first via a standalone MCP client (`test_client.py`) importing the exact same SDK version as the runtime environment, before ever testing inside Claude Desktop.

## Architecture

```
MCP client (e.g. Claude Desktop)
        │  stdio transport
        ▼
   server.py  (FastMCP)
        │
        ├── git subprocess ──────────► sandbox repository (PLAYGROUND_REPO_PATH)
        │                                        ▲
        └── GitHub REST API (httpx) ─────────────┘
             (pull requests, issues — scoped to that repo's remote)
```

- **`server.py`** — the server itself: 18 tools, 3 resources, 1 prompt.
  - *Read tools*: `ping`, `git_status`, `list_branches`, `list_pull_requests`, `get_pull_request`, `list_issues`.
  - *Write tools*: `write_file`, `commit_changes`, `push` (auto-sets the upstream branch on a first push), `pull` (fast-forward only, fails loudly on divergence instead of creating a silent merge commit), `create_branch`, `switch_branch`, `create_pull_request`, `create_issue`.
  - *Destructive tools*, all refusing to touch `main`: `force_push`, `delete_branch`, `close_pull_request`, `merge_pull_request`.
  - *Resources*: `citations://chirac`, `citations://pompidou`, `citations://degaulle` — each reads its file from the sandbox repository on demand (not a cached snapshot).
  - *Prompt*: `open_standard_pr(branch_name, description)`.
- **`test_client.py`** — a standalone MCP client used to smoke-test the server (tool discovery, calls, and the two security checks — path traversal and branch-name injection) without needing an MCP client application at all.
- **`skills/github-sandbox-branch-sync/SKILL.md`** — the Agent Skill described above.

## Running it

```bash
python -m venv .venv
.venv/Scripts/pip install -r requirements.txt   # or .venv/bin/pip on macOS/Linux
```

The server needs a sandbox git repository to operate on — any repository you're willing to dedicate to letting an agent push, branch, and merge freely, with a GitHub remote already configured.

Environment variables (set in the MCP client's server configuration, never as a tool parameter):

| Variable | Required | Purpose |
|---|---|---|
| `PLAYGROUND_REPO_PATH` | yes | Absolute path to the sandbox repository. Fixes the blast radius. |
| `GITHUB_TOKEN` | only for PR/issue tools | A fine-grained GitHub token scoped to that one repository, with `Contents: Read and write` (needed even to *read* PR diffs, and required to merge) and `Pull requests`/`Issues: write`. |

Register the server with an MCP client by pointing it at `python` (or the venv's interpreter) running `server.py`, for example in Claude Desktop's config:

```json
{
  "mcpServers": {
    "github-sandbox": {
      "command": "/absolute/path/to/.venv/Scripts/python.exe",
      "args": ["/absolute/path/to/server.py"],
      "env": {
        "PLAYGROUND_REPO_PATH": "/absolute/path/to/your/sandbox-repo",
        "GITHUB_TOKEN": "your-fine-grained-token"
      }
    }
  }
}
```

To verify the server works before wiring it into any client, run the standalone smoke test instead:

```bash
PLAYGROUND_REPO_PATH=/tmp/fake_playground python test_client.py
```
