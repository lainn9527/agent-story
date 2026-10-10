"""Recorder overrides remain local to each request and keep provider/usage routing."""
import pytest
from story_core import llm_bridge as bridge
from story_core.card_mode import ensemble


@pytest.mark.parametrize('value,expected', [(None,None), ('',None), ('  ',None), (' lite ', 'lite')])
def test_recorder_setting(monkeypatch, value, expected):
    monkeypatch.setattr(bridge, '_get_config', lambda: {'provider':'vertex', 'vertex':{'recorder_model':value}})
    monkeypatch.setattr(bridge, 'get_provider', lambda: 'vertex')
    assert bridge.get_recorder_model() == expected


def test_ensemble_override_does_not_change_narration_model(monkeypatch):
    config={'provider':'vertex','vertex':{'model':'narrator','recorder_model':'lite'}}
    monkeypatch.setattr(bridge, '_get_config', lambda: config)
    monkeypatch.setattr(bridge, 'get_provider', lambda: 'vertex')
    captured=[]
    metered=[]
    def generate(prompt, **kw):
        captured.append(kw['model'])
        return '{"updates": []}'
    monkeypatch.setattr('story_core.vertex_bridge.call_vertex_oneshot', generate)
    monkeypatch.setattr(bridge, '_capture_usage', lambda provider,model: metered.append((provider,model)))
    assert ensemble.record('system', 'user') == []
    bridge.call_oneshot('ordinary call')
    assert captured == ['lite', 'narrator']
    assert metered == [('vertex','lite'),('vertex','narrator')]
    assert config['vertex']['model'] == 'narrator'
