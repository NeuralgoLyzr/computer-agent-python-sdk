"""Tool-use authorization via a remote OPA server.

Run a local OPA:

    docker run --rm -p 8181:8181 \\
        -v "$(pwd)/examples/policies:/policies" \\
        openpolicyagent/opa:latest \\
        run --server --addr=0.0.0.0:8181 /policies/computeragent.rego

Then:

    pip install -e .
    export ANTHROPIC_API_KEY=sk-ant-...
    python examples/with_opa_policy.py

Every tool the agent wants to call goes through:
    POST http://localhost:8181/v1/data/computeragent/tools/allow

The Rego policy answers ``true`` for ``Read|Glob|Grep`` and a few gated cases,
``false`` for everything else.
"""

from __future__ import annotations

import asyncio
import os
import tempfile

from computeragent import ClaudeAgentOptions, PolicyPrincipal, PolicyResource, query
from computeragent.policy import OpaPolicyEngine, PolicyToolAuthorizer
from computeragent.types import ResultMessage


async def main() -> None:
    opa = OpaPolicyEngine(
        url=os.environ.get("OPA_URL", "http://localhost:8181"),
        policy_path="computeragent/tools/allow",
        fail_mode="deny",
        timeout=2.0,
    )

    authorizer = PolicyToolAuthorizer(
        engine=opa,
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
            permission_mode="default",  # NOTE: NOT bypassPermissions
            add_dirs=[],
            env={k: v for k, v in os.environ.items() if k.startswith(("ANTHROPIC_", "AWS_"))},
            # The only addition vs. v0.1 — wire OPA as the can_use_tool callback.
            can_use_tool=authorizer,
        )

        async for message in query(prompt="What files are in the current directory?", options=options):
            if isinstance(message, ResultMessage):
                print("done. cost=$%.4f turns=%s" % (message.total_cost_usd or 0.0, message.num_turns))
                print(message.result)

    await opa.aclose()


if __name__ == "__main__":
    asyncio.run(main())
