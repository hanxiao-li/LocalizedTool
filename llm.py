"""OpenAI-compatible LLM client.

Users configure base_url + api_key + model (any OpenAI-compatible endpoint:
DeepSeek, OpenAI, 通义千问, 智谱, Ollama, ...). Everything is done with
`requests` so no extra SDK dependency is needed.

`base_url` conventions:
  * may end with `/chat/completions` -> used directly
  * otherwise `/chat/completions` is appended (e.g. `https://api.openai.com/v1`)
"""

import json
import logging
import re
import time

import requests

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 90          # 读超时（秒）：给足大批次生成时间；挂死由有界重试兜底
CONNECT_TIMEOUT = 10          # 连接超时（秒）
USER_AGENT = 'LocalizedTool/1.0'

# 复用 TCP/TLS 连接的会话：多批次/多语种并发时省去每次握手开销
_session = requests.Session()


# ── URL helpers ─────────────────────────────────────────────────────────

def _normalize_base_url(base_url: str) -> str:
    """Strip and ensure a scheme prefix; empty stays empty."""
    base_url = (base_url or '').strip()
    if base_url and not base_url.lower().startswith(('http://', 'https://')):
        base_url = 'https://' + base_url
    return base_url


def _completions_url(base_url: str) -> str:
    base_url = _normalize_base_url(base_url).rstrip('/')
    if base_url.endswith('/chat/completions'):
        return base_url
    return base_url + '/chat/completions'


def _models_url(base_url: str) -> str:
    base_url = _normalize_base_url(base_url).rstrip('/')
    if base_url.endswith('/chat/completions'):
        base_url = base_url.rsplit('/chat/completions', 1)[0]
    return base_url.rstrip('/') + '/models'


# ── JSON extraction ─────────────────────────────────────────────────────

def extract_json(text: str):
    """Best-effort JSON parse of a model reply.

    Tries, in order: direct parse, fenced ```json``` block, first balanced
    `{...}` object, first balanced `[...]` array. Returns the parsed value
    or raises ValueError.
    """
    if not text:
        raise ValueError('模型返回为空')
    text = text.strip()

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # Strip code fences
    fenced = re.search(r'```(?:json)?\s*(.*?)\s*```', text, re.S)
    if fenced:
        try:
            return json.loads(fenced.group(1))
        except json.JSONDecodeError:
            pass

    def _balanced(start, open_ch, close_ch):
        depth = 0
        in_str = False
        esc = False
        for i in range(start, len(text)):
            c = text[i]
            if in_str:
                if esc:
                    esc = False
                elif c == '\\':
                    esc = True
                elif c == '"':
                    in_str = False
                continue
            if c == '"':
                in_str = True
            elif c == open_ch:
                depth += 1
            elif c == close_ch:
                depth -= 1
                if depth == 0:
                    return i + 1
        return -1

    for open_ch, close_ch in (('{', '}'), ('[', ']')):
        start = text.find(open_ch)
        if start == -1:
            continue
        end = _balanced(start, open_ch, close_ch)
        if end > 0:
            try:
                return json.loads(text[start:end])
            except json.JSONDecodeError:
                continue

    raise ValueError('无法从模型回复中解析JSON: %s' % text[:200])


# ── Chat ────────────────────────────────────────────────────────────────

class LLMError(Exception):
    """Raised for any chat/connection failure with a user-readable message."""


def chat(base_url: str, api_key: str, model: str, messages: list,
         temperature: float = 0.3, max_tokens: int = 2048,
         timeout: int = DEFAULT_TIMEOUT, json_mode: bool = False) -> str:
    """Send a chat completion request and return the assistant text.

    Raises LLMError with a readable message on any failure.
    """
    url = _completions_url(base_url)
    headers = {
        'User-Agent': USER_AGENT,
        'Content-Type': 'application/json',
    }
    if api_key:
        headers['Authorization'] = f'Bearer {api_key}'

    payload = {
        'model': model,
        'messages': messages,
        'temperature': temperature,
        'max_tokens': max_tokens,
        'stream': False,
    }
    if json_mode:
        payload['response_format'] = {'type': 'json_object'}

    logger.info('LLM request -> %s (model=%s, %d messages)', url, model, len(messages))
    try:
        resp = _session.post(url, headers=headers, json=payload,
                             timeout=(CONNECT_TIMEOUT, timeout))
    except requests.exceptions.Timeout:
        raise LLMError(f'请求大模型超时（{timeout}s）')
    except requests.exceptions.ConnectionError:
        raise LLMError(f'无法连接大模型地址: {url}')
    except requests.exceptions.RequestException as e:
        raise LLMError(f'请求大模型失败: {e}')

    if resp.status_code == 401:
        raise LLMError('鉴权失败（401）：请检查 API Key / Token')
    if resp.status_code == 404:
        raise LLMError('地址或模型不存在（404）：请检查 base_url 与模型名称')
    if resp.status_code == 429:
        raise LLMError('请求过于频繁（429）：请稍后再试')
    if resp.status_code >= 400:
        snippet = resp.text[:300]
        raise LLMError(f'大模型返回错误（{resp.status_code}）: {snippet}')

    try:
        data = resp.json()
    except json.JSONDecodeError:
        raise LLMError('大模型返回了无法解析的内容')

    try:
        content = data['choices'][0]['message']['content']
    except (KeyError, IndexError, TypeError):
        raise LLMError('大模型响应缺少 content，响应: %s' % str(data)[:300])

    # 空/纯空白 content 视为瞬时故障，由上层重试（DeepSeek 偶发返回空字符串）
    if content is None or not str(content).strip():
        raise LLMError('大模型返回空内容（瞬时故障或内容被拦截），请重试')
    return content


# ── Connection test ─────────────────────────────────────────────────────

def test_connection(base_url: str, api_key: str, model: str) -> tuple[bool, str]:
    """Verify the endpoint + credentials. Returns (ok, message).

    Tries GET /models first, then a minimal chat call as a fallback.
    """
    # 1. Standard OpenAI-compatible: GET /models
    try:
        headers = {'User-Agent': USER_AGENT}
        if api_key:
            headers['Authorization'] = f'Bearer {api_key}'
        resp = _session.get(_models_url(base_url), headers=headers, timeout=15)
        if resp.status_code == 200:
            data = resp.json()
            models = data.get('data') if isinstance(data, dict) else None
            if isinstance(models, list):
                names = {m.get('id') for m in models if isinstance(m, dict)}
                if model and names and model not in names:
                    hint = '、'.join(sorted(names)[:5])
                    return (True, f'连接成功。注意：该端点可用的模型中未找到「{model}」，可用示例: {hint}')
                return (True, '连接成功，地址与密钥有效')
            return (True, '连接成功，地址与密钥有效')
        if resp.status_code == 401:
            return (False, '鉴权失败（401）：请检查 API Key / Token')
        if resp.status_code == 404:
            return (False, '地址不存在（404）：请检查 base_url 是否正确')
        # fall through to a chat probe
    except requests.exceptions.RequestException:
        pass

    # 2. Minimal chat probe
    try:
        reply = chat(
            base_url, api_key, model,
            [{'role': 'user', 'content': '请只回复两个字：成功'}],
            max_tokens=16, timeout=30,
        )
        return (True, f'连接成功，模型可正常对话（回复: {reply[:30]}）')
    except LLMError as e:
        return (False, f'连接失败: {e}')


# ── Retry helper ────────────────────────────────────────────────────────

def chat_with_retry(base_url: str, api_key: str, model: str, messages: list,
                    attempts: int = 3, backoff: float = 1.5, **kwargs) -> str:
    """Chat with retries on transient failures (429 / timeout / connection /
    empty content). Permanent errors (auth / model-not-found) never retry."""
    last_err = None
    for i in range(attempts):
        try:
            return chat(base_url, api_key, model, messages, **kwargs)
        except LLMError as e:
            last_err = e
            if any(k in str(e) for k in ('鉴权失败', '地址或模型不存在')):
                raise  # permanent, do not retry
            if i < attempts - 1:
                time.sleep(backoff * (i + 1))
    raise last_err


# ── JSON chat helper ────────────────────────────────────────────────────

def chat_json(base_url: str, api_key: str, model: str, messages: list,
              attempts: int = 2, backoff: float = 1.0, **kwargs):
    """Chat then parse the reply as JSON.

    json_object 优先（实测对 deepseek-v4-flash 最可靠，始终返回有效 JSON；
    明文模式会偶发空返回且耗时更长）。空/失败再退回明文兜底。
    每格式最多 attempts 次；永久错误不重试。
    """
    last_err = None
    for json_mode in (True, False):
        for i in range(attempts):
            try:
                text = chat(base_url, api_key, model, messages,
                            json_mode=json_mode, **kwargs)
                return extract_json(text)
            except LLMError as e:
                last_err = e
                if any(k in str(e) for k in ('鉴权失败', '地址或模型不存在')):
                    raise  # permanent, do not retry
                if i < attempts - 1:
                    time.sleep(backoff * (i + 1))
            except ValueError as e:
                # 空内容 / JSON 解析失败 → 瞬时问题，重试（必要时换格式）
                last_err = e
                if i < attempts - 1:
                    time.sleep(backoff * (i + 1))
    raise last_err
