"""Provider wire contract that prevented the live recall agent from advancing."""
import json
from types import SimpleNamespace

import pytest

from story_core import llm_bridge, vertex_bridge, gemini_bridge
from story_core.card_mode.recall import ACTION_SCHEMA


@pytest.mark.parametrize('provider', ['vertex', 'gemini'])
def test_bridge_forwards_schema_and_captures_failed_usage(provider, monkeypatch):
    bridge = vertex_bridge if provider == 'vertex' else gemini_bridge
    monkeypatch.setattr(llm_bridge, '_get_config', lambda: {'provider': provider, provider: {'model': 'test'}})
    monkeypatch.setattr(llm_bridge, '_provider_override', None)
    def failed(prompt, **kwargs):
        assert kwargs['response_schema'] == ACTION_SCHEMA
        bridge._tls.last_usage = {'prompt_tokens': 10, 'output_tokens': 0, 'total_tokens': 10}
        raise ValueError('MALFORMED_FUNCTION_CALL')
    monkeypatch.setattr(bridge, 'call_'+provider+'_oneshot', failed)
    with pytest.raises(ValueError, match='MALFORMED_FUNCTION_CALL'):
        llm_bridge.call_oneshot('query', response_schema=ACTION_SCHEMA)
    assert llm_bridge.get_last_usage()['total_tokens'] == 10
    assert llm_bridge.get_last_usage()['provider'] == provider


@pytest.mark.parametrize('reason,text', [('STOP', '{"action":"read","id":"m0"}'),
                                        ('MALFORMED_FUNCTION_CALL', None),
                                        ('MAX_TOKENS', '{"action":"finish"}')])
def test_vertex_structured_output_sets_wire_schema_and_checks_finish(monkeypatch, reason, text):
    def generate(**kwargs):
        config = kwargs['config']
        assert config.response_mime_type == 'application/json'
        assert config.response_json_schema == ACTION_SCHEMA
        assert not config.tools
        assert config.tool_config.function_calling_config.mode == 'NONE'
        return SimpleNamespace(text=text, candidates=[SimpleNamespace(finish_reason=reason)],
                               usage_metadata=SimpleNamespace(prompt_token_count=10, candidates_token_count=2, total_token_count=12))
    monkeypatch.setattr(vertex_bridge, '_get_client', lambda _: SimpleNamespace(models=SimpleNamespace(generate_content=generate)))
    if reason == 'STOP':
        assert vertex_bridge.call_vertex_oneshot('query', {}, response_schema=ACTION_SCHEMA) == text
    else:
        with pytest.raises(ValueError, match=reason):
            vertex_bridge.call_vertex_oneshot('query', {}, response_schema=ACTION_SCHEMA)
        assert vertex_bridge.get_last_usage()['total_tokens'] == 12


def test_gemini_wire_schema(monkeypatch):
    class Response:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def read(self):
            return json.dumps({'candidates': [{'content': {'parts': [{'text': '{"action":"finish"}'}]},
                                               'finishReason': 'STOP'}]}).encode()
    def open_request(req, **kwargs):
        config = json.loads(req.data)['generationConfig']
        assert config['responseMimeType'] == 'application/json'
        assert config['responseJsonSchema'] == ACTION_SCHEMA
        return Response()
    monkeypatch.setattr(gemini_bridge.urllib.request, 'urlopen', open_request)
    monkeypatch.setattr(gemini_bridge, '_with_key_fallback', lambda cfg, fn: (fn('test'), None))
    assert gemini_bridge.call_gemini_oneshot('query', {}, response_schema=ACTION_SCHEMA) == '{"action":"finish"}'


@pytest.mark.parametrize('code,retryable', [(500, True), (429, True), (400, False)])
def test_vertex_distinguishes_temporary_service_errors(monkeypatch, code, retryable):
    from story_core.llm_errors import TransientStructuredError
    class ProviderError(Exception): pass
    error=ProviderError('request failed');error.code=code
    def generate(**kwargs): raise error
    monkeypatch.setattr(vertex_bridge, '_get_client', lambda _: SimpleNamespace(models=SimpleNamespace(generate_content=generate)))
    with pytest.raises(ValueError) as caught:
        vertex_bridge.call_vertex_oneshot('query', {}, response_schema=ACTION_SCHEMA)
    assert isinstance(caught.value, TransientStructuredError) == retryable
