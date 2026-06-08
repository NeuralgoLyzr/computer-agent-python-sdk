import asyncio
import os

from dotenv import load_dotenv

from computeragent import ComputerAgent

load_dotenv()


def reply_text(messages):
    """Pull the assistant's final text out of the chat result."""
    for m in reversed(messages):
        if isinstance(m, dict):
            if m.get("type") == "result" and isinstance(m.get("result"), str):
                return m["result"]
            if m.get("type") == "assistant":
                content = (m.get("message") or {}).get("content")
                if isinstance(content, str):
                    return content
                if isinstance(content, list):
                    text = "".join(
                        b.get("text", "")
                        for b in content
                        if isinstance(b, dict) and b.get("type") == "text"
                    )
                    if text:
                        return text
    return ""


async def main():
    async with ComputerAgent(
        harness_url=os.environ["COMPUTERAGENT_HARNESS_URL"],
        harness_kind="server",
        harness_token=os.environ["COMPUTERAGENT_HARNESS_TOKEN"],
        agent_id="6a2542c99cd2a68df3671a1c",
        # Pass name + source explicitly: /agents/resolve is broken for this id
        # (matches the agentId field of a different doc → "claude-agent-sdk").
        # "g2" is the real registry name this id's policy is bound under.
        agent_name="g2",
        source="https://github.com/shreyas-lyzr/general-agent",
        model="claude-sonnet-4-5",
    ) as agent:
        result = await agent.chat(
            "List the files in your current working directory, then read README.md."
        )

    print(f"session : {result.session_id}")
    print(f"tokens  : in={result.usage.input_tokens} out={result.usage.output_tokens}")
    print(f"reply   : {reply_text(result.messages)}")


asyncio.run(main())
