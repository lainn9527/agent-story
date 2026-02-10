"""
Per-story feature configuration system.

Loads and manages feature toggles from data/stories/<story_id>/features.json
"""

import json
import os
from pathlib import Path
from typing import Dict, Any

# Default feature configuration
DEFAULT_FEATURES = {
    "narrative_enhancer": {
        "enabled": False,
        "rules": "default"  # or path to custom rules file
    },
    "math_engine": {
        "enabled": False,
        "precision": 2  # decimal places
    },
    "trigger_system": {
        "enabled": False
    },
    "command_parser": {
        "enabled": False
    }
}


class FeatureConfig:
    """Manages per-story feature configuration."""

    def __init__(self, story_id: str, data_dir: str = "data"):
        self.story_id = story_id
        self.data_dir = data_dir
        self.config_path = Path(data_dir) / "stories" / story_id / "features.json"
        self._config = None
        self._load()

    def _load(self):
        """Load feature configuration from file."""
        if self.config_path.exists():
            try:
                with open(self.config_path, 'r', encoding='utf-8') as f:
                    self._config = json.load(f)
            except Exception as e:
                print(f"Failed to load features.json for {self.story_id}: {e}")
                self._config = DEFAULT_FEATURES.copy()
        else:
            # Create default config file
            self._config = DEFAULT_FEATURES.copy()
            self._save()

    def _save(self):
        """Save feature configuration to file."""
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with open(self.config_path, 'w', encoding='utf-8') as f:
                json.dump(self._config, f, indent=2, ensure_ascii=False)
        except Exception as e:
            print(f"Failed to save features.json for {self.story_id}: {e}")

    def is_enabled(self, feature_name: str) -> bool:
        """Check if a feature is enabled."""
        feature = self._config.get(feature_name, {})
        if isinstance(feature, dict):
            return feature.get("enabled", False)
        return False

    def get_feature_config(self, feature_name: str) -> Dict[str, Any]:
        """Get full configuration for a feature."""
        return self._config.get(feature_name, {})

    def enable_feature(self, feature_name: str):
        """Enable a feature."""
        if feature_name not in self._config:
            self._config[feature_name] = DEFAULT_FEATURES.get(feature_name, {"enabled": True})
        else:
            if isinstance(self._config[feature_name], dict):
                self._config[feature_name]["enabled"] = True
            else:
                self._config[feature_name] = {"enabled": True}
        self._save()

    def disable_feature(self, feature_name: str):
        """Disable a feature."""
        if feature_name in self._config:
            if isinstance(self._config[feature_name], dict):
                self._config[feature_name]["enabled"] = False
            else:
                self._config[feature_name] = {"enabled": False}
            self._save()

    def get_all(self) -> Dict[str, Any]:
        """Get all feature configurations."""
        return self._config.copy()

    def update_feature_config(self, feature_name: str, config: Dict[str, Any]):
        """Update configuration for a specific feature."""
        if feature_name in self._config:
            self._config[feature_name].update(config)
        else:
            self._config[feature_name] = config
        self._save()


def get_feature_config(story_id: str, data_dir: str = "data") -> FeatureConfig:
    """Get feature configuration for a story."""
    return FeatureConfig(story_id, data_dir)
