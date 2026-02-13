"""Tests for branch tree logic in app.py (Phase 2.1).

Tests get_full_timeline, _get_fork_points, _get_sibling_groups,
_resolve_sibling_parent. Uses fixture timeline trees and messages.
"""

import json

import pytest

import app as app_module


@pytest.fixture(autouse=True)
def patch_app_paths(tmp_path, monkeypatch):
    """Redirect app paths to tmp_path."""
    stories_dir = tmp_path / "data" / "stories"
    stories_dir.mkdir(parents=True)
    monkeypatch.setattr(app_module, "STORIES_DIR", str(stories_dir))
    monkeypatch.setattr(app_module, "BASE_DIR", str(tmp_path))
    return stories_dir


@pytest.fixture
def story_id():
    return "test_story"


@pytest.fixture
def setup_tree(tmp_path, story_id):
    """Create a story with timeline tree and messages.

    Returns a helper to write tree and messages.
    """
    story_dir = tmp_path / "data" / "stories" / story_id
    story_dir.mkdir(parents=True, exist_ok=True)

    def _setup(tree, parsed_messages=None, branch_messages=None):
        """
        tree: timeline_tree dict
        parsed_messages: list of messages for parsed_conversation.json (base messages)
        branch_messages: dict of {branch_id: [messages]} for per-branch delta
        """
        # Write timeline_tree.json
        (story_dir / "timeline_tree.json").write_text(
            json.dumps(tree, ensure_ascii=False, indent=2), encoding="utf-8"
        )

        # Write parsed_conversation.json (base messages)
        parsed = parsed_messages or []
        (story_dir / "parsed_conversation.json").write_text(
            json.dumps(parsed, ensure_ascii=False, indent=2), encoding="utf-8"
        )

        # Write per-branch messages
        if branch_messages:
            for bid, msgs in branch_messages.items():
                branch_dir = story_dir / "branches" / bid
                branch_dir.mkdir(parents=True, exist_ok=True)
                (branch_dir / "messages.json").write_text(
                    json.dumps(msgs, ensure_ascii=False, indent=2), encoding="utf-8"
                )

        # Ensure all branch dirs exist
        for bid in tree.get("branches", {}):
            branch_dir = story_dir / "branches" / bid
            branch_dir.mkdir(parents=True, exist_ok=True)
            if not (branch_dir / "messages.json").exists():
                (branch_dir / "messages.json").write_text("[]", encoding="utf-8")

    return _setup


def _msg(index, role="user", content=None):
    return {"index": index, "role": role, "content": content or f"msg_{index}"}


# ===================================================================
# get_full_timeline
# ===================================================================


class TestGetFullTimeline:
    def test_main_branch_linear(self, story_id, setup_tree):
        tree = {
            "active_branch_id": "main",
            "branches": {
                "main": {"id": "main", "parent_branch_id": None, "branch_point_index": None},
            },
        }
        parsed = [_msg(0), _msg(1, "assistant"), _msg(2), _msg(3, "assistant")]
        setup_tree(tree, parsed_messages=parsed)

        timeline = app_module.get_full_timeline(story_id, "main")
        assert len(timeline) == 4
        assert all(m["owner_branch_id"] == "main" for m in timeline)

    def test_forked_branch(self, story_id, setup_tree):
        tree = {
            "active_branch_id": "branch_a",
            "branches": {
                "main": {"id": "main", "parent_branch_id": None, "branch_point_index": None},
                "branch_a": {"id": "branch_a", "parent_branch_id": "main", "branch_point_index": 3},
            },
        }
        parsed = [_msg(0), _msg(1, "assistant"), _msg(2), _msg(3, "assistant"), _msg(4), _msg(5, "assistant")]
        branch_msgs = {
            "branch_a": [_msg(4, content="branch_a_msg_4"), _msg(5, "assistant", "branch_a_msg_5")],
        }
        setup_tree(tree, parsed_messages=parsed, branch_messages=branch_msgs)

        timeline = app_module.get_full_timeline(story_id, "branch_a")
        # base messages up to index 3 + branch_a delta
        assert len(timeline) == 6  # 0,1,2,3 + 4,5 from branch_a
        assert timeline[3]["index"] == 3
        assert timeline[4]["content"] == "branch_a_msg_4"
        assert timeline[4]["owner_branch_id"] == "branch_a"

    def test_three_level_deep(self, story_id, setup_tree):
        tree = {
            "active_branch_id": "branch_c",
            "branches": {
                "main": {"id": "main", "parent_branch_id": None, "branch_point_index": None},
                "branch_a": {"id": "branch_a", "parent_branch_id": "main", "branch_point_index": 3},
                "branch_c": {"id": "branch_c", "parent_branch_id": "branch_a", "branch_point_index": 5},
            },
        }
        parsed = [_msg(0), _msg(1, "assistant"), _msg(2), _msg(3, "assistant")]
        branch_msgs = {
            "branch_a": [_msg(4), _msg(5, "assistant"), _msg(6), _msg(7, "assistant")],
            "branch_c": [_msg(6, content="c_msg_6"), _msg(7, "assistant", "c_msg_7")],
        }
        setup_tree(tree, parsed_messages=parsed, branch_messages=branch_msgs)

        timeline = app_module.get_full_timeline(story_id, "branch_c")
        # main: 0-3, branch_a: 4-5, branch_c: 6-7
        assert len(timeline) == 8
        assert timeline[5]["owner_branch_id"] == "branch_a"
        assert timeline[6]["content"] == "c_msg_6"
        assert timeline[6]["owner_branch_id"] == "branch_c"

    def test_blank_branch_empty_timeline(self, story_id, setup_tree):
        tree = {
            "active_branch_id": "blank_b",
            "branches": {
                "main": {"id": "main", "parent_branch_id": None, "branch_point_index": None},
                "blank_b": {"id": "blank_b", "parent_branch_id": "main", "branch_point_index": -1, "blank": True},
            },
        }
        parsed = [_msg(0), _msg(1, "assistant")]
        setup_tree(tree, parsed_messages=parsed)

        timeline = app_module.get_full_timeline(story_id, "blank_b")
        # branch_point_index=-1 → truncate all parsed messages (keep index <= -1 → nothing)
        # Then blank branch's own delta messages
        assert len(timeline) == 0  # No delta messages for blank branch

    def test_nonexistent_branch_returns_base(self, story_id, setup_tree):
        tree = {"active_branch_id": "main", "branches": {}}
        parsed = [_msg(0), _msg(1, "assistant")]
        setup_tree(tree, parsed_messages=parsed)

        timeline = app_module.get_full_timeline(story_id, "nonexistent")
        assert len(timeline) == 2  # Falls back to parsed_conversation

    def test_circular_parent_no_infinite_loop(self, story_id, setup_tree):
        """Circular parent references should not cause infinite loop (G1)."""
        tree = {
            "active_branch_id": "b1",
            "branches": {
                "main": {"id": "main", "parent_branch_id": None, "branch_point_index": None},
                "b1": {"id": "b1", "parent_branch_id": "b2", "branch_point_index": 3},
                "b2": {"id": "b2", "parent_branch_id": "b1", "branch_point_index": 3},
            },
        }
        parsed = [_msg(0), _msg(1, "assistant"), _msg(2), _msg(3, "assistant")]
        setup_tree(tree, parsed_messages=parsed)

        # Should terminate without hanging
        timeline = app_module.get_full_timeline(story_id, "b1")
        assert isinstance(timeline, list)

    def test_deleted_parent_in_chain_no_crash(self, story_id, setup_tree):
        """Missing parent in ancestor chain should not crash with KeyError (G2)."""
        tree = {
            "active_branch_id": "child",
            "branches": {
                "main": {"id": "main", "parent_branch_id": None, "branch_point_index": None},
                # "missing_parent" is referenced but not in branches dict
                "child": {"id": "child", "parent_branch_id": "missing_parent", "branch_point_index": 3},
            },
        }
        parsed = [_msg(0), _msg(1, "assistant"), _msg(2), _msg(3, "assistant")]
        branch_msgs = {
            "child": [_msg(4, content="child_msg")],
        }
        setup_tree(tree, parsed_messages=parsed, branch_messages=branch_msgs)

        # Should not raise KeyError
        timeline = app_module.get_full_timeline(story_id, "child")
        assert isinstance(timeline, list)
        # Should still have the child's own delta messages
        assert any(m.get("content") == "child_msg" for m in timeline)


# ===================================================================
# _get_fork_points
# ===================================================================


class TestGetForkPoints:
    def test_simple_fork(self, story_id, setup_tree):
        tree = {
            "active_branch_id": "main",
            "branches": {
                "main": {"id": "main", "parent_branch_id": None, "branch_point_index": None},
                "branch_a": {"id": "branch_a", "parent_branch_id": "main", "branch_point_index": 5, "name": "分支A"},
            },
        }
        setup_tree(tree)
        forks = app_module._get_fork_points(story_id, "main")
        assert 5 in forks
        assert forks[5][0]["branch_id"] == "branch_a"

    def test_excludes_deleted(self, story_id, setup_tree):
        tree = {
            "active_branch_id": "main",
            "branches": {
                "main": {"id": "main", "parent_branch_id": None, "branch_point_index": None},
                "deleted_b": {"id": "deleted_b", "parent_branch_id": "main", "branch_point_index": 5, "deleted": True},
            },
        }
        setup_tree(tree)
        forks = app_module._get_fork_points(story_id, "main")
        assert forks == {}

    def test_excludes_blank(self, story_id, setup_tree):
        tree = {
            "active_branch_id": "main",
            "branches": {
                "main": {"id": "main", "parent_branch_id": None, "branch_point_index": None},
                "blank_b": {"id": "blank_b", "parent_branch_id": "main", "branch_point_index": -1, "blank": True},
            },
        }
        setup_tree(tree)
        forks = app_module._get_fork_points(story_id, "main")
        assert forks == {}

    def test_excludes_merged(self, story_id, setup_tree):
        tree = {
            "active_branch_id": "main",
            "branches": {
                "main": {"id": "main", "parent_branch_id": None, "branch_point_index": None},
                "merged_b": {"id": "merged_b", "parent_branch_id": "main", "branch_point_index": 5, "merged": True},
            },
        }
        setup_tree(tree)
        forks = app_module._get_fork_points(story_id, "main")
        assert forks == {}

    def test_multiple_forks_at_same_index(self, story_id, setup_tree):
        tree = {
            "active_branch_id": "main",
            "branches": {
                "main": {"id": "main", "parent_branch_id": None, "branch_point_index": None},
                "b1": {"id": "b1", "parent_branch_id": "main", "branch_point_index": 5, "name": "B1"},
                "b2": {"id": "b2", "parent_branch_id": "main", "branch_point_index": 5, "name": "B2"},
            },
        }
        setup_tree(tree)
        forks = app_module._get_fork_points(story_id, "main")
        assert 5 in forks
        assert len(forks[5]) == 2

    def test_fork_on_child_branch(self, story_id, setup_tree):
        tree = {
            "active_branch_id": "branch_c",
            "branches": {
                "main": {"id": "main", "parent_branch_id": None, "branch_point_index": None},
                "branch_a": {"id": "branch_a", "parent_branch_id": "main", "branch_point_index": 5},
                "branch_c": {"id": "branch_c", "parent_branch_id": "branch_a", "branch_point_index": 8, "name": "C"},
            },
        }
        setup_tree(tree)
        # When viewing branch_a, branch_c should show as fork
        forks = app_module._get_fork_points(story_id, "branch_a")
        assert 8 in forks


# ===================================================================
# _resolve_sibling_parent
# ===================================================================


class TestResolveSiblingParent:
    def test_normal_branch(self):
        branches = {
            "main": {"id": "main", "parent_branch_id": None, "branch_point_index": None},
            "b1": {"id": "b1", "parent_branch_id": "main", "branch_point_index": 5},
        }
        # New branch at index 10 under b1 → b1 is correct parent
        result = app_module._resolve_sibling_parent(branches, "b1", 10)
        assert result == "b1"

    def test_sibling_at_same_index(self):
        branches = {
            "main": {"id": "main", "parent_branch_id": None, "branch_point_index": None},
            "b1": {"id": "b1", "parent_branch_id": "main", "branch_point_index": 5},
        }
        # New branch at index 5 under b1 → should walk up to main (sibling)
        result = app_module._resolve_sibling_parent(branches, "b1", 5)
        assert result == "main"

    def test_sibling_below_parent_index(self):
        branches = {
            "main": {"id": "main", "parent_branch_id": None, "branch_point_index": None},
            "b1": {"id": "b1", "parent_branch_id": "main", "branch_point_index": 5},
        }
        # New branch at index 3 under b1 (< 5) → walk up to main
        result = app_module._resolve_sibling_parent(branches, "b1", 3)
        assert result == "main"

    def test_deep_chain_walks_up(self):
        branches = {
            "main": {"id": "main", "parent_branch_id": None, "branch_point_index": None},
            "b1": {"id": "b1", "parent_branch_id": "main", "branch_point_index": 5},
            "b2": {"id": "b2", "parent_branch_id": "b1", "branch_point_index": 5},
        }
        # b2's branch_point is 5, same as b1's → should walk up to main
        result = app_module._resolve_sibling_parent(branches, "b2", 5)
        assert result == "main"


# ===================================================================
# _get_sibling_groups
# ===================================================================


class TestGetSiblingGroups:
    def test_two_siblings(self, story_id, setup_tree):
        tree = {
            "active_branch_id": "main",
            "branches": {
                "main": {"id": "main", "parent_branch_id": None, "branch_point_index": None, "name": "主線"},
                "b1": {"id": "b1", "parent_branch_id": "main", "branch_point_index": 5, "name": "B1", "created_at": "2026-01-01"},
                "b2": {"id": "b2", "parent_branch_id": "main", "branch_point_index": 5, "name": "B2", "created_at": "2026-01-02"},
            },
        }
        # Main has continuation past index 5
        parsed = [_msg(i) for i in range(10)]
        branch_msgs = {
            "b1": [_msg(6, content="b1_msg")],
            "b2": [_msg(6, content="b2_msg")],
        }
        setup_tree(tree, parsed_messages=parsed, branch_messages=branch_msgs)

        groups = app_module._get_sibling_groups(story_id, "main")
        # Divergent index = branch_point_index + 1 = 6
        assert "6" in groups
        group = groups["6"]
        assert group["total"] >= 2
        # main continuation + b1 + b2 = 3 variants
        assert group["total"] == 3

    def test_no_siblings(self, story_id, setup_tree):
        tree = {
            "active_branch_id": "main",
            "branches": {
                "main": {"id": "main", "parent_branch_id": None, "branch_point_index": None},
            },
        }
        setup_tree(tree)
        groups = app_module._get_sibling_groups(story_id, "main")
        assert groups == {}

    def test_excludes_blank(self, story_id, setup_tree):
        tree = {
            "active_branch_id": "main",
            "branches": {
                "main": {"id": "main", "parent_branch_id": None, "branch_point_index": None},
                "blank": {"id": "blank", "parent_branch_id": "main", "branch_point_index": -1, "blank": True},
            },
        }
        setup_tree(tree)
        groups = app_module._get_sibling_groups(story_id, "main")
        assert groups == {}

    def test_empty_branch_excluded(self, story_id, setup_tree):
        """Branches with no delta messages are excluded (orphan from interrupted stream)."""
        tree = {
            "active_branch_id": "main",
            "branches": {
                "main": {"id": "main", "parent_branch_id": None, "branch_point_index": None, "name": "主線"},
                "orphan": {"id": "orphan", "parent_branch_id": "main", "branch_point_index": 5, "name": "孤兒", "created_at": "2026-01-01"},
            },
        }
        parsed = [_msg(i) for i in range(10)]
        # orphan has no messages (empty delta)
        setup_tree(tree, parsed_messages=parsed)
        groups = app_module._get_sibling_groups(story_id, "main")
        # Single orphan without messages → no sibling group
        assert groups == {}
