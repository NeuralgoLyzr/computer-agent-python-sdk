# Sample Rego policy for computer-agent-py's tool-use gate.
#
# The policy receives `input` shaped like:
#
#   {
#     "principal": {"id": "alice", "groups": ["engineer"], "attributes": {}},
#     "action":    {"name": "tool_use", "tool_name": "Read", "tool_input": {}},
#     "resource":  {"type": "agent", "agent_name": "acme-assist",
#                   "model": "claude-sonnet-4-6", "session_id": "...", "attributes": {}},
#     "context":   {"env": "prod"}
#   }
#
# Either return a bare boolean (`result := true | false`) or an object with
# fields `allow` and optional `reason` / `obligations`. The OPA engine in this
# package accepts both shapes.

package computeragent.tools

import future.keywords.if
import future.keywords.in

# Default deny — fail-closed at the policy layer too.
default allow := false

# Documentation tools are always permitted.
allow if {
    input.action.tool_name in {"Read", "Glob", "Grep"}
}

# Custom MCP tools under acme-assist's namespace are gated by group.
allow if {
    startswith(input.action.tool_name, "mcp__acme-assist-tools__")
    "engineer" in input.principal.groups
}

# Production environment is more restrictive — block Bash entirely.
allow_bash if {
    input.context.env != "prod"
    input.action.tool_name == "Bash"
    "sre" in input.principal.groups
}

allow if {
    allow_bash
}
