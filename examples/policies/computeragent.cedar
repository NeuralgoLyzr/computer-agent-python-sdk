// Sample Cedar policies for computer-agent-py's tool-use gate.
//
// The CedarPolicyEngine translates each PolicyInput into:
//   principal:  User::"<principal.id>"  (parents: Group::"<each group>")
//   action:     Action::"toolUse"       (attribute: tool = <action.tool_name>)
//   resource:   Agent::"<resource.agent_name or session_id>"
//                 (attributes: model, session_id, ...resource.attributes)
//   context:    flat dict from PolicyInput.context
//
// Write the policy against those entities and attributes.

// Documentation tools are always permitted.
permit (
    principal,
    action == Action::"toolUse",
    resource is Agent
) when {
    action.tool == "Read" ||
    action.tool == "Glob" ||
    action.tool == "Grep"
};

// Custom MCP tools under nordassist's namespace are gated by group.
permit (
    principal in Group::"engineer",
    action == Action::"toolUse",
    resource is Agent
) when {
    action.tool like "mcp__nordassist-tools__*"
};

// SREs can use Bash, but only outside prod.
permit (
    principal in Group::"sre",
    action == Action::"toolUse",
    resource is Agent
) when {
    action.tool == "Bash" &&
    context has env &&
    context.env != "prod"
};
