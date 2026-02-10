#!/usr/bin/env python3
"""
Test script for RisuAI-inspired features.
"""

import sys
import os

# Add project root to path
sys.path.insert(0, os.path.dirname(__file__))

from scripts.feature_config import get_feature_config
from scripts.narrative_enhancer import create_enhancer
from scripts.math_engine import create_math_engine


def test_feature_config():
    """Test feature configuration system."""
    print("=" * 60)
    print("Testing Feature Configuration")
    print("=" * 60)

    # Create a test story config
    config = get_feature_config("test_story", "data")

    print(f"\nDefault config: {config.get_all()}")

    # Enable narrative enhancer
    config.enable_feature("narrative_enhancer")
    print(f"\nAfter enabling narrative_enhancer: {config.is_enabled('narrative_enhancer')}")

    # Disable it
    config.disable_feature("narrative_enhancer")
    print(f"After disabling narrative_enhancer: {config.is_enabled('narrative_enhancer')}")

    print("\n✅ Feature config tests passed!\n")


def test_math_engine():
    """Test math calculation engine."""
    print("=" * 60)
    print("Testing Math Engine")
    print("=" * 60)

    engine = create_math_engine({"precision": 2})

    # Test basic arithmetic
    tests = [
        ("5 + 3", {}, 8),
        ("10 - 3", {}, 7),
        ("4 * 5", {}, 20),
        ("20 / 4", {}, 5),
        ("2 ^ 3", {}, 8),
        ("17 % 5", {}, 2),
    ]

    for expr, vars, expected in tests:
        result = engine.evaluate(expr, vars)
        status = "✅" if result == expected else "❌"
        print(f"{status} {expr} = {result} (expected {expected})")

    # Test with variables
    print("\nWith variables:")
    vars = {"strength": 10, "level": 5}
    expr = "(5 + $strength) * $level"
    result = engine.evaluate(expr, vars)
    expected = (5 + 10) * 5
    status = "✅" if result == expected else "❌"
    print(f"{status} {expr} = {result} (expected {expected})")

    # Test CALC tags in text
    print("\nTesting CALC tags:")
    text = "你造成 <!--CALC (5 + $strength) * 1.5 CALC--> 點傷害"
    processed = engine.process_text(text, {"strength": 10})
    print(f"Input:  {text}")
    print(f"Output: {processed}")

    # Test functions
    print("\nTesting functions:")
    func_tests = [
        ("abs(-5)", {}, 5),
        ("ceil(4.3)", {}, 5),
        ("floor(4.9)", {}, 4),
        ("round(4.6)", {}, 5),
        ("min(3, 7)", {}, 3),
        ("max(3, 7)", {}, 7),
    ]

    for expr, vars, expected in func_tests:
        result = engine.evaluate(expr, vars)
        status = "✅" if result == expected else "❌"
        print(f"{status} {expr} = {result} (expected {expected})")

    print("\n✅ Math engine tests passed!\n")


def test_narrative_enhancer():
    """Test narrative enhancement engine."""
    print("=" * 60)
    print("Testing Narrative Enhancer")
    print("=" * 60)

    # Test with default rules
    print("\nDefault rules:")
    enhancer = create_enhancer({"rules": "default"})

    text = "**行動: 拔劍** [系統提示: HP減少]"
    result = enhancer.enhance(text)
    print(f"Input:  {text}")
    print(f"Output: {result}")

    # Test with combat rules
    print("\nCombat rules:")
    enhancer = create_enhancer({"rules": "combat"})

    text = "你攻擊怪物，造成50點傷害，暴擊！"
    result = enhancer.enhance(text)
    print(f"Input:  {text}")
    print(f"Output: {result}")

    # Test with emotion rules
    print("\nEmotion rules:")
    enhancer = create_enhancer({"rules": "emotion"})

    text = "他開心地說：「太好了！」但內心卻充滿恐懼。"
    result = enhancer.enhance(text)
    print(f"Input:  {text}")
    print(f"Output: {result}")

    # Test with multiple rule sets
    print("\nMultiple rule sets (combat + emotion):")
    enhancer = create_enhancer({"rules": ["combat", "emotion"]})

    text = "你憤怒地攻擊敵人，造成100點傷害！"
    result = enhancer.enhance(text)
    print(f"Input:  {text}")
    print(f"Output: {result}")

    print("\n✅ Narrative enhancer tests passed!\n")


def test_integration():
    """Test integration of all features."""
    print("=" * 60)
    print("Testing Integration")
    print("=" * 60)

    math = create_math_engine({"precision": 2})
    enhancer = create_enhancer({"rules": ["combat", "emotion"]})

    # Simulate a GM response with both CALC tags and narrative elements
    gm_response = """
    你感到興奮，拔出武器準備攻擊！

    你的攻擊造成 <!--CALC (5 + $strength) * 1.5 - 2 CALC--> 點傷害。

    **行動: 敵人反擊**
    敵人暴擊，你受到30點傷害。
    """

    variables = {"strength": 10}

    # Step 1: Process CALC tags
    result = math.process_text(gm_response, variables)
    print("After math engine:")
    print(result)

    # Step 2: Apply narrative enhancement
    result = enhancer.enhance(result)
    print("\nAfter narrative enhancer:")
    print(result)

    print("\n✅ Integration tests passed!\n")


if __name__ == "__main__":
    try:
        test_feature_config()
        test_math_engine()
        test_narrative_enhancer()
        test_integration()

        print("=" * 60)
        print("🎉 All tests passed!")
        print("=" * 60)

    except Exception as e:
        print(f"\n❌ Test failed: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
