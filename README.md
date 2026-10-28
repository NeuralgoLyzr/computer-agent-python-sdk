# computer-agent-py

Run an agent that is **registered in AgentOS** in your own process — typically a sandbox built from an
image the agent is baked into.

You name the agent by id. The SDK fetches its definition from AgentOS and reproduces what the AgentOS
dispatch path would have sent to a ComputerAgent server — same model, same engine environment, same
permission mode — except the engine runs here.

**Every call goes through an egress proxy.** The agent config, the guardrails check, the model, MCP
and telemetry are each reached at a URL you configure, with no direct path to any of them.

```bash
export COMPUTERAGENT_AGENT_ID=6aa93f1b3817a13d3103d2c1
export COMPUTERAGENT_ENGINE=claude-agent-sdk
export COMPUTERAGENT_BASE_URL=http://egress/agentos/api     # agent config, MCP bindings
export COMPUTERAGENT_GUARDRAILS_URL=http://egress/guardrails # guardrail checks
export COMPUTERAGENT_LLM_URL=http://egress/llm              # every model call
export COMPUTERAGENT_MCP_URL=http://egress/mcp/v1           # MCP servers bound in AgentOS
export LYZR_API_KEY=...                                     # sent as x-api-key; swapped by the proxy
# optional: COMPUTERAGENT_TELEMETRY_URL=http://egress/otlp (OTLP),
#   COMPUTERAGENT_SESSION_STORE_URL=http://egress/computer-agent-api (http session store),
#   COMPUTERAGENT_GAP_DIR (GAP mode)
```

```python
import asyncio
from computeragent import ComputerAgent

async def main():
    async with ComputerAgent() as agent:
        result = await agent.chat("List the files in your working directory.")
        print(result.usage)

asyncio.run(main())
```

Every setting can also be passed as a constructor argument of the same name (`base_url=`,
`guardrails_url=`, …), which wins over its environment variable. Missing settings are reported together, in
one `SettingsError`, before any request is made.

## Guardrails

The user's message is checked before the turn (block or mask) and every tool call before it runs,
each with `POST {guardrails_url}/v1/check`. The agent gateway behind the egress proxy applies the org's
and the agent's own RAI policies and decides fail-open; the SDK reads no policies and sends no policy
ids. A check that reaches no verdict blocks.

## The agent

The agent is baked into the image; nothing is cloned. By default the engine runs in the working
directory with the agent defined by your `options`. Set `gap_dir` to load a GAP agent
(`agent.yaml`, `SOUL.md`, skills, tools) from a directory instead.

## Install

```bash
pip install 'computer-agent-py[agentos]'
```

`[agentos]` is not optional in practice — every call the SDK makes is HTTP. Add `[otel]` for OTLP
telemetry, `[gap]` for GAP mode, and `[server]` for the app server a stage image runs
(`python -m computeragent.server`; see `docs/USING_THE_SDK.md` §14).

## Documentation

**[`docs/USING_THE_SDK.md`](docs/USING_THE_SDK.md)** is the developer reference: every setting and
the requests made from it, the guardrail flow, MCP, session stores, errors and troubleshooting.

[`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) covers how the pieces fit together, for anyone
changing the SDK rather than using it.

## Also in this package

A **drop-in proxy** for `claude-agent-sdk`. Replace `from claude_agent_sdk import …` with
`from computeragent import …` and every `query()` / `ClaudeSDKClient` call flows through the
telemetry pipeline (PII redaction, guardrails, OpenTelemetry and AgentOS sinks) with no call-site
changes. This is a separate surface from `ComputerAgent`, not a run mode, and it keeps its own
environment-driven telemetry configuration.

## Development

```bash
uv sync --all-extras --dev
uv run pytest -q -m "not integration"
uv run ruff check src tests
uv run mypy src
```

`examples/e2e/live_agent_run.py` runs one turn against live services with every setting taken from
the environment, and prints a pass/fail line per stage, so a failure localises itself.

## Not supported

Sub-agent orchestration: an agent declaring it is **refused at resolve time** rather than run as
something quieter than it declares.

## License

MIT
