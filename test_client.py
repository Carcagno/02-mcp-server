"""Standalone smoke test: acts as a minimal MCP client to validate that
server.py starts, advertises its tools, and executes them correctly —
without needing Claude Desktop. Not part of the deliverable, just a
dev-time check."""

import asyncio
import os
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

SERVER_DIR = Path(__file__).parent


async def call(session, name, args=None):
    print(f"\n--- {name}({args or ''}) ---")
    result = await session.call_tool(name, args or {})
    for block in result.content:
        print(block.text)


async def main():
    params = StdioServerParameters(
        command="python3",
        args=[str(SERVER_DIR / "server.py")],
        env={**os.environ, "PLAYGROUND_REPO_PATH": "/tmp/fake_playground"},
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()

            tools = await session.list_tools()
            print("Tools discovered by the client:")
            for t in tools.tools:
                print(f"  - {t.name}")

            await call(session, "ping")
            await call(session, "git_status")
            await call(session, "write_file", {"path": "hello.txt", "content": "hello from mcp"})
            await call(session, "git_status")
            await call(session, "commit_changes", {"message": "Add hello.txt via MCP tool"})
            await call(session, "push")
            await call(session, "git_status")

            # Security check: this must be refused, not silently escape the repo
            await call(session, "write_file", {"path": "../../escape.txt", "content": "should be blocked"})

            await call(session, "list_branches")
            await call(session, "create_branch", {"name": "feature/mcp-test"})
            await call(session, "switch_branch", {"name": "feature/mcp-test"})
            await call(session, "list_branches")

            # Security check: a branch "name" shaped like a git flag must be refused
            await call(session, "create_branch", {"name": "--upload-pack=/bin/sh"})

            # Real-world case Sonnet hit: pushing a BRAND NEW branch (never
            # pushed before, no upstream configured) must auto-set it
            # instead of failing. Use a fresh name so this repo (reused
            # across test runs) doesn't already have it tracked.
            await call(session, "create_branch", {"name": "never-pushed-before"})
            await call(session, "switch_branch", {"name": "never-pushed-before"})
            await call(session, "write_file", {"path": "on_new_branch.txt", "content": "hi"})
            await call(session, "commit_changes", {"message": "on new branch"})
            await call(session, "push")

            # Destructive tools: force_push and delete_branch must work on a
            # feature branch, and must both REFUSE to touch 'main'.
            await call(session, "write_file", {"path": "on_new_branch.txt", "content": "amended"})
            await call(session, "commit_changes", {"message": "amend"})
            await call(session, "force_push")  # amend+force-push on a non-main branch: allowed
            await call(session, "switch_branch", {"name": "main"})
            await call(session, "force_push")  # must be refused (protected branch)
            await call(session, "delete_branch", {"name": "main"})  # must be refused
            await call(session, "delete_branch", {"name": "never-pushed-before"})  # allowed


asyncio.run(main())
