"""Acceptance for v0.2: one task over a git repository and a browser session, through the MCP server.

A scripted client makes the calls that an agent makes. benchmarks/agent_mcp.py runs the same task
with a language model.
"""
import importlib.util
import sys
from pathlib import Path

import pytest

pytest.importorskip("mcp")
pytest.importorskip("playwright")
import anyio  # noqa: E402
from mcp import Client, StdioServerParameters  # noqa: E402

spec = importlib.util.spec_from_file_location("task_fixture", Path(__file__).parents[2] / "benchmarks" / "task_fixture.py")
fixture = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fixture)


def test_one_task_uses_a_git_handle_and_a_browser_handle_through_mcp(tmp_path):
    site, url = fixture.serve_site()
    try:
        repo = fixture.make_repo(tmp_path / "repo", url)
        command = StdioServerParameters(command=sys.executable, args=[
            "-m", "datahandles.protocols.mcp", "--state", str(tmp_path / "state"),
            "--register", f"repo=git:{repo}", "--register", "web=browser:"])

        async def session():
            async with Client(command) as client:
                async def call(name, arguments):
                    result = await client.call_tool(name, arguments)
                    assert not result.is_error, result.structured_content
                    return result.structured_content

                found = {h["name"]: h for h in (await call("handle_find", {}))["handles"]}
                repo_id, web_id = found["repo"]["id"], found["web"]["id"]
                assert (found["repo"]["type"], found["web"]["type"]) == ("repository", "browser_session")
                caps = [c["name"] for c in (await call("handle_describe", {"handle": web_id}))["capabilities"]]
                files = (await call("handle_invoke", {"handle": repo_id, "capability": "repo.files", "input": {}}))["output"]["files"]
                ops = (await call("handle_invoke", {"handle": repo_id, "capability": "repo.read", "input": {"path": "docs/OPERATIONS.md"}}))["output"]["text"]
                status_url = next(line.split(": ", 1)[1] for line in ops.splitlines() if line.startswith("Status page"))
                page = (await call("handle_invoke", {"handle": web_id, "capability": "browser.goto", "input": {"url": status_url}}))["output"]
                notes = (await call("handle_invoke", {"handle": web_id, "capability": "browser.click", "input": {"selector": "text=Release notes"}}))["output"]
                text = (await call("handle_invoke", {"handle": web_id, "capability": "browser.text", "input": {"selector": "h2"}}))["output"]["text"]
                return caps, files, status_url, page, notes, text

        caps, files, status_url, page, notes, text = anyio.run(session)
        assert "browser.goto" in caps and "docs/OPERATIONS.md" in files
        assert status_url == url + "/status" and page["title"] == "Shop service status"
        assert notes["title"] == "Release notes" and text.startswith(fixture.ANSWER)
    finally:
        site.shutdown()
        site.server_close()
