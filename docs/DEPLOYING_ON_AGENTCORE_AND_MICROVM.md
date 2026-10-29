# Integrating the CAS SDK, and deploying it on AgentCore and Lambda MicroVM

This guide is for engineers who are adding the CAS SDK (`computer-agent-py`) to a system they
already run. It covers:

1. [Pick an integration mode](#1-pick-an-integration-mode): the SDK as a library inside your service,
   or the SDK's app server as a sandbox image that CAS drives.
2. [Settings](#2-settings): what goes into the image and what arrives per session.
3. [Use the SDK as a library](#3-use-the-sdk-as-a-library-in-your-own-service).
4. [Build a sandbox image](#4-build-a-sandbox-image) with the SDK's app server.
5. [Push it to ECR](#5-push-the-image-to-ecr).
6. [Register it as a Lambda MicroVM image](#6-register-a-lambda-microvm-image).
7. [Register it as an AgentCore runtime](#7-register-an-agentcore-runtime).
8. [Register a computer agent and point it at the image](#8-register-a-computer-agent-and-point-it-at-the-image):
   create it, patch it, read it back.
9. [Agent configuration reference](#9-agent-configuration-reference): every field, and the model,
   guardrail and MCP providers the agent gateway accepts.
10. [Troubleshooting](#10-troubleshooting).

Working examples of everything below are in `examples/sdk_default_lambda_microvm/` and
`examples/sdk_default_agentcore/`: default sandbox images that run any agent CAS hands them. The
settings reference is [`USING_THE_SDK.md`](USING_THE_SDK.md) §3.

## How the pieces fit

```mermaid
sequenceDiagram
  participant U as AgentOS / your caller
  participant C as CAS (computer-agent-service)
  participant S as Sandbox: your image (SDK app server)
  participant P as CAS egress proxy
  participant G as Agent gateway
  U->>C: run agent (image.ref = MicroVM image or AgentCore runtime)
  C->>S: boot sandbox (RunMicrovm / InvokeAgentRuntime)
  C->>S: POST /v1/sessions (envs: COMPUTER_AGENT_ID, COMPUTER_AGENT_EGRESS_PROXY_SECRET)
  C->>S: POST /v1/sessions/{id}/messages (credential = fresh token)
  S->>P: agent document, guardrails, model, MCP, telemetry (token as x-api-key)
  P->>G: forwards with the real Lyzr key
  S-->>C: GET /v1/sessions/{id}/events (SSE)
```

The sandbox never holds a provider key, a Lyzr key or a git token. Its only secret is the
short-lived sandbox token (`sbt_…`), and every call it makes goes to the egress proxy, which swaps
the token for the real credentials. The SDK takes care of the token, including the reissued
successor the proxy sends before expiry (`USING_THE_SDK.md` §8a).

## 1. Pick an integration mode

| | A. Library in your service | B. Sandbox image driven by CAS |
|---|---|---|
| Who calls the agent | your code: `ComputerAgent(...).chat(...)` | CAS, over the harness REST API |
| What you deploy | your existing service, with the SDK as a dependency | a container image running the SDK's app server, registered as a Lambda MicroVM image or an AgentCore runtime |
| Where the token comes from | you supply it (`COMPUTER_AGENT_EGRESS_PROXY_SECRET` or `api_key=`) | CAS, per session and per message |
| Read | §2, §3 | §2, §4–§8 |

Mode B is what CAS needs for an agent's `image.ref`. Your own code does not go into the app server.
It goes into the agent itself (GAP directory, tools, MCP servers bound in AgentOS), or behind the
egress proxy.

## 2. Settings

The SDK reads every setting from a constructor argument or, when that is unset, from an environment
variable. Every missing required setting is reported in one `SettingsError`.

| Variable | Argument | Required | In a sandbox image it comes from |
|---|---|---|---|
| `COMPUTER_AGENT_ID` | `agent_id` | yes | CAS, per session (`POST /v1/sessions` `envs`) |
| `COMPUTER_AGENT_EGRESS_PROXY_SECRET` | `api_key` | yes | CAS, per session and per message. **Never bake it into an image.** |
| `COMPUTER_AGENT_ENGINE` | `engine` | yes | the image (`claude-agent-sdk` or `gitagent`), or CAS per session when the image leaves it unset |
| `COMPUTER_AGENT_EGRESS_API_URL` | `base_url` | yes | CAS per session, or the image: `<proxy>/computer-agent-api/agentos/api` |
| `COMPUTER_AGENT_EGRESS_LLM_URL` | `llm_url` | yes | CAS per session, or the image: `<proxy>/llm` |
| `COMPUTER_AGENT_EGRESS_MCP_URL` | `mcp_url` | yes | CAS per session, or the image: `<proxy>/mcp/v1` |
| `COMPUTER_AGENT_EGRESS_GUARDRAILS_URL` | `guardrails_url` | yes | CAS per session, or the image: `<proxy>/guardrails` |
| `COMPUTER_AGENT_EGRESS_TELEMETRY_URL` | `telemetry_url` | no | CAS per session, or the image: `<proxy>/otlp` (needs the `otel` extra) |
| `COMPUTER_AGENT_EGRESS_SESSION_STORE_URL` | `session_store_url` | no | CAS per session, or the image: `<proxy>/computer-agent-api` |
| `COMPUTER_AGENT_GAP_DIR` | `gap_dir` | no | the image, when the agent is a GAP directory baked into it |

`<proxy>` is the egress proxy's base URL, e.g. `https://egress.example.com`. CAS passes the six egress
routes per session; an image that sets one in its own environment keeps it (and an agent factory's
arguments win over both), so one image works in every environment CAS runs in. The examples keep the
image's settings in a `.env` file that the Dockerfile copies next to `app.py`. Start from
`examples/*/.env.example`.

## 3. Use the SDK as a library in your own service

Install:

```bash
pip install 'computer-agent-py[agentos]'            # add [gitagent] for the gitagent engine, [otel] for telemetry
```

Run one turn:

```python
import asyncio
from computeragent import ComputerAgent, AgentResolveError

async def answer(prompt: str) -> str:
    # Construction fetches the agent document with a blocking call: keep it off the event loop.
    agent = await asyncio.to_thread(ComputerAgent)          # settings from COMPUTER_AGENT_* (§2)
    async with agent:
        result = await asyncio.wait_for(agent.chat(prompt), timeout=300)
    for message in reversed(result.messages):               # the assistant's final text
        text = getattr(message, "result", None)
        if isinstance(text, str) and text:
            return text
    return ""
```

To pass settings from your own config instead of the environment, use the arguments, which win over
the variables:

```python
agent = ComputerAgent(
    agent_id=cfg.agent_id,
    engine="claude-agent-sdk",
    base_url=f"{cfg.egress}/computer-agent-api/agentos/api",
    llm_url=f"{cfg.egress}/llm",
    mcp_url=f"{cfg.egress}/mcp/v1",
    guardrails_url=f"{cfg.egress}/guardrails",
    api_key=sandbox_token,          # or leave unset and keep it in COMPUTER_AGENT_EGRESS_PROXY_SECRET
)
```

Things to get right in a long-running service:

- **One `ComputerAgent` is one session.** Do not run two `chat()` calls on the same instance at
  once. Build one instance per conversation.
- **A turn has no timeout and nothing retries.** Wrap `chat()` in `asyncio.wait_for`.
- **The token is re-read every turn** when it comes from `COMPUTER_AGENT_EGRESS_PROXY_SECRET`. If
  your system mints a fresh sandbox token per turn, write it there before the turn. An explicit
  `api_key=` stays the key of that instance, replaced only by the proxy's own reissued successor.
- **Errors at construction** (`SettingsError`, `AgentResolveError`, `UnsupportedAgentError`) name
  the setting or field, never its value, so they are safe to log and return.

`examples/e2e/live_agent_run.py` runs one turn and prints a pass/fail line per stage. Use it to check
a new environment.

## 4. Build a sandbox image

The image runs the SDK's app server: `computeragent.server.create_app()` (or
`python -m computeragent.server`). The same process serves both runtimes:

| Runtime | Routes the platform and CAS call |
|---|---|
| Lambda MicroVM | `POST /aws/lambda-microvms/runtime/v1/{ready,validate,run,suspend,resume,terminate}`, then the harness API: `GET /v1/health`, `POST /v1/sessions`, `POST /v1/sessions/{id}/messages`, `GET /v1/sessions/{id}/events`, … |
| AgentCore | `GET /ping`, and `POST /invocations` carrying the harness API in relay envelope v1 |

The full route list is in `USING_THE_SDK.md` §14.

### 4.1 Start from an example

```bash
cp examples/sdk_default_lambda_microvm/.env.example examples/sdk_default_lambda_microvm/.env   # the engine
docker buildx build --platform linux/arm64 \
  -f examples/sdk_default_lambda_microvm/Dockerfile -t my-agent-sandbox .  # from the repository root
```

`examples/sdk_default_agentcore/` is the same app with AgentCore wording; one image works on both
runtimes. Neither bakes an egress route: CAS passes them per session (§2).

### 4.2 Or add it to your own Dockerfile

```dockerfile
FROM python:3.12-slim
RUN pip install 'computer-agent-py[agentos,server,gitagent,otel]' 'python-dotenv>=1,<2'
COPY app.py .env /app/

# The Claude CLI refuses to skip permission prompts as root; the engine runs in the working directory.
RUN useradd --create-home --uid 10001 agent && mkdir /workspace && chown agent:agent /workspace
USER agent
WORKDIR /workspace
EXPOSE 8080
CMD ["python", "/app/app.py"]
```

with `app.py`:

```python
from pathlib import Path
import uvicorn
from dotenv import load_dotenv
from computeragent.server import create_app

load_dotenv(Path(__file__).with_name(".env"), override=False)   # the image's settings (§2)
app = create_app()

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8080, log_level="warning", access_log=False)
```

Rules every image must follow:

- **`linux/arm64`.** Both runtimes run on Graviton.
- **Port 8080**, the only port the platforms talk to.
- **A non-root user.**
- **No secrets and no per-session values.** The image's settings are the egress routes and the
  engine. `COMPUTER_AGENT_ID` and `COMPUTER_AGENT_EGRESS_PROXY_SECRET` arrive per session. The app
  server must boot without them: a MicroVM image build runs the container to take its snapshot,
  with no session.
- **A single-platform image with Docker v2 media types.** The MicroVM build service cannot resolve
  an OCI image index (buildx's default, because of the provenance attestation):

  ```bash
  docker buildx build --platform linux/arm64 --provenance=false --sbom=false \
    --output type=image,oci-mediatypes=false,push=true \
    -f Dockerfile -t <account>.dkr.ecr.<region>.amazonaws.com/<repo>:<tag> .
  ```

### 4.3 Check it locally before you push

```bash
docker run --rm -p 8080:8080 my-agent-sandbox
curl -s -X POST localhost:8080/aws/lambda-microvms/runtime/v1/ready -o /dev/null -w '%{http_code}\n'   # 200
curl -s localhost:8080/v1/health                                                                     # {"ok":true,...}
curl -s localhost:8080/ping                                                                          # {"status":"Healthy",...}
curl -s -X POST localhost:8080/v1/sessions -H 'content-type: application/json' -d '{
  "engine": "claude-agent-sdk",
  "identity": {"loader": "gitagentprotocol", "source": {"type": "local", "path": "/agent"}},
  "envs": {"COMPUTER_AGENT_ID": "<agent id>", "COMPUTER_AGENT_EGRESS_PROXY_SECRET": "<sandbox token>"}
}'
```

With a real token, the session answers `201`. With a fake one, the answer is a `502
AGENT_BUILD_FAILED` whose detail names the proxy URL it tried, e.g.
`…/computer-agent-api/agentos/api/v1/agents/<agent id>` and `401 TOKEN_INVALID`. That error proves
the routes and the per-session values are wired.

## 5. Push the image to ECR

Use an ECR repository in the **same AWS account** as the runtime. The MicroVM build service pulls
only from public ECR or same-account ECR.

```bash
aws ecr create-repository --repository-name <team>/<agent>-sandbox \
  --image-tag-mutability MUTABLE --encryption-configuration encryptionType=KMS
aws ecr get-login-password | docker login --username AWS --password-stdin <account>.dkr.ecr.<region>.amazonaws.com
# build and push as in §4.2, then read the digest back:
aws ecr describe-images --repository-name <team>/<agent>-sandbox --image-ids imageTag=<tag> \
  --query 'imageDetails[0].imageDigest' --output text
```

Always register by digest (`<repo>@sha256:<digest>`), not by tag. A tag can move.

The role that pulls the image (the MicroVM build role, or the AgentCore execution role) needs pull
permission on your repository. Grant it in a repository policy scoped to that one role, rather than
widening the role:

```bash
aws ecr set-repository-policy --repository-name <team>/<agent>-sandbox --policy-text '{
  "Version": "2012-10-17",
  "Statement": [{
    "Sid": "PullForRuntime",
    "Effect": "Allow",
    "Principal": {"AWS": "arn:aws:iam::<account>:role/<build-or-execution-role>"},
    "Action": ["ecr:BatchGetImage", "ecr:GetDownloadUrlForLayer", "ecr:BatchCheckLayerAvailability"]
  }]
}'
```

AgentCore needs only `ecr:BatchGetImage` and `ecr:GetDownloadUrlForLayer`. Lambda MicroVM's build
role also needs `ecr:BatchCheckLayerAvailability`.

## 6. Register a Lambda MicroVM image

### 6.1 Prerequisites

| What | Requirement |
|---|---|
| Build role | trusted by `lambda.amazonaws.com`. It can read your code artifact (`s3:GetObject`) and pull your image (§5): `ecr:GetAuthorizationToken` on `*`, plus the three pull actions on your repository, in the role's policy or a repository policy. |
| Execution role | the role the running MicroVM assumes, e.g. for `bedrock:InvokeModel`. CAS passes it on every `RunMicrovm` (`LAMBDA_MICROVM_EXECUTION_ROLE_ARN`, §8.5); it is not part of the image. |
| Your principal | `lambda:CreateMicrovmImage` / `lambda:UpdateMicrovmImage`, and `iam:PassRole` on the build role. Without `iam:PassRole` the call fails with `AccessDeniedException … iam:PassRole`. Pushing to ECR and uploading the artifact need no `iam:PassRole`, so a narrower role can do those. |
| S3 bucket | one the build role can read, for the code artifact; one prefix per image (`<agent>-sandbox/artifact.zip`) |
| Base image | the AWS image the build runs on: `arn:aws:lambda:<region>:aws:microvm-image:al2023-1` |
| Egress | the MicroVM must reach the egress proxy. `INTERNET_EGRESS` (the AWS-managed connector) for a public proxy; a VPC egress connector for a private one. |

### 6.2 The code artifact

The MicroVM build takes a zip with a `Dockerfile` at its root and builds it on an AWS base image.
Make it one line that names your pushed image by digest:

```bash
printf 'FROM <account>.dkr.ecr.<region>.amazonaws.com/<team>/<agent>-sandbox@sha256:<digest>\n' > Dockerfile
zip artifact.zip Dockerfile
aws s3 cp artifact.zip s3://<bucket>/<agent>-sandbox/artifact.zip
```

### 6.3 Create the image

```bash
aws lambda-microvms create-microvm-image \
  --name <agent>-sandbox \
  --base-image-arn arn:aws:lambda:<region>:aws:microvm-image:al2023-1 \
  --build-role-arn arn:aws:iam::<account>:role/<build-role> \
  --code-artifact uri=s3://<bucket>/<agent>-sandbox/artifact.zip \
  --egress-network-connectors arn:aws:lambda:<region>:aws:network-connector:aws-network-connector:INTERNET_EGRESS \
  --cpu-configurations architecture=ARM_64 \
  --resources minimumMemoryInMiB=2048 \
  --hooks 'port=8080,microvmHooks={run=ENABLED,runTimeoutInSeconds=60,resume=ENABLED,resumeTimeoutInSeconds=60,suspend=ENABLED,suspendTimeoutInSeconds=60,terminate=ENABLED,terminateTimeoutInSeconds=60},microvmImageHooks={ready=ENABLED,readyTimeoutInSeconds=600,validate=ENABLED,validateTimeoutInSeconds=600}' \
  --tags Owner=<you>,Service=<agent>-sandbox
```

| Flag | Why |
|---|---|
| `--hooks` | Enables all six lifecycle hooks on port 8080. The paths are fixed by the platform; the app server answers each with `200`. CAS sends no traffic until `run` answers. |
| `--cpu-configurations architecture=ARM_64` | The only architecture MicroVMs support |
| `--resources minimumMemoryInMiB=2048` | Baseline memory; vCPU scales with it. Raise it for heavy agents. |
| no `--environment-variables` | They are captured in the snapshot and shared by every session. Keep per-session values and secrets out. |

The call returns the image ARN and version `1.0` in state `CREATING`. Wait for the build. Pass the
**ARN**, not the name; the name gives `ValidationException: Invalid ARN format`:

```bash
aws lambda-microvms get-microvm-image-version \
  --image-identifier arn:aws:lambda:<region>:<account>:microvm-image:<agent>-sandbox \
  --image-version 1.0 --query '[state,status,stateReason]' --output text
# SUCCESSFUL  ACTIVE  None    (FAILED carries the reason)
```

### 6.4 Ship a new version

Re-pushing an ECR tag does **not** change a MicroVM image: the build took a snapshot. For every new
image, push it, point the artifact at the new digest, and build a new version:

```bash
aws lambda-microvms update-microvm-image \
  --image-identifier arn:aws:lambda:<region>:<account>:microvm-image:<agent>-sandbox \
  --code-artifact uri=s3://<bucket>/<agent>-sandbox/artifact.zip \
  --base-image-arn arn:aws:lambda:<region>:aws:microvm-image:al2023-1 \
  --build-role-arn arn:aws:iam::<account>:role/<build-role>
```

`--base-image-arn` and `--build-role-arn` are required on every update. Poll the returned version as
in §6.3. Agents pinned to an older version keep it until you move the pin (§8).

### 6.5 Limits to plan for

- A MicroVM lives at most 8 hours.
- AWS base images move from `AVAILABLE` to `DEPRECATED`, then `EXPIRING` and `EXPIRED`; an expired
  base can neither build nor run. Rebuild on a current base before then.
- Quotas: 100 images per account and region, 50 versions per image.

### 6.6 Remove it

```bash
aws lambda-microvms delete-microvm-image \
  --image-identifier arn:aws:lambda:<region>:<account>:microvm-image:<agent>-sandbox
aws s3 rm s3://<bucket>/<agent>-sandbox/artifact.zip
```

## 7. Register an AgentCore runtime

### 7.1 Prerequisites

| What | Requirement |
|---|---|
| Execution role | trusted by `bedrock-agentcore.amazonaws.com`, best limited with `aws:SourceAccount` = your account and `aws:SourceArn` = `arn:aws:bedrock-agentcore:<region>:<account>:runtime/*`. It needs `ecr:GetAuthorizationToken`, pull (`BatchGetImage`, `GetDownloadUrlForLayer`) on your repository (§5), and logs under `/aws/bedrock-agentcore/runtimes/<runtime name>*`. |
| Your principal | `bedrock-agentcore:CreateAgentRuntime` / `UpdateAgentRuntime`, and `iam:PassRole` on the execution role |
| Repository policy first | `create-agent-runtime` checks the pull with the execution role; without it the call fails with `ValidationException … Access denied while validating ECR URI` |
| Network | `PUBLIC` reaches a public egress proxy. For a private proxy use `VPC` with your subnets and security groups. |
| Name | letters, digits and `_`, starting with a letter; the ARN adds a 10-character suffix. A shared prefix lets one log policy cover all your runtimes. |

### 7.2 Create the runtime

```bash
aws bedrock-agentcore-control create-agent-runtime \
  --agent-runtime-name <agent>_sandbox \
  --agent-runtime-artifact 'containerConfiguration={containerUri=<account>.dkr.ecr.<region>.amazonaws.com/<team>/<agent>-sandbox@sha256:<digest>}' \
  --role-arn arn:aws:iam::<account>:role/<execution-role> \
  --network-configuration networkMode=PUBLIC \
  --protocol-configuration serverProtocol=HTTP \
  --tags Owner=<you>,Service=<agent>-sandbox
```

The name allows letters, digits and `_` only, starting with a letter. The answer carries the runtime
ARN, `arn:aws:bedrock-agentcore:<region>:<account>:runtime/<agent>_sandbox-<10 chars>`. Wait for
`READY`:

```bash
aws bedrock-agentcore-control get-agent-runtime --agent-runtime-id <agent>_sandbox-<10 chars> \
  --query '[status,failureReason,agentRuntimeVersion]' --output text
```

Leave `--environment-variables` unset, for the same reason as on MicroVM: the values are shared by
every session.

### 7.3 Ship a new version

`update-agent-runtime` with the new digest (and the same role, network and protocol) creates a new
immutable runtime version. The `DEFAULT` endpoint follows the latest version. Create a named
endpoint (`create-agent-runtime-endpoint`) if you want to move agents explicitly.

### 7.4 Remove it

```bash
aws bedrock-agentcore-control delete-agent-runtime --agent-runtime-id <agent>_sandbox-<10 chars>
```

## 8. Register a computer agent and point it at the image

A computer agent is an AgentOS registry entry plus a **gateway agent** that AgentOS creates for it
in the agent gateway. AgentOS keeps the identity, source, harness, runtime and image; the gateway
agent keeps what the sandbox may use (model, guardrail, MCP servers) and the gateway enforces it
on every call through the egress proxy. You create and change both through AgentOS.

### 8.1 Before you start

| What | Requirement |
|---|---|
| AgentOS API | `https://<agentos host>/agentos/api/v1` |
| Credential | your own **Lyzr workspace key** as `x-api-key`. AgentOS writes the gateway agent with it, so a session cookie alone is refused with `401 AUTH_LYZR_KEY_REQUIRED` as soon as `llm`, `guardrails` or `mcp` is sent. |
| Permissions | `agents:write` to register or patch; `mcp:bind` as well to set or clear `mcp` |
| Agent gateway | configured on AgentOS (`AGENT_GATEWAY_URL`), else `503 AGENT_GATEWAY_NOT_CONFIGURED` |
| Your ids | an SRS RAI policy id for `guardrails`, MCP server ids and their action names for `mcp`, a credential id for a private source |

### 8.2 Register a new agent

`POST /agentos/api/v1/agents/register`. Only `name` is required; `llm`, `guardrails` and `mcp`
go to the gateway agent, everything else to the registry. A full example, with the image of §6:

```bash
AGENTOS=https://<agentos host>/agentos/api/v1
curl -s -X POST "$AGENTOS/agents/register" \
  -H "x-api-key: $LYZR_KEY" -H 'content-type: application/json' -d '{
  "name": "support-agent",
  "label": "Support agent",
  "source": "github.com/acme/support-agent",
  "harness": "claude-agent-sdk",
  "llm": {"provider": "anthropic", "config": {"model": "claude-sonnet-4-5"}},
  "guardrails": {"provider": "lyzr-srs", "config": {"id": "<SRS RAI policy id>"}},
  "mcp": {"provider": "lyzr-mcp", "servers": [
    {"server_id": "<MCP server id>", "action_names": ["search", "get_issue"], "tool_name": "Jira Cloud"}
  ]},
  "runtime": "lambda-microvm",
  "image": {"runtime": "lambda-microvm", "ref": "arn:aws:lambda:<region>:<account>:microvm-image:<agent>-sandbox", "version": "1.0"},
  "credentials": {"id": "<credential id>", "provider_id": "github_app"},
  "type": "managed"
}'
```

```json
{"ok": true, "id": "6a99eca0b53aa307f117c79b", "name": "support-agent", "gatewayAgentId": "agwa_01J9Z8Y7X6W5V4T3S2R1Q0P9N8"}
```

- `id` is the registry id, used in every `/agents/{id}` path and passed to the sandbox as
  `COMPUTER_AGENT_ID`. `gatewayAgentId` names the gateway agent AgentOS created.
- **Leave out `image` (and `runtime`)** to run on the server defaults (`LAMBDA_MICROVM_IMAGE_ARN`,
  `AGENTCORE_RUNTIME_ARN`, §8.5). With `runtime` unset, `image.runtime` picks the runtime.
- **Set `llm.provider` to one the gateway's catalog enables** (§9.2). AgentOS defaults it to
  `kong`; a gateway without `kong` refuses the agent. With no `llm` at all the gateway agent gets
  AgentOS's default model (`AGENTOS_DEFAULT_MODEL`, `claude-haiku-4-5`) on that provider.
- `name` is unique within the owning group: a second register with the same name is
  `409 AGENT_NAME_TAKEN` (with the existing id) unless the body has `"update": true`, which edits
  it in place. `PATCH` (§8.3) is the usual way to edit.
- The new agent is owned by you and by `ownerGroup` (you must be a member) or your first group.
- The removed fields `model`, `rai_policy_id`, `rai_policy_name`, `policy_name`, `mcp_servers`,
  `model_gateway`, `mcp_mode`, `telemetry` and `git` are refused with `400 REQUEST_INVALID` naming
  the field.

### 8.3 Change an agent

`PATCH /agentos/api/v1/agents/{id}`, with the registry id from §8.2. Every field is optional.
Each binding field (`image`, `runtime`, `type`, `credentials`, `guardrails`, `mcp`,
`orchestration`) has three states:

| You send | Effect |
|---|---|
| the key left out | unchanged |
| a value | set |
| an empty value: `null` or `""`; for `credentials` `null` or an empty `id`; for `mcp` `null` or `"servers": []` | cleared |

`llm` cannot be cleared, and an `llm` without `config.model` keeps the current model. A section's
`provider` left out keeps the current one. AgentOS reads the gateway agent's whole config, applies
your sections, and writes it back in one call, so a PATCH never drops a section you did not send.

```bash
patch() { curl -s -X PATCH "$AGENTOS/agents/$AGENT_ID" -H "x-api-key: $LYZR_KEY" \
  -H 'content-type: application/json' -d "$1"; }

patch '{"llm": {"config": {"model": "claude-sonnet-4-5"}}}'                          # change the model
patch '{"llm": {"provider": "anthropic"}}'                                           # change the provider, keep the model
patch '{"guardrails": {"config": {"id": "<SRS RAI policy id>"}}}'                    # set the guardrail
patch '{"guardrails": null}'                                                          # remove it
patch '{"mcp": {"servers": [{"server_id": "<id>", "action_names": ["search"], "tool_name": "Jira Cloud"}]}}'  # replace the MCP servers
patch '{"mcp": null}'                                                                 # remove every MCP server
patch '{"image": {"runtime": "agentcore", "ref": "arn:aws:bedrock-agentcore:<region>:<account>:runtime/<agent>_sandbox-<10 chars>"}}'  # move to AgentCore
patch '{"image": {"runtime": "lambda-microvm", "ref": "arn:aws:lambda:<region>:<account>:microvm-image:<agent>-sandbox", "version": "2.0"}}'  # pin a MicroVM version
patch '{"image": null, "runtime": ""}'                                                # back to the server defaults
patch '{"name": "support-agent-v2"}'                                                  # rename (cascades; see below)
```

- `mcp.servers` is replaced as a whole: send the full list you want.
- An image change applies to the **next new sandbox**; running sandboxes keep theirs.
- A rename moves the agent's sessions, logs and schedules to the new name; sandbox snapshots in S3
  stay under the old name, which the response reports in `warnings`. `409 AGENT_NAME_TAKEN` if the
  name is taken in your groups.
- The answer is `{"ok": true, "gatewayAgentId": …}`, plus `renamed` and `warnings` after a rename.
- `409 AGENT_GATEWAY_VERSION_CONFLICT` means someone else changed the gateway agent meanwhile:
  resend the same PATCH.

### 8.4 Read it back

```bash
curl -s "$AGENTOS/agents/$AGENT_ID" -H "x-api-key: $LYZR_KEY"
```

The answer has the registry fields (`runtime`, `image`, `source`, `harness`, …) and the gateway
agent's sections as AgentOS writes them: `llm` `{provider, config: {model}}`, `guardrails`
`{provider, config: {id}}` and `mcp` `{provider, servers: […]}`. A section is `null` when the agent
has none or the gateway agent could not be read, in which case `warnings` says why. The sandbox
reads exactly this document (USING_THE_SDK §6): `llm.config.model` is the model it sends, and
`mcp.servers` become its MCP servers.

### 8.5 What CAS needs to boot it

CAS must be allowed to use the image: `lambda:RunMicrovm` on the MicroVM image, or
`bedrock-agentcore:InvokeAgentRuntime` on the runtime. A refusal at boot surfaces as
`MICROVM_AGENT_IMAGE_*` or `AGENTCORE_AGENT_RUNTIME_ACCESS_DENIED`.

CAS's own role needs, for any image or runtime it should boot:

| Runtime | Permissions |
|---|---|
| Lambda MicroVM | `lambda:RunMicrovm`, `CreateMicrovmAuthToken`, `TerminateMicrovm`, `GetMicrovmImage`, `GetMicrovmImageVersion`; `iam:PassRole` on the MicroVM execution role; `lambda:PassNetworkConnector` on `arn:aws:lambda:<region>:aws:network-connector:aws-network-connector:*` |
| AgentCore | `bedrock-agentcore:InvokeAgentRuntime`, `StopRuntimeSession` on `runtime/*` |

Its settings:

| Setting | Meaning |
|---|---|
| `LAMBDA_MICROVM_IMAGE_ARN` | the image for an agent with none |
| `LAMBDA_MICROVM_EXECUTION_ROLE_ARN` | the role every MicroVM runs as |
| `AGENTCORE_RUNTIME_ARN` | the runtime for an agent with none |
| `AGENTCORE_QUALIFIER` | the endpoint invoked when the agent's `image.version` is empty; default `DEFAULT` |
| `CAS_EGRESS_PROXY_PUBLIC_URL` | the egress proxy CAS derives the six `COMPUTER_AGENT_EGRESS_*_URL` from |

With neither an agent image nor a server default, CAS answers `422 MICROVM_IMAGE_REQUIRED` or
`AGENTCORE_RUNTIME_REQUIRED` before any AWS call.

### 8.6 What CAS sends your image

| When | Request | What the app server does |
|---|---|---|
| Boot (MicroVM) | lifecycle hooks | answers `200` |
| Session start | `POST /v1/sessions` with `envs.COMPUTER_AGENT_ID`, `envs.COMPUTER_AGENT_EGRESS_PROXY_SECRET`, the six `envs.COMPUTER_AGENT_EGRESS_*_URL` and optionally `envs.COMPUTER_AGENT_ENGINE` | builds the session's `ComputerAgent` for that agent, with that token, engine and routes (an engine or route the image sets itself wins); every other `envs` key is ignored |
| Each turn | `POST /v1/sessions/{id}/messages` with `credential` | uses the fresh token for the turn |
| Streaming | `GET /v1/sessions/{id}/events` | the turn's events; one `ca_session_ended` when the session ends |
| AgentCore | the same requests inside `POST /invocations` (relay envelope v1) | answers HTTP 200 always; the real status is in the envelope |

## 9. Agent configuration reference

### 9.1 Registry fields (AgentOS)

| Field | Values | Notes |
|---|---|---|
| `name` | string, required on register | unique within the owning group; sending a new one on PATCH renames |
| `label` | string | display name; defaults to `name` |
| `source` | git URL or path (`github.com/<owner>/<repo>`), or an inline identity source object | what the harness checks out as the agent. Unset: `AGENTOS_DEFAULT_SOURCE`. A GitHub source also gives the gateway agent a read-only `git` section (§9.2) |
| `harness` | default `claude-agent-sdk` | the engine that runs the agent. The SDK images run `claude-agent-sdk` and `gitagent` only |
| `runtime` | `lambda-microvm`, `agentcore`; `""` clears | where the sandbox runs; unset: the server default (`AGENTOS_RUNTIME`), or `image.runtime` |
| `image` | `{runtime, ref, version?}`; `null` clears | `runtime` `lambda-microvm` with `ref` a MicroVM image ARN and `version` a pinned image version (absent: latest active); or `runtime` `agentcore` with `ref` a runtime ARN and `version` an endpoint (absent: `AGENTCORE_QUALIFIER`, `DEFAULT`). ECR URIs are refused |
| `credentials` | `{id, provider_id}`; `null` clears | a vault credential that clones a private https git `source`: `provider_id` `github` (PAT) or `github_app`. Needs your Lyzr key; checked on save (`CREDENTIAL_*`) |
| `orchestration` | `{mode, isolation, collect, maxParallel, budgetUsd}`; `null` clears | for an agent that runs sub-agents: `mode` `supervised` or `roster`, `isolation` `worktree` or `none`, `collect` `into-context` or `gate`. The SDK refuses an agent with it set (USING_THE_SDK §12), so leave it out for SDK images |
| `type` | `managed`, `client`; `""` clears | classification only; nothing reads it |
| `ownerGroup` | a group you belong to | register only; the group that owns the agent |
| `update` | boolean | register only; `true` edits an existing agent of the same name instead of `409` |

### 9.2 Gateway sections: providers

`llm`, `guardrails` and `mcp` become the gateway agent's sections. Each names a **provider**, and
three lists must all allow it: AgentOS's (what register and PATCH accept), the gateway's (the
providers it implements) and the deployment's catalog (the ones this gateway has enabled, with their
URLs and keys). A provider missing from the catalog is refused when AgentOS saves the agent:
`422 AGENT_GATEWAY_CONFIG_REJECTED` with `meta.gatewayCode: provider_unknown`.

| Section | AgentOS accepts | Gateway implements |
|---|---|---|
| `llm` | `kong` (default), `anthropic` | `kong`, `litellm`, `anthropic`, `openai` |
| `guardrails` | `lyzr-srs` | `lyzr-srs` |
| `mcp` | `lyzr-mcp` | `lyzr-mcp` |
| `git` | not settable: AgentOS writes `github-app` for a GitHub `source` | `github-app` |
| `telemetry` | not settable: AgentOS always writes the `signoz` sink | `signoz`, `new-relic` |

Which of these a deployment enables is its catalog, the gateway's `providers` config read from
`AGENT_GATEWAY_CATALOG_PATH`; ask the gateway's operator. Provider URLs and keys are deployment
settings (`ANTHROPIC_BASE_URL`/`ANTHROPIC_API_KEY`, `LYZR_SRS_BASE_URL`, `LYZR_MCP_BASE_URL`, …),
never part of an agent. A catalog without a `git` provider refuses every agent with a GitHub
`source`, since AgentOS always writes that section.

### 9.3 Gateway sections: settings

**`llm`**: `{"provider": "anthropic", "config": {"model": "<model id>"}}`

- `model` is the provider's own model id, passed through as is (`claude-sonnet-4-5`,
  `claude-haiku-4-5`): 1–256 bytes, no spaces.
- AgentOS writes it as the gateway agent's one route. The SDK reads the model from the agent
  document (USING_THE_SDK §6) and sends it; the gateway refuses any other model with
  `403 model_not_allowed`.
- On create, an `llm` without `config.model` is `400`; no `llm` at all uses `AGENTOS_DEFAULT_MODEL`.

**`guardrails`**: `{"provider": "lyzr-srs", "config": {"id": "<SRS RAI policy id>"}}`

- One RAI policy per agent, written as the gateway's `{"policyIds": ["<id>"], "failOpen": false}`:
  if the guardrails service cannot answer, the checked call is refused, not let through.
- The organisation's own RAI policies run first; the agent's policy adds to them.
- The SDK calls it through the `guardrails` egress route: one `input` check per text block of the
  user's message and one `tool-input` check per tool call (USING_THE_SDK §9).

**`mcp`**: `{"provider": "lyzr-mcp", "servers": [ … ]}`, at most 32 servers.

| Server field | Required | Meaning |
|---|---|---|
| `server_id` | yes | the Lyzr MCP server id |
| `action_names` | yes, non-empty (else `400 MCP_ACTIONS_REQUIRED`) | the exact tool names the sandbox may call, at most 256; every other tool of the server is hidden and refused |
| `tool_name` | no, default `server_id` | the server's name in the gateway (`/mcp/{name}`); the model sees its tools as `mcp__<tool_name>__<tool>`. 1–128 printable ASCII, not `.` or `..`, unique within the agent |
| `persist_auth` | no, default `false` | `false`: each user's own connection to the server is used, and the tools are withheld from a user who has not connected one; `true`: the agent owner's connection is used for everyone |
| `standard_server_id` | no | the MCP catalog id, when the server came from the catalog |

The gateway's other settings are not exposed through AgentOS: several `llm` routes for
failover, more than one guardrail policy, `failOpen: true`, `readOnlyTools`, and a
`new-relic` telemetry sink.

## 10. Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| MicroVM version `FAILED`: "An unknown error occurred" | The pushed image is an OCI index or uses OCI media types | Push with `--provenance=false --sbom=false --output type=image,oci-mediatypes=false,push=true` (§4.2) |
| MicroVM version `FAILED` on the pull | The build role cannot pull from your repository | Repository policy for the build role (§5) |
| `AccessDeniedException … iam:PassRole` on create | Your principal cannot pass the build or execution role | Grant `iam:PassRole` on that role only |
| `ValidationException: Invalid ARN format` on `get-microvm-image-version` | The image name was passed | Pass the image ARN |
| MicroVM build hangs at `ready`, or the container exits at start | The app requires a per-session value at boot | Read `COMPUTER_AGENT_ID` and the token per session; the app server does |
| Agent registration: `IMAGE_REF_INVALID` / `AGENTCORE_RUNTIME_ARN_INVALID` | `ref` is an ECR URI or a malformed ARN | Register the MicroVM image ARN or the AgentCore runtime ARN (§8) |
| Register/PATCH: `422 AGENT_GATEWAY_CONFIG_REJECTED`, `meta.gatewayCode: provider_unknown` | A provider the deployment's gateway catalog does not enable: AgentOS's default `kong` where the catalog lacks it, or `github-app` for a GitHub source where the catalog has no `git` provider | Name an enabled provider (§9.2), e.g. `"llm": {"provider": "anthropic", …}` |
| Register/PATCH: `401 AUTH_LYZR_KEY_REQUIRED` | `llm`, `guardrails` or `mcp` sent without your Lyzr key | Call with `x-api-key: <your Lyzr key>` (§8.1) |
| Register/PATCH: `403 AUTH_PERMISSION_MISSING` on `mcp` | You lack `mcp:bind` | Ask for the permission, or leave `mcp` out |
| Sandbox LLM call: `403 model_not_allowed` | The engine asked for a model other than the agent's `llm.config.model`, e.g. a GAP sub-agent that declares its own `model` | PATCH the agent's model, or remove the sub-agent's `model` |
| `create-agent-runtime`: `ValidationException … Access denied while validating ECR URI` | The execution role cannot pull from the repository | Repository policy for the execution role (§7.1) before creating the runtime |
| Session start: `502 AGENT_BUILD_FAILED … 401 TOKEN_INVALID` | The token is not a valid sandbox token for this proxy, or it expired | CAS mints it; for a manual test, mint one at the proxy |
| Session start: `502 AGENT_BUILD_FAILED … SettingsError: missing required settings: …` | The image lacks an egress route or the engine | Set them in the image's `.env` (§2) |
| A pushed fix does not show up on MicroVM | MicroVM images are snapshots | `update-microvm-image` (§6.4) and move the agent's `version` pin |
