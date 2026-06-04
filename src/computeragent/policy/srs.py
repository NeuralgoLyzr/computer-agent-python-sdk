"""SRS-backed :class:`PolicyEngine` — delegates tool-call authorization to Lyzr
SRS, so Python agents enforce the **same RAI policies authored in the Console**
(OPA + Cedar guardrails) as the TypeScript stack.

**Where the policy comes from mirrors the TS path — fetched from agentos over
HTTP**, the same channel library-mode agents already use to register telemetry
(``AGENTOS_INGEST_URL`` / ``AGENTOS_INGEST_TOKEN``). agentos resolves the
per-agent ``agent_policies`` binding (its ``srsPolicyForAgent``) and returns the
``{endpoint, apiKey, policyId, principalId}`` block — so the Python process needs
**no Mongo creds and no SRS key**; agentos hands it everything, exactly like
agentos hands the harness the ``policy`` block on a TS run.

Resolution order:
  1. explicit ``SRS_BASE_URL`` + ``SRS_POLICY_ID`` (standalone, no agentos), else
  2. fetch ``GET {ingest_base}/policy-config/{agent_name}`` from agentos.
Unbound agent → no policy → runs free (inert), like TS returning null.

Enforcement: fetch the RAI policy once (``GET /v1/rai/policies/{id}``), cache its
cedar + opa guardrails, then ``POST /v1/guardrails/evaluate-tool-call`` per tool.
Fail-closed on SRS errors; fail-open if the binding can't be resolved.

Only depends on ``httpx`` (a core dependency).
"""

from __future__ import annotations

import logging
import os
from typing import Any

import httpx

from .types import FailMode, PolicyDecision, PolicyInput, PolicyResult

logger = logging.getLogger(__name__)


def _ingest_base() -> str | None:
    """Derive the agentos ingest base (``.../agentos/api/ingest``) from
    ``AGENTOS_INGEST_URL`` (which points at the ``/events`` endpoint)."""
    url = os.environ.get("AGENTOS_INGEST_URL")
    if not url:
        return None
    base = url.rstrip("/")
    if base.endswith("/events"):
        base = base[: -len("/events")]
    return base


class SrsPolicyEngine:
    """``PolicyEngine`` that authorizes tool calls through Lyzr SRS.

    Either give ``endpoint`` + ``policy_id`` explicitly, or give
    ``agentos_base`` + ``agent_name`` and the SRS config (endpoint / api key /
    policy id) is fetched from agentos lazily on the first tool call — so
    construction stays synchronous.
    """

    name = "srs"

    def __init__(
        self,
        *,
        endpoint: str | None = None,
        policy_id: str | None = None,
        api_key: str = "",
        agentos_base: str | None = None,
        agentos_token: str | None = None,
        agent_name: str = "",
        fail_mode: FailMode = "deny",
        timeout: float = 10.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._endpoint = endpoint.rstrip("/") if endpoint else None
        self._policy_id = policy_id
        self._api_key = api_key
        self._agentos_base = agentos_base.rstrip("/") if agentos_base else None
        self._agentos_token = agentos_token
        self._agent_name = agent_name
        self._fail_mode: FailMode = fail_mode
        self._guardrails: dict[str, Any] | None = None
        self._resolved = False
        self._inert = False  # no policy bound -> allow everything
        self._client = client or httpx.AsyncClient(timeout=timeout)

    @classmethod
    def from_env(cls, *, agent_name: str = "", **overrides: Any) -> "SrsPolicyEngine":
        return cls(
            endpoint=overrides.pop("endpoint", None) or os.environ.get("SRS_BASE_URL"),
            api_key=overrides.pop("api_key", None) or os.environ.get("SRS_API_KEY", ""),
            policy_id=overrides.pop("policy_id", None) or os.environ.get("SRS_POLICY_ID"),
            agentos_base=overrides.pop("agentos_base", None) or _ingest_base(),
            agentos_token=overrides.pop("agentos_token", None) or os.environ.get("AGENTOS_INGEST_TOKEN"),
            agent_name=agent_name,
            **overrides,
        )

    async def _resolve_config(self) -> None:
        """Resolve endpoint + api_key + policy_id (once). Explicit config wins;
        otherwise fetch the agent's policy block from agentos (the TS-style
        binding lookup, over HTTP)."""
        if self._resolved:
            return
        self._resolved = True
        if self._endpoint and self._policy_id:
            return  # fully explicit
        if not self._agentos_base or not self._agent_name:
            self._inert = True
            return
        try:
            headers = {"authorization": f"Bearer {self._agentos_token}"} if self._agentos_token else {}
            resp = await self._client.get(
                f"{self._agentos_base}/policy-config/{self._agent_name}",
                headers=headers,
            )
            resp.raise_for_status()
            policy = (resp.json() or {}).get("policy")
            if not policy or not policy.get("policyId"):
                self._inert = True  # unbound -> run free
                return
            self._endpoint = str(policy.get("endpoint", "")).rstrip("/") or self._endpoint
            self._api_key = policy.get("apiKey", "") or self._api_key
            self._policy_id = policy.get("policyId")
        except Exception as exc:  # noqa: BLE001 - a control-plane blip shouldn't brick the agent
            logger.warning("agentos policy-config fetch failed for %r: %s", self._agent_name, exc)
            self._inert = True

    async def _load_policy(self) -> None:
        await self._resolve_config()
        if self._inert or self._guardrails is not None:
            return
        resp = await self._client.get(
            f"{self._endpoint}/v1/rai/policies/{self._policy_id}",
            headers={"x-api-key": self._api_key},
        )
        resp.raise_for_status()
        doc = resp.json()
        self._guardrails = {
            "cedar_guardrail": doc.get("cedar_guardrail"),
            "opa_guardrail": doc.get("opa_guardrail"),
        }

    async def evaluate(self, input: PolicyInput) -> PolicyResult:
        try:
            await self._load_policy()
            if self._inert:
                return PolicyResult(decision=PolicyDecision.ALLOW, engine="srs")
            assert self._guardrails is not None
            body = {
                "cedar_guardrail": self._guardrails.get("cedar_guardrail"),
                "opa_guardrail": self._guardrails.get("opa_guardrail"),
                "tool_name": input.action.tool_name,
                "tool_args": input.action.tool_input,
                "principal_id": input.principal.id,
                "bundle_id": input.resource.agent_name,
                "bundle_name": input.resource.agent_name,
            }
            resp = await self._client.post(
                f"{self._endpoint}/v1/guardrails/evaluate-tool-call",
                headers={"x-api-key": self._api_key},
                json=body,
            )
            resp.raise_for_status()
            out = resp.json()
            allowed = bool(out.get("allowed"))
            return PolicyResult(
                decision=PolicyDecision.ALLOW if allowed else PolicyDecision.DENY,
                reason=out.get("reason", "") or "",
                engine="srs",
            )
        except Exception as exc:  # noqa: BLE001 - SRS errors fail per mode (default closed)
            logger.warning("SRS policy evaluation failed (%s mode): %s", self._fail_mode, exc)
            if self._fail_mode == "allow":
                return PolicyResult(decision=PolicyDecision.ALLOW, engine="srs")
            return PolicyResult(
                decision=PolicyDecision.DENY,
                reason=f"SRS error (fail-closed): {exc}",
                engine="srs",
            )

    async def aclose(self) -> None:
        await self._client.aclose()


def srs_authorizer_from_env(agent_name: str = ""):
    """Build a ``PolicyToolAuthorizer`` backed by SRS from env, or ``None`` when
    SRS isn't reachable (no explicit ``SRS_BASE_URL``+``SRS_POLICY_ID`` and no
    ``AGENTOS_INGEST_URL`` to fetch the binding from agentos).

    The Python mirror of agentos's ``srsPolicyForAgent`` — but resolved over the
    same HTTP ingest channel the SDK already uses, so no Mongo / SRS creds are
    needed in the Python process.
    """
    explicit = bool(os.environ.get("SRS_BASE_URL") and os.environ.get("SRS_POLICY_ID"))
    if not explicit and not _ingest_base():
        return None

    from .authorizer import PolicyToolAuthorizer
    from .types import PolicyPrincipal, PolicyResource

    principal_id = os.environ.get("SRS_PRINCIPAL_ID") or agent_name or "agent"
    engine = SrsPolicyEngine.from_env(agent_name=agent_name)
    return PolicyToolAuthorizer(
        engine=engine,
        principal_resolver=lambda ctx: PolicyPrincipal(id=principal_id),
        resource_resolver=lambda ctx: PolicyResource(
            type="agent",
            agent_name=agent_name or "",
            session_id=getattr(ctx, "session_id", "") or "",
        ),
    )
