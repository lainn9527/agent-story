"""Tests for the LLM state reviewer (Phase 2).

Tests _review_state_update_llm() and its integration with _run_state_gate().
All LLM calls are mocked — no real API calls.
"""

import json
from unittest.mock import patch

import pytest

import app as app_module


SCHEMA = {
    "fields": [
        {"key": "name", "label": "姓名", "type": "text"},
        {"key": "current_phase", "label": "階段", "type": "text"},
        {"key": "reward_points", "label": "獎勵點", "type": "number"},
        {"key": "current_status", "label": "狀態", "type": "text"},
        {"key": "gene_lock", "label": "基因鎖", "type": "text"},
        {"key": "physique", "label": "體質", "type": "text"},
        {"key": "spirit", "label": "精神力", "type": "text"},
        {"key": "systems", "label": "體系", "type": "map"},
    ],
    "lists": [
        {"key": "inventory", "label": "道具欄", "type": "map"},
        {
            "key": "abilities",
            "label": "能力",
            "state_add_key": "abilities_add",
            "state_remove_key": "abilities_remove",
        },
        {
            "key": "completed_missions",
            "label": "已完成任務",
            "state_add_key": "completed_missions_add",
        },
        {"key": "relationships", "label": "人際關係", "type": "map", "render": "inline"},
    ],
    "direct_overwrite_keys": [
        "gene_lock", "physique", "spirit", "current_status", "current_phase",
    ],
}

INITIAL_STATE = {
    "name": "測試者",
    "current_phase": "主神空間",
    "reward_points": 5000,
    "inventory": {"封印之鏡": "", "鎮魂符": "×5"},
    "relationships": {"小薇": "信任"},
    "completed_missions": ["咒怨 — 完美通關"],
    "abilities": ["火球術", "空間跳躍"],
    "gene_lock": "未開啟",
    "physique": "普通人",
    "spirit": "普通人",
    "current_status": "等待任務",
    "systems": {"死生之道": "B級"},
}


# ===================================================================
# _review_state_update_llm — unit tests
# ===================================================================


class TestReviewerBasics:
    def test_reviewer_returns_candidate_on_valid_patch(self):
        """Reviewer returns valid patch → merged into candidate."""
        reviewer_response = json.dumps({
            "patch": {"current_phase": "副本中"},
            "drop_keys": [],
            "reason": "修正 phase 為合法值",
        })
        with patch("app._review_state_update_llm.__module__", "app"):
            with patch("llm_bridge.call_oneshot", return_value=reviewer_response):
                result = app_module._review_state_update_llm(
                    current_state=INITIAL_STATE,
                    schema=SCHEMA,
                    original_update={"current_phase": "戰鬥", "gene_lock": "第一階"},
                    sanitized_update={"gene_lock": "第一階"},
                    violations=[{"key": "current_phase", "rule": "invalid_phase",
                                 "value": "戰鬥", "action": "drop"}],
                )
        assert result is not None
        assert result["current_phase"] == "副本中"
        assert result["gene_lock"] == "第一階"

    def test_reviewer_drop_keys_removes_from_sanitized(self):
        """Reviewer can request dropping keys from sanitized."""
        reviewer_response = json.dumps({
            "patch": {},
            "drop_keys": ["gene_lock"],
            "reason": "此欄位不應更新",
        })
        with patch("llm_bridge.call_oneshot", return_value=reviewer_response):
            result = app_module._review_state_update_llm(
                current_state=INITIAL_STATE,
                schema=SCHEMA,
                original_update={"gene_lock": "第一階"},
                sanitized_update={"gene_lock": "第一階"},
                violations=[],
            )
        assert result is not None
        assert "gene_lock" not in result

    def test_reviewer_returns_none_on_empty_response(self):
        with patch("llm_bridge.call_oneshot", return_value=""):
            result = app_module._review_state_update_llm(
                INITIAL_STATE, SCHEMA, {}, {}, [])
        assert result is None

    def test_reviewer_returns_none_on_malformed_json(self):
        with patch("llm_bridge.call_oneshot", return_value="not json"):
            result = app_module._review_state_update_llm(
                INITIAL_STATE, SCHEMA, {}, {}, [])
        assert result is None

    def test_reviewer_returns_none_on_non_dict_response(self):
        with patch("llm_bridge.call_oneshot", return_value="[1, 2, 3]"):
            result = app_module._review_state_update_llm(
                INITIAL_STATE, SCHEMA, {}, {}, [])
        assert result is None

    def test_reviewer_returns_none_on_patch_not_dict(self):
        resp = json.dumps({"patch": "bad", "drop_keys": [], "reason": ""})
        with patch("llm_bridge.call_oneshot", return_value=resp):
            result = app_module._review_state_update_llm(
                INITIAL_STATE, SCHEMA, {}, {}, [])
        assert result is None

    def test_reviewer_returns_none_on_drop_keys_not_list(self):
        resp = json.dumps({"patch": {}, "drop_keys": "bad", "reason": ""})
        with patch("llm_bridge.call_oneshot", return_value=resp):
            result = app_module._review_state_update_llm(
                INITIAL_STATE, SCHEMA, {}, {}, [])
        assert result is None

    def test_reviewer_handles_code_fence_wrapper(self):
        """LLM sometimes wraps response in ```json ... ```."""
        inner = json.dumps({"patch": {"current_phase": "副本中"}, "drop_keys": [], "reason": ""})
        resp = f"```json\n{inner}\n```"
        with patch("llm_bridge.call_oneshot", return_value=resp):
            result = app_module._review_state_update_llm(
                INITIAL_STATE, SCHEMA,
                {"current_phase": "戰鬥"},
                {},
                [{"key": "current_phase", "rule": "invalid_phase", "value": "戰鬥", "action": "drop"}],
            )
        assert result is not None
        assert result["current_phase"] == "副本中"

    def test_reviewer_returns_none_on_timeout(self):
        """Timeout should return None quickly (fallback)."""
        import time

        def slow_call(*args, **kwargs):
            time.sleep(0.2)
            return "{}"

        with patch("llm_bridge.call_oneshot", side_effect=slow_call):
            # Set very short timeout
            old_timeout = app_module.STATE_REVIEW_LLM_TIMEOUT
            app_module.STATE_REVIEW_LLM_TIMEOUT = 0.01
            try:
                t0 = time.time()
                result = app_module._review_state_update_llm(
                    INITIAL_STATE, SCHEMA, {}, {}, [])
                elapsed = time.time() - t0
            finally:
                app_module.STATE_REVIEW_LLM_TIMEOUT = old_timeout
        assert result is None
        assert elapsed < 0.15

    def test_reviewer_timeout_releases_inflight_slot_once(self, monkeypatch):
        """Timeout path should release slot; worker finally must not double-release."""
        import time

        class _CountingSemaphore:
            def __init__(self):
                self.release_calls = 0

            def acquire(self, blocking=False):
                return True

            def release(self):
                self.release_calls += 1

        sem = _CountingSemaphore()
        monkeypatch.setattr(app_module, "_STATE_REVIEW_LLM_SEM", sem)

        def slow_call(*args, **kwargs):
            time.sleep(0.05)
            return "{}"

        old_timeout = app_module.STATE_REVIEW_LLM_TIMEOUT
        app_module.STATE_REVIEW_LLM_TIMEOUT = 0.01
        try:
            with patch("llm_bridge.call_oneshot", side_effect=slow_call):
                result = app_module._review_state_update_llm(
                    INITIAL_STATE, SCHEMA, {}, {}, [])
            # Wait for worker thread to exit and run its finally block.
            time.sleep(0.08)
        finally:
            app_module.STATE_REVIEW_LLM_TIMEOUT = old_timeout

        assert result is None
        assert sem.release_calls == 1

    def test_reviewer_returns_none_on_exception(self):
        with patch("llm_bridge.call_oneshot", side_effect=RuntimeError("API down")):
            result = app_module._review_state_update_llm(
                INITIAL_STATE, SCHEMA, {}, {}, [])
        assert result is None

    def test_reviewer_drops_out_of_scope_patch_keys(self):
        """Reviewer patch must not inject keys absent from original/sanitized update."""
        reviewer_response = json.dumps({
            "patch": {"current_phase": "副本中", "修真境界": "元嬰"},
            "drop_keys": [],
            "reason": "修正",
        })
        with patch("llm_bridge.call_oneshot", return_value=reviewer_response):
            result = app_module._review_state_update_llm(
                current_state=INITIAL_STATE,
                schema=SCHEMA,
                original_update={"current_phase": "戰鬥"},
                sanitized_update={},
                violations=[{"key": "current_phase", "rule": "invalid_phase",
                             "value": "戰鬥", "action": "drop"}],
            )
        assert result is not None
        assert result["current_phase"] == "副本中"
        assert "修真境界" not in result

    def test_reviewer_logs_usage_when_story_context_provided(self):
        reviewer_response = json.dumps({
            "patch": {"current_phase": "副本中"},
            "drop_keys": [],
            "reason": "",
        })
        with patch("llm_bridge.call_oneshot", return_value=reviewer_response):
            with patch.object(app_module, "_log_llm_usage") as mock_usage:
                result = app_module._review_state_update_llm(
                    current_state=INITIAL_STATE,
                    schema=SCHEMA,
                    original_update={"current_phase": "戰鬥"},
                    sanitized_update={},
                    violations=[{"key": "current_phase", "rule": "invalid_phase",
                                 "value": "戰鬥", "action": "drop"}],
                    story_id="s1",
                    branch_id="main",
                )
        assert result is not None
        mock_usage.assert_called_once()

    def test_reviewer_inflight_limit_returns_none(self, monkeypatch):
        class _BusySemaphore:
            def acquire(self, blocking=False):
                return False

        monkeypatch.setattr(app_module, "_STATE_REVIEW_LLM_SEM", _BusySemaphore())
        with patch("llm_bridge.call_oneshot") as mock_call:
            result = app_module._review_state_update_llm(
                INITIAL_STATE, SCHEMA, {"current_phase": "戰鬥"}, {}, [])
        assert result is None
        mock_call.assert_not_called()


# ===================================================================
# _run_state_gate with LLM reviewer
# ===================================================================


class TestRunStateGateWithReviewer:
    def test_no_violations_skips_reviewer(self, monkeypatch):
        """No violations → reviewer not called even if LLM is on."""
        monkeypatch.setattr(app_module, "STATE_REVIEW_MODE", "enforce")
        monkeypatch.setattr(app_module, "STATE_REVIEW_LLM", "on")

        with patch("app._review_state_update_llm") as mock_reviewer:
            result = app_module._run_state_gate(
                {"current_phase": "副本中", "gene_lock": "第一階"},
                SCHEMA, INITIAL_STATE)
            mock_reviewer.assert_not_called()
        assert result["current_phase"] == "副本中"
        assert result["gene_lock"] == "第一階"

    def test_violations_calls_reviewer_when_on(self, monkeypatch):
        """Violations + enforce + LLM on → reviewer called."""
        monkeypatch.setattr(app_module, "STATE_REVIEW_MODE", "enforce")
        monkeypatch.setattr(app_module, "STATE_REVIEW_LLM", "on")

        reviewer_response = json.dumps({
            "patch": {"current_phase": "副本中"},
            "drop_keys": [],
            "reason": "修正 phase",
        })
        with patch("llm_bridge.call_oneshot", return_value=reviewer_response):
            result = app_module._run_state_gate(
                {"current_phase": "戰鬥", "gene_lock": "第一階"},
                SCHEMA, INITIAL_STATE)
        assert result["current_phase"] == "副本中"
        assert result["gene_lock"] == "第一階"

    def test_reviewer_off_uses_sanitized(self, monkeypatch):
        """STATE_REVIEW_LLM=off → only deterministic gate, no reviewer."""
        monkeypatch.setattr(app_module, "STATE_REVIEW_MODE", "enforce")
        monkeypatch.setattr(app_module, "STATE_REVIEW_LLM", "off")

        with patch("app._review_state_update_llm") as mock_reviewer:
            result = app_module._run_state_gate(
                {"current_phase": "戰鬥", "gene_lock": "第一階"},
                SCHEMA, INITIAL_STATE)
            mock_reviewer.assert_not_called()
        assert "current_phase" not in result
        assert result["gene_lock"] == "第一階"

    def test_reviewer_fallback_on_none(self, monkeypatch):
        """Reviewer returns None → fallback to sanitized."""
        monkeypatch.setattr(app_module, "STATE_REVIEW_MODE", "enforce")
        monkeypatch.setattr(app_module, "STATE_REVIEW_LLM", "on")

        with patch("llm_bridge.call_oneshot", return_value=""):
            result = app_module._run_state_gate(
                {"current_phase": "戰鬥", "gene_lock": "第一階"},
                SCHEMA, INITIAL_STATE)
        assert "current_phase" not in result
        assert result["gene_lock"] == "第一階"

    def test_reviewer_output_revalidated(self, monkeypatch):
        """Out-of-scope reviewer keys are dropped; valid in-scope fix still applies."""
        monkeypatch.setattr(app_module, "STATE_REVIEW_MODE", "enforce")
        monkeypatch.setattr(app_module, "STATE_REVIEW_LLM", "on")

        # Reviewer tries to sneak in a scene key
        reviewer_response = json.dumps({
            "patch": {"current_phase": "副本中", "location": "深山"},
            "drop_keys": [],
            "reason": "修正 phase 並加入場景",
        })
        with patch("llm_bridge.call_oneshot", return_value=reviewer_response):
            result = app_module._run_state_gate(
                {"current_phase": "戰鬥", "gene_lock": "第一階"},
                SCHEMA, INITIAL_STATE)
        # location is out-of-scope and dropped; current_phase repair still applies.
        assert result["current_phase"] == "副本中"
        assert "location" not in result
        assert result["gene_lock"] == "第一階"

    def test_warn_mode_skips_reviewer(self, monkeypatch):
        """warn mode never calls reviewer, even if LLM is on."""
        monkeypatch.setattr(app_module, "STATE_REVIEW_MODE", "warn")
        monkeypatch.setattr(app_module, "STATE_REVIEW_LLM", "on")

        with patch("app._review_state_update_llm") as mock_reviewer:
            result = app_module._run_state_gate(
                {"current_phase": "戰鬥", "gene_lock": "第一階"},
                SCHEMA, INITIAL_STATE)
            mock_reviewer.assert_not_called()
        # warn mode returns original
        assert result["current_phase"] == "戰鬥"

    def test_allow_llm_false_skips_reviewer(self, monkeypatch):
        """allow_llm=False (used by normalize path) → no reviewer."""
        monkeypatch.setattr(app_module, "STATE_REVIEW_MODE", "enforce")
        monkeypatch.setattr(app_module, "STATE_REVIEW_LLM", "on")

        with patch("app._review_state_update_llm") as mock_reviewer:
            result = app_module._run_state_gate(
                {"current_phase": "戰鬥", "gene_lock": "第一階"},
                SCHEMA, INITIAL_STATE, allow_llm=False)
            mock_reviewer.assert_not_called()
        assert "current_phase" not in result

    def test_reviewer_repairs_phase(self, monkeypatch):
        """Full flow: invalid phase → reviewer fixes to valid phase → passes second gate."""
        monkeypatch.setattr(app_module, "STATE_REVIEW_MODE", "enforce")
        monkeypatch.setattr(app_module, "STATE_REVIEW_LLM", "on")

        reviewer_response = json.dumps({
            "patch": {"current_phase": "副本結算"},
            "drop_keys": [],
            "reason": "根據上下文修正為副本結算",
        })
        with patch("llm_bridge.call_oneshot", return_value=reviewer_response):
            result = app_module._run_state_gate(
                {"current_phase": "結算中", "reward_points_delta": 1000},
                SCHEMA, INITIAL_STATE)
        assert result["current_phase"] == "副本結算"
        assert result["reward_points_delta"] == 1000

    def test_inventory_add_fallback_survives_reviewer_path(self, monkeypatch):
        """inventory_add (fallback key) must work even when reviewer is active."""
        monkeypatch.setattr(app_module, "STATE_REVIEW_MODE", "enforce")
        monkeypatch.setattr(app_module, "STATE_REVIEW_LLM", "on")

        # Update has both a violation and a fallback key
        reviewer_response = json.dumps({
            "patch": {"current_phase": "副本中"},
            "drop_keys": [],
            "reason": "修正 phase",
        })
        with patch("llm_bridge.call_oneshot", return_value=reviewer_response):
            result = app_module._run_state_gate(
                {"current_phase": "戰鬥", "inventory_add": ["新道具"]},
                SCHEMA, INITIAL_STATE)
        assert result["current_phase"] == "副本中"
        assert result["inventory_add"] == ["新道具"]

    def test_reviewer_cannot_inject_new_top_level_keys(self, monkeypatch):
        monkeypatch.setattr(app_module, "STATE_REVIEW_MODE", "enforce")
        monkeypatch.setattr(app_module, "STATE_REVIEW_LLM", "on")

        reviewer_response = json.dumps({
            "patch": {"current_phase": "副本中", "修真境界": "元嬰"},
            "drop_keys": [],
            "reason": "修正",
        })
        with patch("llm_bridge.call_oneshot", return_value=reviewer_response):
            result = app_module._run_state_gate(
                {"current_phase": "戰鬥", "gene_lock": "第一階"},
                SCHEMA, INITIAL_STATE)
        assert result["current_phase"] == "副本中"
        assert result["gene_lock"] == "第一階"
        assert "修真境界" not in result


class TestEnvParsingHelpers:
    def test_parse_env_float_invalid_falls_back(self, monkeypatch):
        monkeypatch.setenv("TEST_FLOAT_ENV", "bad")
        assert app_module._parse_env_float("TEST_FLOAT_ENV", 12.5) == 12.5

    def test_parse_env_int_invalid_falls_back(self, monkeypatch):
        monkeypatch.setenv("TEST_INT_ENV", "bad")
        assert app_module._parse_env_int("TEST_INT_ENV", 7) == 7
