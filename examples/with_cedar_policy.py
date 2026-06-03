"""Tool-use authorization via in-process Cedar policies.

No external service — Cedar evaluates the policy in this Python process via
the ``cedarpy`` bindings (Rust-backed). Install:

    pip install 'computer-agent-py[cedar]'
    export ANTHROPIC_API_KEY=sk-ant-...
    python examples/with_cedar_policy.py
"""

from __future__ import annotations

import asyncio
import os
import pathlib
import tempfile

from computeragent import ClaudeAgentOptions, PolicyPrincipal, PolicyResource, query
from computeragent.policy import CedarPolicyEngine, PolicyToolAuthorizer
from computeragent.types import ResultMessage

POLICIES_FILE = pathlib.Path(__file__).parent / "policies" / "computeragent.cedar"


async def main() -> None:
    cedar = CedarPolicyEngine(
        policies=POLICIES_FILE.read_text(),
        fail_mode="deny",
    )

    authorizer = PolicyToolAuthorizer(
        engine=cedar,
        principal_resolver=lambda ctx: PolicyPrincipal(
            id=os.environ.get("USER_ID", "alice"),
            groups=os.environ.get("USER_GROUPS", "engineer").split(","),
        ),
        resource_resolver=lambda ctx: PolicyResource(
            agent_name="acme-assist", model="claude-sonnet-4-5"
        ),
        context_resolver=lambda ctx: {"env": os.environ.get("ENV", "dev")},
    )

    with tempfile.TemporaryDirectory() as session_dir:
        options = ClaudeAgentOptions(
            model=os.environ.get("AGENT_MODEL", "claude-sonnet-4-5"),
            cwd=session_dir,
            system_prompt="You are AcmeAssist. Answer concisely.",
            allowed_tools=["Read", "Glob", "Grep"],
            mcp_servers={},
            permission_mode="default",
            add_dirs=[],
            env={k: v for k, v in os.environ.items() if k.startswith(("ANTHROPIC_", "AWS_"))},
            can_use_tool=authorizer,
        )

        async for message in query(prompt="What files are in the current directory?", options=options):
            if isinstance(message, ResultMessage):
                print("done. cost=$%.4f turns=%s" % (message.total_cost_usd or 0.0, message.num_turns))
                print(message.result)

    await cedar.aclose()


if __name__ == "__main__":
    asyncio.run(main())
