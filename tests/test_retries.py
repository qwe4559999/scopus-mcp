"""Offline tests for retry behavior and the entitlement-400 translation.

All network calls are mocked; no API key or internet access required.
"""
import asyncio
import os
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from scopus_mcp.client import ScopusClient, ENTITLEMENT_NOTE

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


_REQ = httpx.Request('GET', 'https://api.elsevier.com/content/search/scopus')


def _resp(status, json_data=None, headers=None, text=''):
    if json_data is not None:
        return httpx.Response(status, json=json_data, headers=headers, request=_REQ)
    return httpx.Response(status, text=text, headers=headers, request=_REQ)


ENTITLEMENT_400_BODY = {
    'service-error': {
        'status': {
            'statusCode': 'INVALID_INPUT',
            'statusText': 'Error translating query',
        }
    }
}

SYNTAX_400_BODY = {
    'service-error': {
        'status': {
            'statusCode': 'INVALID_INPUT',
            'statusText': 'Invalid field name',
        }
    }
}


def _make_client(env=None):
    """Build a ScopusClient with env-based config and no disk cache."""
    env = {'SCOPUS_API_KEY': 'dummy', **(env or {})}
    with (
        patch.dict(os.environ, env),
        patch('scopus_mcp.client.CacheManager') as MockCache,
    ):
        MockCache.return_value.get.return_value = None
        client = ScopusClient()
    return client


# ---------------------------------------------------------------------------
# Entitlement-400 translation
# ---------------------------------------------------------------------------

def test_translation_note_appended_on_exact_signature():
    """400 with 'Error translating query' → note appended, original body kept."""
    client = _make_client()
    with patch('scopus_mcp.client.httpx.AsyncClient.request',
               new_callable=AsyncMock, return_value=_resp(400, ENTITLEMENT_400_BODY)):
        with pytest.raises(Exception) as excinfo:
            _run(client.search_scopus('ALL(gene)', count=1))
    msg = str(excinfo.value)
    assert 'Error translating query' in msg          # original message preserved
    assert 'lacks subscriber entitlement' in msg     # note appended
    assert 'SCOPUS_INSTTOKEN' in msg
    _run(client.close())


def test_translation_note_not_appended_on_other_400():
    """A different 400 body (real syntax error) must NOT get the note."""
    client = _make_client()
    with patch('scopus_mcp.client.httpx.AsyncClient.request',
               new_callable=AsyncMock, return_value=_resp(400, SYNTAX_400_BODY)):
        with pytest.raises(Exception) as excinfo:
            _run(client.search_scopus('BADFIELD(x)', count=1))
    msg = str(excinfo.value)
    assert 'Invalid field name' in msg
    assert ENTITLEMENT_NOTE not in msg
    _run(client.close())


def test_translation_note_not_appended_on_non_json_400():
    """A 400 with a non-JSON body must not crash and must not get the note."""
    client = _make_client()
    with patch('scopus_mcp.client.httpx.AsyncClient.request',
               new_callable=AsyncMock, return_value=_resp(400, text='Bad Request')):
        with pytest.raises(Exception) as excinfo:
            _run(client.search_scopus('ALL(gene)', count=1))
    assert ENTITLEMENT_NOTE not in str(excinfo.value)
    _run(client.close())


# ---------------------------------------------------------------------------
# Retry behavior
# ---------------------------------------------------------------------------

def test_retry_on_read_timeout_then_success():
    """A ReadTimeout is retried; second attempt succeeds."""
    client = _make_client()
    mock_request = AsyncMock(side_effect=[
        httpx.ReadTimeout('timed out', request=_REQ),
        _resp(200, {'ok': True}),
    ])
    with (
        patch('scopus_mcp.client.httpx.AsyncClient.request', mock_request),
        patch('asyncio.sleep', new_callable=AsyncMock),
    ):
        result = _run(client._request('GET', 'content/search/scopus',
                                      {'query': 'ALL(gene)', 'count': 1}))
    assert result == {'ok': True}
    assert mock_request.call_count == 2
    _run(client.close())


def test_read_timeout_exhausts_retries():
    """Persistent ReadTimeout fails after 1 + SCOPUS_MAX_RETRIES attempts."""
    client = _make_client()  # default max_retries=2 → 3 attempts
    mock_request = AsyncMock(side_effect=httpx.ReadTimeout('timed out', request=_REQ))
    with (
        patch('scopus_mcp.client.httpx.AsyncClient.request', mock_request),
        patch('asyncio.sleep', new_callable=AsyncMock),
    ):
        with pytest.raises(Exception) as excinfo:
            _run(client._request('GET', 'content/search/scopus'))
    assert 'Network error' in str(excinfo.value)
    assert mock_request.call_count == 3
    _run(client.close())


def test_no_retry_on_plain_400():
    """4xx (other than 429) is deterministic — exactly one attempt."""
    client = _make_client()
    mock_request = AsyncMock(return_value=_resp(400, SYNTAX_400_BODY))
    with (
        patch('scopus_mcp.client.httpx.AsyncClient.request', mock_request),
        patch('asyncio.sleep', new_callable=AsyncMock) as mock_sleep,
    ):
        with pytest.raises(Exception):
            _run(client._request('GET', 'content/search/scopus'))
    assert mock_request.call_count == 1
    mock_sleep.assert_not_called()
    _run(client.close())


def test_max_retries_zero_disables_retries():
    """SCOPUS_MAX_RETRIES=0 → a single attempt, transport error raised."""
    client = _make_client(env={'SCOPUS_MAX_RETRIES': '0'})
    assert client.max_retries == 0
    mock_request = AsyncMock(side_effect=httpx.ReadTimeout('timed out', request=_REQ))
    with (
        patch('scopus_mcp.client.httpx.AsyncClient.request', mock_request),
        patch('asyncio.sleep', new_callable=AsyncMock),
    ):
        with pytest.raises(Exception) as excinfo:
            _run(client._request('GET', 'content/search/scopus'))
    assert 'Network error' in str(excinfo.value)
    assert mock_request.call_count == 1
    _run(client.close())


def test_retry_on_5xx_then_success():
    """A 503 is retried; second attempt succeeds."""
    client = _make_client()
    mock_request = AsyncMock(side_effect=[
        _resp(503, text='Service Unavailable'),
        _resp(200, {'ok': True}),
    ])
    with (
        patch('scopus_mcp.client.httpx.AsyncClient.request', mock_request),
        patch('asyncio.sleep', new_callable=AsyncMock),
    ):
        result = _run(client._request('GET', 'content/search/scopus'))
    assert result == {'ok': True}
    assert mock_request.call_count == 2
    _run(client.close())


def test_429_honors_retry_after_capped():
    """429 with Retry-After sleeps that many seconds (capped at 10)."""
    client = _make_client()
    mock_request = AsyncMock(side_effect=[
        _resp(429, text='slow down', headers={'Retry-After': '3'}),
        _resp(200, {'ok': True}),
    ])
    with (
        patch('scopus_mcp.client.httpx.AsyncClient.request', mock_request),
        patch('asyncio.sleep', new_callable=AsyncMock) as mock_sleep,
    ):
        result = _run(client._request('GET', 'content/search/scopus'))
    assert result == {'ok': True}
    mock_sleep.assert_awaited_once_with(3.0)
    _run(client.close())


def test_retry_after_cap():
    """Retry-After above 10 s is capped at 10 s."""
    client = _make_client()
    assert client._retry_after_delay(httpx.Headers({'Retry-After': '120'})) == 10.0
    assert client._retry_after_delay(httpx.Headers({'Retry-After': '3'})) == 3.0
    assert client._retry_after_delay(httpx.Headers({})) is None
    _run(client.close())
