"""Offline tests for the diagnose_connection tool's classification logic.

All network calls are mocked; no API key or internet access required.
"""
import asyncio
import os
from unittest.mock import AsyncMock, MagicMock, patch

import httpx

from scopus_mcp.client import ScopusClient, CANARY_SCOPUS_ID

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


ENTITLEMENT_400_MSG = (
    "Scopus API error 400 for https://api.elsevier.com/content/search/scopus "
    "(query='ALL(gene)'): {\"service-error\":{\"status\":{\"statusCode\":"
    "\"INVALID_INPUT\",\"statusText\":\"Error translating query\"}}}"
)
AUTH_FAILED_MSG = "Authentication failed: Invalid API Key"
NETWORK_MSG = (
    "Network error contacting Scopus API for content/search/scopus "
    "after 3 attempt(s): ReadTimeout: timed out"
)


def _make_client(env=None):
    env = {'SCOPUS_API_KEY': 'dummy', **(env or {})}
    with (
        patch.dict(os.environ, env),
        patch('scopus_mcp.client.CacheManager') as MockCache,
    ):
        MockCache.return_value.get.return_value = None
        client = ScopusClient()
    return client


def _diagnose(client, request_side_effect):
    """Run diagnose_connection with mocked reachability and _request."""
    writer = MagicMock()
    writer.close = MagicMock()
    with (
        patch.dict(os.environ, {'SCOPUS_API_KEY': 'dummy'}),
        patch('asyncio.open_connection', new_callable=AsyncMock,
              return_value=(MagicMock(), writer)),
        patch.object(client.client, 'get', new_callable=AsyncMock,
                     return_value=httpx.Response(
                         200, request=httpx.Request('GET', 'https://api.elsevier.com/'))),
        patch.object(client, '_request', new_callable=AsyncMock,
                     side_effect=request_side_effect),
    ):
        return _run(client.diagnose_connection())


def _abstract_then_search(abstract_result, search_result):
    """Side-effect fn: first canary abstract call, then canary search call."""
    def side_effect(method, endpoint, *args, **kwargs):
        outcome = abstract_result if CANARY_SCOPUS_ID in endpoint else search_result
        if isinstance(outcome, Exception):
            raise outcome
        return outcome
    return side_effect


# ---------------------------------------------------------------------------
# Verdict branches
# ---------------------------------------------------------------------------

def test_all_ok_verdict():
    client = _make_client()
    report = _diagnose(client, _abstract_then_search({'ok': True}, {'ok': True}))
    assert report['config']['api_key_present'] is True
    assert report['reachability']['reachable'] is True
    assert report['metadata']['status'] == 'ok'
    assert report['search']['status'] == 'ok'
    assert report['verdict'] == 'Connection and entitlement healthy.'
    _run(client.close())


def test_entitlement_missing_verdict():
    """Metadata ok + search 400-translating-error → off-network verdict."""
    client = _make_client()
    report = _diagnose(client, _abstract_then_search(
        {'ok': True}, Exception(ENTITLEMENT_400_MSG)))
    assert report['metadata']['status'] == 'ok'
    assert report['search']['status'] == 'entitlement_missing'
    assert "off your institution's network" in report['verdict']
    assert 'SCOPUS_INSTTOKEN' in report['verdict']
    _run(client.close())


def test_auth_failed_verdict():
    client = _make_client()
    report = _diagnose(client, _abstract_then_search(
        Exception(AUTH_FAILED_MSG), Exception(AUTH_FAILED_MSG)))
    assert report['metadata']['status'] == 'auth_failed'
    assert report['search']['status'] == 'auth_failed'
    assert report['verdict'].startswith('API key rejected')
    _run(client.close())


def test_network_error_classification():
    client = _make_client()
    report = _diagnose(client, _abstract_then_search(
        {'ok': True}, Exception(NETWORK_MSG)))
    assert report['search']['status'] == 'network'
    _run(client.close())


def test_unknown_error_classification_includes_snippet():
    client = _make_client()
    long_msg = 'Scopus API error 500 for x: ' + 'z' * 500
    report = _diagnose(client, _abstract_then_search({'ok': True}, Exception(long_msg)))
    assert report['search']['status'] == 'error'
    assert report['search']['detail'] == long_msg[:300]
    _run(client.close())


def test_config_reports_presence_not_values():
    client = _make_client(env={'SCOPUS_INSTTOKEN': 'secret-token-value'})
    with patch.dict(os.environ, {'SCOPUS_API_KEY': 'dummy',
                                 'SCOPUS_INSTTOKEN': 'secret-token-value'}):
        report = _diagnose(client, _abstract_then_search({'ok': True}, {'ok': True}))
    assert report['config']['insttoken_present'] is True
    assert 'secret-token-value' not in str(report)
    assert 'dummy' not in str(report)
    _run(client.close())


def test_unreachable_verdict():
    """Transport error on the reachability GET → reachable false."""
    client = _make_client()
    with (
        patch('asyncio.open_connection', new_callable=AsyncMock,
              side_effect=OSError('unreachable')),
        patch.object(client.client, 'get', new_callable=AsyncMock,
                     side_effect=httpx.ConnectError('unreachable')),
        patch.object(client, '_request', new_callable=AsyncMock,
                     side_effect=_abstract_then_search(
                         Exception(NETWORK_MSG), Exception(NETWORK_MSG))),
    ):
        report = _run(client.diagnose_connection())
    assert report['reachability']['reachable'] is False
    assert 'not reachable' in report['verdict']
    _run(client.close())


def test_degraded_verdict_appended_on_slow_connect():
    """Connect latency above 2 s appends the degraded-path warning."""
    client = _make_client()
    report = {
        'config': {'api_key_present': True, 'insttoken_present': False},
        'reachability': {'reachable': True, 'connect_seconds': 3.2,
                         'total_seconds': 4.0},
        'metadata': {'status': 'ok'},
        'search': {'status': 'ok'},
    }
    verdict = ScopusClient._build_verdict(report)
    assert verdict.startswith('Connection and entitlement healthy.')
    assert 'degraded' in verdict
    _run(client.close())
