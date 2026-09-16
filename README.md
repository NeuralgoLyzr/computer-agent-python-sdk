# computer-agent-py

Run an agent that is **registered in AgentOS** on your own machine, in your own process.

You name the agent by id. The SDK fetches its definition from AgentOS and reproduces what the
AgentOS dispatch path would have sent to a ComputerAgent server — same source, same model, same
engine environment, same permission mode — except the engine runs here instead of in a sandbox.

```python
import asyncio
from computeragent import ComputerAgent

async def main():
    async with ComputerAgent(
        base_url="http://localhost:8788/agentos/api",
        agent_id="6aa93f1b3817a13d3103d2c1",
        api_key="<lyzr workspace key>",
        llm_proxy_url="http://127.0.0.1:8786",   # only for a gateway-bound agent
    ) as agent:
        result = await agent.chat("List the files in your working directory.")
        print(result.usage)

asyncio.run(main())
```

That is the only run mode. Everything beyond the three required arguments is an override for
something the agent document already answers.

## Install

```bash
pip install 'computer-agent-py[agentos]'
```

`[agentos]` is not optional in practice — resolving an agent is an HTTP call. The
`claude-agent-sdk` engine also needs the Claude Code CLI on `PATH`, and a cloned source needs
`git`.

## Documentation

**[`docs/USING_THE_SDK.md`](docs/USING_THE_SDK.md)** is the developer reference: every constructor
parameter with its legal values and where to obtain it, the three model-routing cases, session
stores, error handling, and a troubleshooting table.

[`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) covers how the pieces fit together, for anyone
changing the SDK rather than using it.

## Also in this package

A **drop-in proxy** for `claude-agent-sdk`. Replace `from claude_agent_sdk import …` with
`from computeragent import …` and every `query()` / `ClaudeSDKClient` call flows through the
telemetry pipeline (PII redaction, guardrails, OpenTelemetry and AgentOS sinks) with no call-site
changes. Every upstream symbol is re-exported identity-preserving, so `isinstance` checks keep
working. This is a separate surface from `ComputerAgent`, not a run mode.

## Development

```bash
uv sync --all-extras --dev
uv run pytest -q -m "not integration"
uv run ruff check src tests
uv run mypy src
```

`examples/e2e/live_agent_run.py` runs one turn against a live AgentOS and prints a pass/fail line
per stage, so a failure localises itself. Integration-marked tests need real services and are
skipped unless their environment variables are set.

## Not supported

MCP servers and sub-agent orchestration. An agent declaring either is **refused at resolve time**
rather than run as something quieter than it declares. Design notes for both live in
[`docs/plans/`](docs/plans/).

## License

MIT
