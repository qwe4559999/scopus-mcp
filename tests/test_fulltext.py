"""Offline tests for get_fulltext waterfall.

All network calls are mocked; no API key or internet access required.
"""
import asyncio
import json
import os
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _json_result(result):
    return json.loads(result[0].text)


# ---------------------------------------------------------------------------
# Fake response data
# ---------------------------------------------------------------------------

FULL_SD_RESPONSE = {
    'full-text-retrieval-response': {
        'originalText': 'A' * 600,  # >500 chars → treated as full text
        'coredata': {'dc:title': 'Test Paper'},
    }
}

ABSTRACT_ONLY_SD_RESPONSE = {
    'full-text-retrieval-response': {
        'originalText': 'Short abstract.',  # <500 chars → abstract-only
        'coredata': {'dc:title': 'Test Paper'},
    }
}

ABSTRACT_RESPONSE = {
    'abstracts-retrieval-response': {
        'coredata': {
            'dc:identifier': 'SCOPUS_ID:123',
            'dc:description': 'The abstract text of the paper.',
            'dc:title': 'Test Paper',
            'prism:doi': '10.1234/test',
        },
        'authors': {'author': []},
    }
}

OA_OPENALEX_PDF = {
    'open_access': {'is_oa': True, 'oa_url': 'https://example.com/paper.pdf'},
    'best_oa_location': {'pdf_url': 'https://example.com/paper.pdf', 'url': 'https://example.com/paper.pdf'},
}

OA_OPENALEX_HTML = {
    'open_access': {'is_oa': True, 'oa_url': 'https://example.com/paper.html'},
    'best_oa_location': {'pdf_url': None, 'url': 'https://example.com/paper.html'},
}

# ---------------------------------------------------------------------------
# Fake httpx async client helper
# ---------------------------------------------------------------------------

def _make_fake_httpx_ctx(side_effect_fn):
    """Return a MagicMock that acts as `async with httpx.AsyncClient() as c:`."""
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=ctx)
    ctx.__aexit__ = AsyncMock(return_value=False)
    ctx.get = AsyncMock(side_effect=side_effect_fn)
    return ctx


def _fake_http_resp(status=200, json_data=None, content=b'', text='', headers=None):
    r = MagicMock()
    r.status_code = status
    r.json.return_value = json_data or {}
    r.content = content
    r.text = text
    r.headers = headers or {}
    r.raise_for_status = MagicMock()
    return r


# ---------------------------------------------------------------------------
# Tier 1: ScienceDirect full text
# ---------------------------------------------------------------------------

def test_sd_fulltext_returns_provenance_and_writes_disk(tmp_path):
    """ScienceDirect returns >500-char originalText → provenance sciencedirect-fulltext, written to disk."""
    with (
        patch.dict(os.environ, {'SCOPUS_API_KEY': 'dummy', 'SCOPUS_MCP_OUTPUT_DIR': str(tmp_path)}),
        patch('scopus_mcp.server.client') as mock_client,
        patch('scopus_mcp.server.fetch_oa_fulltext', new_callable=AsyncMock) as mock_oa,
    ):
        mock_client.get_sciencedirect_fulltext = AsyncMock(return_value=FULL_SD_RESPONSE)
        mock_oa.return_value = {'text': None, 'source_url': None}

        from scopus_mcp.server import handle_call_tool
        result = _run(handle_call_tool('get_fulltext', {'doi': '10.1234/test'}))
        data = _json_result(result)

        assert data['provenance'] == 'sciencedirect-fulltext'
        assert data['char_count'] == 600
        assert data['doi'] == '10.1234/test'
        assert 'file_path' in data
        written = Path(data['file_path']).read_text()
        assert written == 'A' * 600


def test_sd_abstract_only_falls_through(tmp_path):
    """ScienceDirect returns short originalText → falls through to OA/abstract tier."""
    with (
        patch.dict(os.environ, {'SCOPUS_API_KEY': 'dummy', 'SCOPUS_MCP_OUTPUT_DIR': str(tmp_path)}),
        patch('scopus_mcp.server.client') as mock_client,
        patch('scopus_mcp.server.fetch_oa_fulltext', new_callable=AsyncMock) as mock_oa,
    ):
        mock_client.get_sciencedirect_fulltext = AsyncMock(return_value=ABSTRACT_ONLY_SD_RESPONSE)
        mock_oa.return_value = {'text': None, 'source_url': None}
        mock_client.get_abstract_by = AsyncMock(return_value=ABSTRACT_RESPONSE)

        from scopus_mcp.server import handle_call_tool
        result = _run(handle_call_tool('get_fulltext', {'doi': '10.1234/test'}))
        data = _json_result(result)

        assert data['provenance'] in ('oa-fulltext', 'scopus-abstract')


def test_sd_none_falls_through_to_abstract(tmp_path):
    """ScienceDirect returns None (403) → falls through to abstract."""
    with (
        patch.dict(os.environ, {'SCOPUS_API_KEY': 'dummy', 'SCOPUS_MCP_OUTPUT_DIR': str(tmp_path)}),
        patch('scopus_mcp.server.client') as mock_client,
        patch('scopus_mcp.server.fetch_oa_fulltext', new_callable=AsyncMock) as mock_oa,
    ):
        mock_client.get_sciencedirect_fulltext = AsyncMock(return_value=None)
        mock_oa.return_value = {'text': None, 'source_url': None}
        mock_client.get_abstract_by = AsyncMock(return_value=ABSTRACT_RESPONSE)

        from scopus_mcp.server import handle_call_tool
        result = _run(handle_call_tool('get_fulltext', {'doi': '10.1234/test'}))
        data = _json_result(result)
        assert data['provenance'] in ('oa-fulltext', 'scopus-abstract')


# ---------------------------------------------------------------------------
# Tier 2: OA full text – PDF
# ---------------------------------------------------------------------------

def test_oa_pdf_path(tmp_path):
    """OA path: PDF fetch → pymupdf extraction → provenance oa-fulltext, written to disk."""
    pdf_text = 'C' * 4000

    def fake_get(url, **kwargs):
        if 'openalex' in url:
            return _fake_http_resp(json_data=OA_OPENALEX_PDF)
        return _fake_http_resp(content=b'%PDF', headers={'content-type': 'application/pdf'})

    with (
        patch.dict(os.environ, {'SCOPUS_API_KEY': 'dummy', 'SCOPUS_MCP_OUTPUT_DIR': str(tmp_path)}),
        patch('scopus_mcp.server.client') as mock_client,
        patch('scopus_mcp.utils._extract_pdf_text', return_value=pdf_text),
        patch('scopus_mcp.utils.httpx.AsyncClient', return_value=_make_fake_httpx_ctx(fake_get)),
    ):
        mock_client.get_sciencedirect_fulltext = AsyncMock(return_value=None)

        from scopus_mcp.server import handle_call_tool
        result = _run(handle_call_tool('get_fulltext', {'doi': '10.1234/test'}))
        data = _json_result(result)

        assert data['provenance'] == 'oa-fulltext'
        assert data['char_count'] == 4000
        assert 'file_path' in data
        assert data['source_url'] == 'https://example.com/paper.pdf'


def test_oa_html_path(tmp_path):
    """OA path: HTML fetch → HTML extraction → provenance oa-fulltext."""
    extracted = 'D' * 4000

    def fake_get(url, **kwargs):
        if 'openalex' in url:
            return _fake_http_resp(json_data=OA_OPENALEX_HTML)
        return _fake_http_resp(text='<html><body>content</body></html>',
                               headers={'content-type': 'text/html'})

    with (
        patch.dict(os.environ, {'SCOPUS_API_KEY': 'dummy', 'SCOPUS_MCP_OUTPUT_DIR': str(tmp_path)}),
        patch('scopus_mcp.server.client') as mock_client,
        patch('scopus_mcp.utils._extract_html_text', return_value=extracted),
        patch('scopus_mcp.utils.httpx.AsyncClient', return_value=_make_fake_httpx_ctx(fake_get)),
    ):
        mock_client.get_sciencedirect_fulltext = AsyncMock(return_value=None)

        from scopus_mcp.server import handle_call_tool
        result = _run(handle_call_tool('get_fulltext', {'doi': '10.1234/test'}))
        data = _json_result(result)

        assert data['provenance'] == 'oa-fulltext'
        assert data['char_count'] == 4000
        assert data['source_url'] == 'https://example.com/paper.html'


def test_oa_fetch_failure_falls_through_to_abstract(tmp_path):
    """OA URL found but text extraction fails → falls through to abstract."""
    with (
        patch.dict(os.environ, {'SCOPUS_API_KEY': 'dummy', 'SCOPUS_MCP_OUTPUT_DIR': str(tmp_path)}),
        patch('scopus_mcp.server.client') as mock_client,
        patch('scopus_mcp.server.fetch_oa_fulltext', new_callable=AsyncMock) as mock_oa,
    ):
        mock_client.get_sciencedirect_fulltext = AsyncMock(return_value=None)
        mock_oa.return_value = {'text': None, 'source_url': 'https://example.com/paper.pdf'}
        mock_client.get_abstract_by = AsyncMock(return_value=ABSTRACT_RESPONSE)

        from scopus_mcp.server import handle_call_tool
        result = _run(handle_call_tool('get_fulltext', {'doi': '10.1234/test'}))
        data = _json_result(result)

        assert data['provenance'] == 'scopus-abstract'
        assert data['source_url'] == 'https://example.com/paper.pdf'


# ---------------------------------------------------------------------------
# Tier 3: Abstract fallback
# ---------------------------------------------------------------------------

def test_abstract_fallback(tmp_path):
    """All retrieval fails except Scopus abstract → provenance scopus-abstract."""
    with (
        patch.dict(os.environ, {'SCOPUS_API_KEY': 'dummy', 'SCOPUS_MCP_OUTPUT_DIR': str(tmp_path)}),
        patch('scopus_mcp.server.client') as mock_client,
        patch('scopus_mcp.server.fetch_oa_fulltext', new_callable=AsyncMock) as mock_oa,
    ):
        mock_client.get_sciencedirect_fulltext = AsyncMock(return_value=None)
        mock_oa.return_value = {'text': None, 'source_url': None}
        mock_client.get_abstract_by = AsyncMock(return_value=ABSTRACT_RESPONSE)

        from scopus_mcp.server import handle_call_tool
        result = _run(handle_call_tool('get_fulltext', {'doi': '10.1234/test'}))
        data = _json_result(result)

        assert data['provenance'] == 'scopus-abstract'
        assert 'abstract text' in data['sample']


# ---------------------------------------------------------------------------
# Tier 4: Nothing resolves
# ---------------------------------------------------------------------------

def test_nothing_resolves_returns_none_provenance(tmp_path):
    """When all tiers fail → provenance none, no crash."""
    with (
        patch.dict(os.environ, {'SCOPUS_API_KEY': 'dummy', 'SCOPUS_MCP_OUTPUT_DIR': str(tmp_path)}),
        patch('scopus_mcp.server.client') as mock_client,
        patch('scopus_mcp.server.fetch_oa_fulltext', new_callable=AsyncMock) as mock_oa,
    ):
        mock_client.get_sciencedirect_fulltext = AsyncMock(return_value=None)
        mock_oa.return_value = {'text': None, 'source_url': None}
        mock_client.get_abstract_by = AsyncMock(side_effect=Exception("API error"))

        from scopus_mcp.server import handle_call_tool
        result = _run(handle_call_tool('get_fulltext', {'doi': '10.1234/test'}))
        data = _json_result(result)

        assert data['provenance'] == 'none'
        assert data['char_count'] == 0
        assert 'file_path' not in data


# ---------------------------------------------------------------------------
# Insttoken header tests
# ---------------------------------------------------------------------------

def test_insttoken_header_sent_when_set():
    """When SCOPUS_INSTTOKEN is set, X-ELS-Insttoken must appear in client.headers."""
    with patch.dict(os.environ, {'SCOPUS_API_KEY': 'dummy', 'SCOPUS_INSTTOKEN': 'mytoken123'}):
        import importlib
        import scopus_mcp.config as cfg_mod
        import scopus_mcp.client as client_mod

        importlib.reload(cfg_mod)
        importlib.reload(client_mod)

        c = client_mod.ScopusClient()
        assert 'X-ELS-Insttoken' in c.headers
        assert c.headers['X-ELS-Insttoken'] == 'mytoken123'
        asyncio.new_event_loop().run_until_complete(c.close())


def test_insttoken_header_absent_when_not_set():
    """When SCOPUS_INSTTOKEN is not set, X-ELS-Insttoken must NOT be in client.headers."""
    env = {k: v for k, v in os.environ.items() if k != 'SCOPUS_INSTTOKEN'}
    env['SCOPUS_API_KEY'] = 'dummy'
    with patch.dict(os.environ, env, clear=True):
        import importlib
        import scopus_mcp.config as cfg_mod
        import scopus_mcp.client as client_mod

        importlib.reload(cfg_mod)
        importlib.reload(client_mod)

        c = client_mod.ScopusClient()
        assert 'X-ELS-Insttoken' not in c.headers
        asyncio.new_event_loop().run_until_complete(c.close())
