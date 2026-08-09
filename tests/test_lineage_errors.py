"""Tests for error-surfacing in citation_lineage (feat/lineage-error-surfacing).

All tests are offline — client methods are mocked throughout.
Covers:
  - Auth error on node fetch → summary reports failure, NOT "no papers found"
  - All fetches fail → summary clearly states walk failed due to API errors
  - Mixed success/failure → summary reports both found papers and skipped nodes
  - Genuine empty result → still reports "no papers found" (distinguishable)
  - 429 retry: mock returning 429-then-200 retries and succeeds
  - Persistent auth error: mock returning 401 does NOT retry indefinitely
"""
import os
import tempfile
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import httpx

os.environ.setdefault('SCOPUS_API_KEY', 'test-key')


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_abstract_raw(sid, title='Test Paper', year='2020', venue='Test Journal',
                       cbc='100'):
    return {
        'abstracts-retrieval-response': {
            'coredata': {
                'dc:identifier': f'SCOPUS_ID:{sid}',
                'dc:title': title,
                'prism:coverDate': f'{year}-01-01',
                'prism:publicationName': venue,
                'citedby-count': cbc,
                'prism:doi': f'10.1/{sid}',
                'link': [],
            },
            'authors': {'author': [{'ce:indexed-name': 'Doe J.', '@auid': '1'}]},
        }
    }


def _make_coupling_ref_raw(seed_id, ref_ids):
    refs = [
        {
            '@id': str(i),
            'scopus-id': rid,
            'title': f'Reference {i}',
            'sourcetitle': 'Some Journal',
            'prism:coverDate': '2020-01-01',
            'author-list': {'author': [{'ce:indexed-name': 'Ref A.', '@auid': f'a{i}'}]},
        }
        for i, rid in enumerate(ref_ids)
    ]
    return {
        'abstracts-retrieval-response': {
            'coredata': {
                'dc:identifier': f'SCOPUS_ID:{seed_id}',
                'dc:title': 'Seed Paper',
                'prism:coverDate': '2020-01-01',
                'prism:publicationName': 'Test Journal',
                'link': [],
            },
            'authors': {'author': [{'ce:indexed-name': 'Author A.', '@auid': '111'}]},
            'references': {'reference': refs},
        }
    }


async def _dispatch(args, tmp_dir, get_abstract_fn, get_refs_fn=None, search_all_fn=None):
    with patch('scopus_mcp.server.client') as mock_client, \
         patch.dict(os.environ, {'SCOPUS_MCP_OUTPUT_DIR': tmp_dir}):
        mock_client.get_abstract = AsyncMock(side_effect=get_abstract_fn)
        if get_refs_fn is not None:
            mock_client.get_references = AsyncMock(side_effect=get_refs_fn)
        if search_all_fn is not None:
            mock_client.search_all = AsyncMock(side_effect=search_all_fn)
        from scopus_mcp.server import handle_call_tool
        return await handle_call_tool('citation_lineage', args)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestLineageErrorSurfacing(unittest.IsolatedAsyncioTestCase):

    async def test_auth_error_surfaced_not_empty(self):
        """A node fetch that raises an auth error must report the failure, not 'no papers found'."""
        async def get_refs_fail(sid):
            raise Exception("REF-view fetch failed: Invalid API Key — likely a REF-view entitlement or quota limit")

        with tempfile.TemporaryDirectory() as td:
            result = await _dispatch(
                {'seed_id': 'seed1', 'direction': 'backward', 'generations': 1},
                td,
                lambda sid: _make_abstract_raw(sid),
                get_refs_fn=get_refs_fail,
            )
        text = result[0].text
        assert 'no papers found' not in text, (
            f"Should not report 'no papers found' when API error occurred. Got:\n{text}"
        )
        assert 'REF-view' in text or 'API error' in text or 'FAILED' in text, (
            f"Should mention the failure reason. Got:\n{text}"
        )

    async def test_all_fetches_fail_reports_walk_failed(self):
        """When every node fetch fails, the summary must state the walk failed due to API errors."""
        async def get_refs_fail(sid):
            raise Exception("REF-view fetch failed: Invalid API Key — likely a REF-view entitlement or quota limit, not a bad key")

        with tempfile.TemporaryDirectory() as td:
            result = await _dispatch(
                {'seed_id': 'seed1', 'direction': 'backward', 'generations': 1},
                td,
                lambda sid: _make_abstract_raw(sid),
                get_refs_fn=get_refs_fail,
            )
        text = result[0].text
        # Must report failure, not empty walk
        assert 'FAILED' in text or 'failed' in text.lower(), (
            f"Summary must say the walk failed. Got:\n{text}"
        )
        assert 'no papers found' not in text, (
            f"Must not report 'no papers found' when all fetches errored. Got:\n{text}"
        )
        assert 'REF-view' in text or 'Invalid API Key' in text, (
            f"Error message must be included in summary. Got:\n{text}"
        )

    async def test_partial_failure_reports_both_found_and_skipped(self):
        """When some fetches succeed and some fail, report both found papers and skipped count."""
        call_count = [0]

        async def get_refs_mixed(sid):
            call_count[0] += 1
            if call_count[0] == 1:
                # First call (seed node) succeeds — returns 2 refs
                return _make_coupling_ref_raw(sid, ['ref1', 'ref2'])
            # Second call (first ref node in gen 2) fails
            raise Exception("REF-view fetch failed: quota exceeded")

        with tempfile.TemporaryDirectory() as td:
            result = await _dispatch(
                {'seed_id': 'seed1', 'direction': 'backward', 'generations': 2},
                td,
                lambda sid: _make_abstract_raw(sid),
                get_refs_fn=get_refs_mixed,
            )
        text = result[0].text
        # Should have found papers (gen 1: 2)
        assert 'gen 1: 2' in text, f"Should report gen 1 papers. Got:\n{text}"
        # Should warn about the failed fetch
        assert 'Warning' in text or 'failed' in text.lower(), (
            f"Should warn about partial failure. Got:\n{text}"
        )
        assert 'no papers found' not in text, (
            f"Should not say 'no papers found' when some were found. Got:\n{text}"
        )

    async def test_genuine_empty_still_reports_no_papers_found(self):
        """A genuinely empty result (fetch succeeds, returns zero items) reports 'no papers found'."""
        async def get_refs_empty(sid):
            return _make_coupling_ref_raw(sid, [])  # valid response, zero references

        with tempfile.TemporaryDirectory() as td:
            result = await _dispatch(
                {'seed_id': 'seed1', 'direction': 'backward', 'generations': 1},
                td,
                lambda sid: _make_abstract_raw(sid),
                get_refs_fn=get_refs_empty,
            )
        text = result[0].text
        assert 'no papers found' in text, (
            f"Genuine empty result should report 'no papers found'. Got:\n{text}"
        )
        # Must NOT say the walk failed — it succeeded, just found nothing
        assert 'FAILED' not in text and 'API error' not in text, (
            f"Genuine empty result should not claim an error. Got:\n{text}"
        )


class TestRetryAndAuthBehavior(unittest.IsolatedAsyncioTestCase):

    def _make_client(self):
        with patch('scopus_mcp.client.get_api_key', return_value='fake_key'), \
             patch('scopus_mcp.client.get_insttoken', return_value=None), \
             patch('scopus_mcp.client.get_cache_config',
                   return_value={'default': 3600, 'search': 1800, 'abstract': 7200}), \
             patch('scopus_mcp.client.CacheManager'):
            from scopus_mcp.client import ScopusClient
            return ScopusClient()

    async def test_429_retries_and_succeeds(self):
        """A 429 followed by a 200 should retry and return the successful result."""
        client = self._make_client()

        response_429 = MagicMock()
        response_429.status_code = 429
        response_429.headers = {'X-RateLimit-Reset': '0', 'X-RateLimit-Remaining': '0'}

        response_200 = MagicMock()
        response_200.status_code = 200
        response_200.json.return_value = {'ok': True}
        response_200.headers = {'X-RateLimit-Remaining': '10'}

        with patch.object(client.client, 'request',
                          side_effect=[response_429, response_200]) as mock_req, \
             patch('asyncio.sleep', new_callable=AsyncMock):
            result = await client._request('GET', 'test/endpoint', use_cache=False)

        assert result == {'ok': True}, f"Should succeed after retry. Got: {result}"
        assert mock_req.call_count == 2, "Should have made exactly 2 requests"

        await client.close()

    async def test_persistent_auth_error_does_not_retry(self):
        """A persistent 401 must raise immediately without retrying."""
        client = self._make_client()

        mock_response = MagicMock()
        mock_response.status_code = 401
        mock_response.headers = {'X-ELS-Status': 'AUTHENTICATION_ERROR'}
        http_error = httpx.HTTPStatusError(
            "401 Unauthorized", request=MagicMock(), response=mock_response
        )
        mock_response.raise_for_status.side_effect = http_error

        with patch.object(client.client, 'request',
                          side_effect=[mock_response]) as mock_req, \
             patch('asyncio.sleep', new_callable=AsyncMock) as mock_sleep:
            with self.assertRaises(Exception) as ctx:
                await client._request('GET', 'test/endpoint', use_cache=False)

        # Must not retry — only 1 request made
        assert mock_req.call_count == 1, (
            f"Auth error must not trigger retries. Got {mock_req.call_count} calls."
        )
        assert mock_sleep.call_count == 0, "Must not sleep on auth error"
        assert 'Invalid API Key' in str(ctx.exception) or 'Authentication' in str(ctx.exception)

        await client.close()

    async def test_ref_view_401_annotates_error_message(self):
        """A 401 on a REF-view endpoint should include the entitlement annotation."""
        client = self._make_client()

        mock_response = MagicMock()
        mock_response.status_code = 401
        mock_response.headers = {'X-ELS-Status': 'AUTHENTICATION_ERROR', 'X-RateLimit-Remaining': '0'}
        http_error = httpx.HTTPStatusError(
            "401 Unauthorized", request=MagicMock(), response=mock_response
        )
        mock_response.raise_for_status.side_effect = http_error

        with patch.object(client.client, 'request', side_effect=[mock_response]), \
             patch('asyncio.sleep', new_callable=AsyncMock):
            with self.assertRaises(Exception) as ctx:
                await client._request(
                    'GET', 'content/abstract/scopus_id/123',
                    params={'view': 'REF'}, use_cache=False
                )

        err = str(ctx.exception)
        assert 'REF-view' in err, f"Error should mention REF-view. Got: {err}"
        assert 'entitlement' in err or 'quota' in err, (
            f"Error should note entitlement/quota possibility. Got: {err}"
        )

        await client.close()


if __name__ == '__main__':
    unittest.main()
