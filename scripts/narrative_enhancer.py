"""
Narrative Enhancement Engine - Regex-based text transformations.

Inspired by RisuAI's regex script system for enhancing narrative quality.
"""

import re
from typing import List, Tuple, Callable, Union, Dict, Any
from functools import lru_cache


class NarrativeEnhancer:
    """
    Applies regex-based transformations to enhance narrative text.

    Supports multiple rule sets that can be enabled/disabled per-story.
    """

    def __init__(self, enabled_rules: List[str] = None):
        """
        Initialize the enhancer with specified rule sets.

        Args:
            enabled_rules: List of rule set names to enable.
                          If None, uses default rules.
        """
        self.enabled_rules = enabled_rules or ["default"]
        self.rules: List[Tuple[re.Pattern, Union[str, Callable], int]] = []
        self._load_rules()

    def _load_rules(self):
        """Load transformation rules based on enabled rule sets."""
        self.rules = []

        for rule_set_name in self.enabled_rules:
            if rule_set_name == "default":
                self.rules.extend(self._get_default_rules())
            elif rule_set_name == "combat":
                self.rules.extend(self._get_combat_rules())
            elif rule_set_name == "literary":
                self.rules.extend(self._get_literary_rules())
            elif rule_set_name == "emotion":
                self.rules.extend(self._get_emotion_rules())

    def _get_default_rules(self) -> List[Tuple[re.Pattern, Union[str, Callable], int]]:
        """Get default transformation rules."""
        return [
            # Format actions with special markers
            (re.compile(r'\*\*行動[:：]\s*(.+?)\*\*', re.IGNORECASE), r'✦ \1', 0),
            (re.compile(r'\*\*動作[:：]\s*(.+?)\*\*', re.IGNORECASE), r'✦ \1', 0),

            # Format system messages
            (re.compile(r'\[系統提示[:：]\s*(.+?)\]', re.IGNORECASE), r'⚙️ \1', 0),
            (re.compile(r'\[提示[:：]\s*(.+?)\]', re.IGNORECASE), r'💡 \1', 0),
        ]

    def _get_combat_rules(self) -> List[Tuple[re.Pattern, Union[str, Callable], int]]:
        """Get combat enhancement rules."""
        return [
            # Damage formatting
            (re.compile(r'造成\s*(\d+)\s*點傷害'), r'💥 造成 \1 點傷害', 0),
            (re.compile(r'受到\s*(\d+)\s*點傷害'), r'🩸 受到 \1 點傷害', 0),

            # Combat actions
            (re.compile(r'\b攻擊\b'), r'⚔️ 攻擊', 0),
            (re.compile(r'\b防禦\b'), r'🛡️ 防禦', 0),
            (re.compile(r'\b閃避\b'), r'💨 閃避', 0),
            (re.compile(r'\b格擋\b'), r'🛡️ 格擋', 0),

            # Status effects
            (re.compile(r'\b中毒\b'), r'☠️ 中毒', 0),
            (re.compile(r'\b暈眩\b'), r'💫 暈眩', 0),
            (re.compile(r'\b流血\b'), r'🩸 流血', 0),
            (re.compile(r'\b燃燒\b'), r'🔥 燃燒', 0),

            # Critical hits
            (re.compile(r'暴擊|會心一擊|致命一擊', re.IGNORECASE), r'💀 暴擊', 0),
        ]

    def _get_literary_rules(self) -> List[Tuple[re.Pattern, Union[str, Callable], int]]:
        """Get literary style enhancement rules."""
        return [
            # Replace plain verbs with more descriptive ones
            (re.compile(r'\b說\b'), '低聲道', 0),
            (re.compile(r'\b喊\b'), '高聲呼喊', 0),
            (re.compile(r'\b看\b'), '凝視', 0),
            (re.compile(r'\b走\b'), '步行', 0),
            (re.compile(r'\b跑\b'), '疾馳', 0),

            # Enhance transitions
            (re.compile(r'^然後'), '緊接著', re.MULTILINE),
            (re.compile(r'^接著'), '隨後', re.MULTILINE),
            (re.compile(r'^最後'), '終於', re.MULTILINE),
        ]

    def _get_emotion_rules(self) -> List[Tuple[re.Pattern, Union[str, Callable], int]]:
        """Get emotion indicator rules."""
        return [
            # Positive emotions
            (re.compile(r'\b(開心|高興|快樂|喜悅)\b'), r'😊 \1', 0),
            (re.compile(r'\b(興奮|激動)\b'), r'🤩 \1', 0),
            (re.compile(r'\b(驚喜|驚訝)\b'), r'😲 \1', 0),

            # Negative emotions
            (re.compile(r'\b(憤怒|生氣|暴怒)\b'), r'😠 \1', 0),
            (re.compile(r'\b(悲傷|難過|哀傷)\b'), r'😢 \1', 0),
            (re.compile(r'\b(恐懼|害怕|驚恐)\b'), r'😱 \1', 0),
            (re.compile(r'\b(困惑|疑惑)\b'), r'🤔 \1', 0),

            # Neutral/complex emotions
            (re.compile(r'\b(思考|沉思)\b'), r'🤔 \1', 0),
            (re.compile(r'\b(疲憊|疲勞)\b'), r'😓 \1', 0),
            (re.compile(r'\b(決心|決定)\b'), r'💪 \1', 0),
        ]

    @lru_cache(maxsize=1000)
    def enhance(self, text: str, mode: str = "output") -> str:
        """
        Apply narrative enhancements to text.

        Args:
            text: Input text to transform
            mode: Processing mode ("input", "output", "display")

        Returns:
            Enhanced text with transformations applied
        """
        if not text:
            return text

        result = text
        for pattern, replacement, flags in self.rules:
            try:
                if callable(replacement):
                    result = pattern.sub(replacement, result)
                else:
                    result = pattern.sub(replacement, result)
            except Exception as e:
                print(f"Narrative enhancement error: {e}")
                continue

        return result

    def add_custom_rule(self, pattern: str, replacement: Union[str, Callable],
                       flags: int = 0):
        """
        Add a custom transformation rule.

        Args:
            pattern: Regex pattern string
            replacement: Replacement string or function
            flags: Regex flags (e.g., re.IGNORECASE)
        """
        try:
            compiled_pattern = re.compile(pattern, flags)
            self.rules.append((compiled_pattern, replacement, flags))
            # Clear cache when rules change
            self.enhance.cache_clear()
        except Exception as e:
            print(f"Failed to add custom rule: {e}")

    def clear_rules(self):
        """Clear all transformation rules."""
        self.rules = []
        self.enhance.cache_clear()


def create_enhancer(config: Dict[str, Any]) -> NarrativeEnhancer:
    """
    Create a NarrativeEnhancer from configuration.

    Args:
        config: Feature configuration dict with 'rules' key

    Returns:
        Configured NarrativeEnhancer instance
    """
    rules = config.get("rules", "default")

    if isinstance(rules, str):
        # Single rule set name
        rule_list = [rules]
    elif isinstance(rules, list):
        # Multiple rule sets
        rule_list = rules
    else:
        rule_list = ["default"]

    return NarrativeEnhancer(enabled_rules=rule_list)
