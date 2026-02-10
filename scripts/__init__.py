"""
RisuAI-inspired feature modules for Story RPG.

This package contains optional features that can be enabled per-story via features.json:
- narrative_enhancer: Regex-based text transformations for enhanced storytelling
- math_engine: Mathematical expression evaluator for damage/stat calculations
- trigger_system: Event-driven automation system
- command_parser: In-game command interpreter
"""

__all__ = [
    'feature_config',
    'narrative_enhancer',
    'math_engine',
]
