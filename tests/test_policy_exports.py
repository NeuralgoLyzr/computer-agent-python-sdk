"""The public policy surface after 0.7.0: no standalone SRS engine."""

from __future__ import annotations

import importlib

import pytest

import computeragent.policy as policy


def test_srs_engine_removed():
    assert not hasattr(policy, "SrsPolicyEngine")
    assert not hasattr(policy, "srs_authorizer_from_env")
    assert "SrsPolicyEngine" not in policy.__all__
    assert "srs_authorizer_from_env" not in policy.__all__
    with pytest.raises(ImportError):
        importlib.import_module("computeragent.policy.srs")
