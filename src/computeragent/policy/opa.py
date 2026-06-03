"""``OpaPolicyEngine`` — remote-HTTP OPA backend.

POSTs to ``{url}/v1/data/{policy_path}`` with body ``{"input": <PolicyInput>}``.
Parses OPA's response in either of two common shapes:

* ``{"result": true | false}`` — bare boolean decision.
* ``{"result": {"allow": bool, "reason": str?, "obligations": list?}}`` —
  object form with optional reason / advisory obligations.

Failure modes (network error, timeout, non-2xx, malformed JSON) honour
``fail_mode=`` — DENY by default, ``"allow"`` for non-prod environments.
Uses ``httpx`` which is already a core dependency, so no extra to install.
"""

from __future__ import annotations

import logging
from typing import Any, cast

import httpx

from .types import FailMode, PolicyDecision, PolicyInput, PolicyResult

logger = logging.getLogger("computeragent.policy")


class OpaPolicyEngine:
    """Open Policy Agent backend over OPA's REST data API.

    Parameters mirror an OPA sidecar deployment. The ``policy_path`` is the
    period-or-slash-separated path under ``/v1/data``: e.g. for a Rego file
    ``package computeragent.tools`` with rule ``allow``, set
    ``policy_path="computeragent/tools/allow"``.
    """

    name = "opa"

    def __init__(
        self,
        *,
        url: str,
        policy_path: str,
        fail_mode: FailMode = "deny",
        timeout: float = 2.0,
        headers: dict[str, str] | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._url = url.rstrip("/")
        self._policy_path = policy_path.strip("/").replace(".", "/")
        self._fail_mode = fail_mode
        self._timeout = timeout
        self._headers = headers or {}
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(timeout=timeout)

    async def evaluate(self, input: PolicyInput) -> PolicyResult:
        endpoint = f"{self._url}/v1/data/{self._policy_path}"
        body = {"input": input.to_json()}
        try:
            response = await self._client.post(
                endpoint,
                json=body,
                headers=self._headers,
                timeout=self._timeout,
            )
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            # Connection error, timeout, non-2xx, JSON decode failure.
            return self._failure_result(exc)

        return self._parse_result(payload)

    async def aclose(self) -> None:
        if self._owns_client:
            try:
                await self._client.aclose()
            except Exception:  # noqa: BLE001 - cleanup must not raise
                logger.debug("opa client aclose failed", exc_info=True)

    # ── helpers ────────────────────────────────────────────────────────────

    def _parse_result(self, payload: dict[str, Any]) -> PolicyResult:
        result_val = payload.get("result")
        if isinstance(result_val, bool):
            return PolicyResult(
                decision=PolicyDecision.ALLOW if result_val else PolicyDecision.DENY,
                reason="opa allowed" if result_val else "opa denied",
                engine=self.name,
            )
        if isinstance(result_val, dict):
            allow = bool(result_val.get("allow", False))
            reason = str(result_val.get("reason") or "")
            obligations_raw = result_val.get("obligations") or []
            obligations: list[dict[str, Any]] = []
            if isinstance(obligations_raw, list):
                obligations = [
                    cast("dict[str, Any]", o) for o in obligations_raw if isinstance(o, dict)
                ]
            return PolicyResult(
                decision=PolicyDecision.ALLOW if allow else PolicyDecision.DENY,
                reason=reason or ("opa allowed" if allow else "opa denied"),
                obligations=obligations,
                engine=self.name,
            )
        # Unknown shape — treat as deny with diagnostics.
        return self._failure_result(
            ValueError(f"unexpected OPA result shape: {type(result_val).__name__}")
        )

    def _failure_result(self, exc: BaseException) -> PolicyResult:
        if self._fail_mode == "allow":
            logger.warning("OPA policy engine error; fail_mode=allow → allowing: %s", exc)
            return PolicyResult(
                decision=PolicyDecision.ALLOW,
                reason=f"policy engine unavailable (fail_mode=allow): {exc}",
                engine=self.name,
            )
        logger.warning("OPA policy engine error; fail_mode=deny → denying: %s", exc)
        return PolicyResult(
            decision=PolicyDecision.DENY,
            reason=f"policy engine unavailable: {exc}",
            engine=self.name,
        )
