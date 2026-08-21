"""SRS-backed :class:`PolicyEngine` — delegates tool-call authorization to Lyzr
SRS, so Python agents enforce the **same RAI policies authored in the Console**
(OPA + Cedar guardrails) as the TypeScript stack.

**Every SRS call is routed through the agentos ingest proxy** (bearer token —
``AGENTOS_INGEST_URL`` / ``AGENTOS_INGEST_TOKEN``), the same channel library-mode
agents already use for telemetry. agentos resolves the per-agent
``agent_policies`` binding (its ``srsPolicyForAgent``) and proxies both the
policy fetch and the per-tool evaluation to SRS, injecting the SRS key
**server-side**. So the Python process needs **no Mongo creds, no SRS key, and
no network path to SRS** — it only ever talks to agentos.

Resolution order:
  1. explicit ``endpoint`` + ``SRS_POLICY_ID`` → legacy *standalone* path that
     calls SRS directly (testing / no agentos), else
  2. ``GET {ingest_base}/policy-config/{agent_name}`` → agentos returns just the
     bound ``policyId`` (no endpoint/key); all further calls go via agentos.
Unbound agent → no policy → runs free (inert), like TS returning null.

Enforcement: fetch the RAI policy once (cache cedar + opa guardrails), then
``POST .../guardrails/evaluate-tool-call`` per tool — both through the agentos
proxy. Fail-closed on SRS errors; fail-open if the binding can't be resolved.

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
    """Derive the agentos ingest base (``.../agentos/api/ingest``) from the
    resolved ingest endpoint: the explicit ``AGENTOS_INGEST_URL`` override wins,
    else the AgentOS discovery document (``AGENTOS_DISCOVERY_URL``) — the same
    resolution the telemetry sink uses. Without this discovery fallback, a
    deployment configured ONLY via discovery (the documented one-config setup)
    silently ran with no SRS policy wiring at all — console-bound guardrails
    were never enforced."""
    url = os.environ.get("AGENTOS_INGEST_URL")
    if not url:
        from .. import agentos

        url = agentos.ingest_url()
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
        self._via_agentos = False  # set in _resolve_config: proxy vs direct SRS
        self._client = client or httpx.AsyncClient(timeout=timeout)

    @classmethod
    def from_env(cls, *, agent_name: str = "", **overrides: Any) -> SrsPolicyEngine:
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
        """Decide transport + resolve ``policy_id`` (once).

        Default: route **through the agentos ingest proxy** (bearer token) — the
        SDK never holds the SRS api key nor needs network access to SRS; agentos
        injects the key server-side. An explicit ``endpoint`` + ``policy_id``
        selects a legacy standalone path that calls SRS directly (testing only).
        """
        if self._resolved:
            return
        self._resolved = True
        if self._endpoint and self._policy_id:
            self._via_agentos = False  # explicit standalone SRS (no agentos)
            return
        if not self._agentos_base or not self._agent_name:
            self._inert = True
            return
        self._via_agentos = True
        try:
            resp = await self._client.get(
                f"{self._agentos_base}/policy-config/{self._agent_name}",
                headers=self._headers(),
            )
            resp.raise_for_status()
            policy = (resp.json() or {}).get("policy")
            if not policy or not policy.get("policyId"):
                self._inert = True  # unbound -> run free
                return
            self._policy_id = policy.get("policyId")
        except Exception as exc:  # noqa: BLE001 - a control-plane blip shouldn't brick the agent
            logger.warning("agentos policy-config fetch failed for %r: %s", self._agent_name, exc)
            self._inert = True

    def _url(self, suffix: str) -> str:
        """Build the SRS URL — via the agentos proxy (default) or SRS directly
        (standalone). ``suffix`` is the SRS path *without* the ``/v1`` prefix,
        e.g. ``/rai/policies/<id>`` or ``/guardrails/evaluate-tool-call``."""
        if self._via_agentos:
            return f"{self._agentos_base}{suffix}"
        return f"{self._endpoint}/v1{suffix}"

    def _headers(self) -> dict[str, str]:
        """Auth for the chosen transport: agentos bearer token (proxy) vs the
        SRS api key (standalone direct)."""
        if self._via_agentos:
            return {"authorization": f"Bearer {self._agentos_token}"} if self._agentos_token else {}
        return {"x-api-key": self._api_key}

    async def _load_policy(self) -> None:
        await self._resolve_config()
        if self._inert or self._guardrails is not None:
            return
        resp = await self._client.get(
            self._url(f"/rai/policies/{self._policy_id}"),
            headers=self._headers(),
        )
        resp.raise_for_status()
        doc = resp.json()
        self._guardrails = {
            "cedar_guardrail": doc.get("cedar_guardrail"),
            "opa_guardrail": doc.get("opa_guardrail"),
        }

    def _mode(self) -> str:
        """Guardrail enforcement mode (``enforce``/``audit``) from the fetched
        config — OPA's ``mode`` preferred, then Cedar's, default ``enforce``.
        Mirrors the TS SrsPolicyDecider.mode()."""
        if not self._guardrails:
            return "enforce"
        for key in ("opa_guardrail", "cedar_guardrail"):
            g = self._guardrails.get(key)
            if isinstance(g, dict) and g.get("enabled") is not False:
                m = g.get("mode")
                if isinstance(m, str):
                    return "audit" if m == "audit" else "enforce"
        return "enforce"

    async def evaluate(self, input: PolicyInput) -> PolicyResult:
        try:
            await self._load_policy()
            if self._inert:
                return PolicyResult(
                    decision=PolicyDecision.ALLOW, engine="srs", outcome="allowed"
                )
            assert self._guardrails is not None
            mode = self._mode()
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
                self._url("/guardrails/evaluate-tool-call"),
                headers=self._headers(),
                json=body,
            )
            resp.raise_for_status()
            out = resp.json()
            # SRS enforces the mode in `allowed` (audit ⇒ always true). Derive
            # the trace-facing outcome; a violation is a real deny OR an
            # audit-mode finding SRS surfaced via denied_by/reason.
            allowed = bool(out.get("allowed"))
            reason = out.get("reason", "") or ""
            violation = (not allowed) or bool(out.get("denied_by")) or bool(reason)
            if mode == "audit":
                outcome = "audit_violation" if violation else "audit_clean"
            else:
                outcome = "allowed" if allowed else "blocked"
            return PolicyResult(
                decision=PolicyDecision.ALLOW if allowed else PolicyDecision.DENY,
                reason=reason,
                engine="srs",
                mode=mode,
                outcome=outcome,
                policy_id=self._policy_id,
            )
        except Exception as exc:  # noqa: BLE001 - SRS errors fail per mode (default closed)
            logger.warning("SRS policy evaluation failed (%s mode): %s", self._fail_mode, exc)
            if self._fail_mode == "allow":
                return PolicyResult(
                    decision=PolicyDecision.ALLOW, engine="srs", outcome="error"
                )
            return PolicyResult(
                decision=PolicyDecision.DENY,
                reason=f"SRS error (fail-closed): {exc}",
                engine="srs",
                outcome="error",
                policy_id=self._policy_id,
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
