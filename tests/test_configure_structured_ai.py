from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest


SCRIPT = (
    Path(__file__).resolve().parents[1]
    / "scripts"
    / "configure-structured-ai.py"
)
SPEC = importlib.util.spec_from_file_location("configure_structured_ai", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def test_redacted_remote_requires_explicit_consent() -> None:
    with pytest.raises(RuntimeError, match="explicit external provider consent"):
        MODULE.updated_configuration(
            {},
            privacy_mode="redacted_remote",
            rollout_mode="shadow",
            external_provider_consent=False,
        )


def test_redacted_remote_shadow_preserves_unrelated_configuration() -> None:
    original = {
        "source": {"mode": "local_filesystem"},
        "private_canary": "preserve-without-printing",
        "ai": {
            "model": "selected-model",
            "max_calls_per_sync": 3,
            "custom_future_setting": "keep",
        },
    }

    updated = MODULE.updated_configuration(
        original,
        privacy_mode="redacted_remote",
        rollout_mode="shadow",
        external_provider_consent=True,
    )

    assert updated["source"] == original["source"]
    assert updated["private_canary"] == "preserve-without-printing"
    assert updated["ai"]["enabled"] is True
    assert updated["ai"]["provider"] == "hermes"
    assert updated["ai"]["privacy_mode"] == "redacted_remote"
    assert updated["ai"]["rollout_mode"] == "shadow"
    assert updated["ai"]["model"] == "selected-model"
    assert updated["ai"]["max_calls_per_sync"] == 3
    assert updated["ai"]["custom_future_setting"] == "keep"


def test_local_only_does_not_require_external_consent() -> None:
    updated = MODULE.updated_configuration(
        {},
        privacy_mode="local_only",
        rollout_mode="assist",
        external_provider_consent=False,
    )

    assert updated["ai"]["enabled"] is True
    assert updated["ai"]["privacy_mode"] == "local_only"
    assert updated["ai"]["rollout_mode"] == "assist"
