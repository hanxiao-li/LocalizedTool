"""LLM 重试逻辑：空内容/瞬时故障自动重试，永久错误不重试。

对应修复：DeepSeek 偶发返回空 content，此前一次空返回就让整批翻译失败。
"""

import pytest

import llm
from llm import LLMError


class _Resp:
    def __init__(self, status, data):
        self.status_code = status
        self._data = data

    def json(self):
        return self._data

    @property
    def text(self):
        return str(self._data)


def test_chat_raises_on_blank_content(monkeypatch):
    """chat() 对空/纯空白 content 应抛可重试的 LLMError（而非静默返回）。"""
    def fake_post(url, headers, json, timeout):
        return _Resp(200, {'choices': [{'message': {'content': '   '}}]})
    monkeypatch.setattr(llm._session, 'post', fake_post)
    with pytest.raises(LLMError) as ei:
        llm.chat('x', 'k', 'm', [{'role': 'user', 'content': 'hi'}])
    assert '空内容' in str(ei.value)


def test_chat_json_retries_on_empty_content(monkeypatch):
    """chat_json 对空内容应重试后成功。"""
    calls = {'n': 0}

    def fake_chat(base_url, api_key, model, messages, **kwargs):
        calls['n'] += 1
        if calls['n'] <= 2:
            raise LLMError('大模型返回空内容（瞬时故障或内容被拦截），请重试')
        return '[{"id": 1, "translation": "Hallo"}]'

    monkeypatch.setattr(llm, 'chat', fake_chat)
    data = llm.chat_json('x', 'k', 'm', [{'role': 'user', 'content': 'hi'}],
                         attempts=3, backoff=0)
    assert calls['n'] == 3
    assert data == [{'id': 1, 'translation': 'Hallo'}]


def test_chat_json_retries_on_json_parse_failure(monkeypatch):
    """chat_json 对 JSON 解析失败也应重试。"""
    calls = {'n': 0}

    def fake_chat(base_url, api_key, model, messages, **kwargs):
        calls['n'] += 1
        if calls['n'] == 1:
            return 'not json at all'
        return '{"ok": true}'

    monkeypatch.setattr(llm, 'chat', fake_chat)
    data = llm.chat_json('x', 'k', 'm', [{'role': 'user', 'content': 'hi'}],
                         attempts=2, backoff=0)
    assert calls['n'] == 2
    assert data == {'ok': True}


def test_chat_json_gives_up_after_attempts(monkeypatch):
    """连续空内容超过 attempts 后应抛错。"""
    def fake_chat(base_url, api_key, model, messages, **kwargs):
        raise LLMError('大模型返回空内容（瞬时故障或内容被拦截），请重试')

    monkeypatch.setattr(llm, 'chat', fake_chat)
    with pytest.raises(LLMError):
        llm.chat_json('x', 'k', 'm', [{'role': 'user', 'content': 'hi'}],
                      attempts=2, backoff=0)


def test_chat_json_json_object_first(monkeypatch):
    """json_object 优先：一次成功则不需要明文兜底。"""
    calls = []

    def fake_chat(base_url, api_key, model, messages, json_mode=False, **kwargs):
        calls.append(json_mode)
        return '[{"id": 1, "translation": "Hallo"}]'

    monkeypatch.setattr(llm, 'chat', fake_chat)
    data = llm.chat_json('x', 'k', 'm', [{'role': 'user', 'content': 'hi'}],
                         attempts=2, backoff=0)
    assert data == [{'id': 1, 'translation': 'Hallo'}]
    assert calls == [True]  # 只用 json_object，未退回明文


def test_chat_json_falls_back_to_plain(monkeypatch):
    """json_object 失败/空内容后，应退回明文兜底。"""
    calls = []

    def fake_chat(base_url, api_key, model, messages, json_mode=False, **kwargs):
        calls.append(json_mode)
        if json_mode is True:
            return 'not json at all'
        return '{"ok": true}'

    monkeypatch.setattr(llm, 'chat', fake_chat)
    data = llm.chat_json('x', 'k', 'm', [{'role': 'user', 'content': 'hi'}],
                         attempts=1, backoff=0)
    assert data == {'ok': True}
    assert calls == [True, False]


def test_chat_json_does_not_retry_permanent_errors(monkeypatch):
    """鉴权/模型不存在等永久错误不应重试。"""
    calls = {'n': 0}

    def fake_chat(base_url, api_key, model, messages, **kwargs):
        calls['n'] += 1
        raise LLMError('鉴权失败（401）：请检查 API Key / Token')

    monkeypatch.setattr(llm, 'chat', fake_chat)
    with pytest.raises(LLMError):
        llm.chat_json('x', 'k', 'm', [{'role': 'user', 'content': 'hi'}],
                      attempts=3, backoff=0)
    assert calls['n'] == 1
