"""Tests for ScopusClient.search_all — no live API calls.

Mocking strategy:
- start-based paging: patch search_scopus on the instance
- cursor-based paging: patch _request on the instance

Paging tests pin the page size to the shipped default (25) by patching
get_page_size, so a stray SCOPUS_PAGE_SIZE in the environment cannot change
what they assert.  get_page_size itself is covered separately below.
"""
import asyncio
import os
import unittest
from unittest.mock import patch

from scopus_mcp.client import ScopusClient
from scopus_mcp.config import get_page_size

PAGE_SIZE = 25

# Any test whose mock server would page forever on a regression trips this
# first, so a broken guard fails the suite instead of hanging it.
RUNAWAY_CALLS = 2000


def _entry(n: int) -> dict:
    return {'dc:identifier': f'SCOPUS_ID:{n}', 'dc:title': f'Paper {n}'}


def _search_resp(entries: list, total: int = 0, next_cursor: str | None = None) -> dict:
    sr: dict = {
        'entry': entries,
        'opensearch:totalResults': str(total or len(entries)),
    }
    if next_cursor is not None:
        sr['cursor'] = {'@current': 'c0', '@next': next_cursor}
    return {'search-results': sr}


class TestPageSizeConfig(unittest.TestCase):
    """config.get_page_size — the knob search_all pages with."""

    def _get(self, env: dict, file_config: dict | None = None):
        with patch('scopus_mcp.config.load_config_file', return_value=file_config or {}), \
             patch.dict(os.environ, env, clear=False):
            os.environ.pop('SCOPUS_PAGE_SIZE', None)
            os.environ.update(env)
            return get_page_size()

    def test_default_is_25(self):
        """25 is the per-request 'count' ceiling for non-institutional keys.

        Regression guard: PAGE_SIZE=200 previously caused a silent 400
        INVALID_INPUT that swallowed forward citation_lineage results.
        """
        self.assertEqual(self._get({}), 25)

    def test_env_var_overrides_default(self):
        self.assertEqual(self._get({'SCOPUS_PAGE_SIZE': '200'}), 200)

    def test_config_file_overrides_default(self):
        self.assertEqual(self._get({}, {'page_size': 100}), 100)

    def test_env_var_wins_over_config_file(self):
        self.assertEqual(self._get({'SCOPUS_PAGE_SIZE': '50'}, {'page_size': 100}), 50)

    def test_clamped_to_api_maximum(self):
        self.assertEqual(self._get({'SCOPUS_PAGE_SIZE': '5000'}), 200)

    def test_clamped_to_at_least_one(self):
        self.assertEqual(self._get({'SCOPUS_PAGE_SIZE': '0'}), 1)

    def test_non_numeric_falls_back_to_default(self):
        self.assertEqual(self._get({'SCOPUS_PAGE_SIZE': 'lots'}), 25)


class TestSearchAll(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        self.config_patcher = patch('scopus_mcp.client.get_api_key', return_value='fake_key')
        self.config_patcher.start()
        self.page_size_patcher = patch('scopus_mcp.client.get_page_size', return_value=PAGE_SIZE)
        self.page_size_patcher.start()
        self.cache_patcher = patch('scopus_mcp.client.CacheManager')
        MockCache = self.cache_patcher.start()
        MockCache.return_value.get.return_value = None
        self.client = ScopusClient()

    async def asyncTearDown(self):
        self.config_patcher.stop()
        self.page_size_patcher.stop()
        self.cache_patcher.stop()
        await self.client.close()

    # ------------------------------------------------------------------
    # Page size plumbing
    # ------------------------------------------------------------------

    async def test_page_size_comes_from_config(self):
        """The client pages with the configured size, not a hard-coded constant."""
        self.assertEqual(self.client.page_size, PAGE_SIZE)

        self.client.page_size = 200
        counts = []

        async def fake_search(query, count, start, sort):
            counts.append(count)
            return _search_resp([_entry(i) for i in range(count)], total=1000)

        self.client.search_scopus = fake_search
        await self.client.search_all('TITLE(test)', max_results=400)

        self.assertEqual(counts, [200, 200])

    # ------------------------------------------------------------------
    # Start-based paging
    # ------------------------------------------------------------------

    async def test_aggregation_across_multiple_pages(self):
        """Two full pages then one partial page are merged into a single list."""
        corpus = [_entry(i) for i in range(60)]
        call_args = []

        async def fake_search(query, count, start, sort):
            call_args.append((start, count))
            return _search_resp(corpus[start:start + count], total=60)

        self.client.search_scopus = fake_search
        result = await self.client.search_all('TITLE(test)', max_results=60)

        entries = result['search-results']['entry']
        self.assertEqual(len(entries), 60)
        self.assertEqual(call_args, [(0, 25), (25, 25), (50, 10)])
        self.assertFalse(result['_meta']['truncated'])

    async def test_stops_at_max_results(self):
        """Stops paging once max_results entries have been collected."""
        call_count = 0

        async def fake_search(query, count, start, sort):
            nonlocal call_count
            call_count += 1
            self.assertLess(call_count, RUNAWAY_CALLS, "search_all did not terminate")
            # A full page every time, so paging would be unbounded without the
            # max_results guard.
            return _search_resp(
                [_entry(i) for i in range(start, start + count)], total=5000
            )

        self.client.search_scopus = fake_search
        result = await self.client.search_all('TITLE(test)', max_results=100)

        self.assertEqual(len(result['search-results']['entry']), 100)
        self.assertEqual(call_count, 4)  # 100 / 25

    async def test_stops_when_results_exhausted_early(self):
        """Stops cleanly when API returns fewer results than the page size."""
        sparse = [_entry(i) for i in range(10)]

        async def fake_search(query, count, start, sort):
            if start == 0:
                return _search_resp(sparse, total=10)
            raise AssertionError("unexpected second page request")

        self.client.search_scopus = fake_search
        result = await self.client.search_all('TITLE(test)', max_results=200)

        self.assertEqual(len(result['search-results']['entry']), 10)
        self.assertFalse(result['_meta']['truncated'])

    async def test_deduplication_across_pages(self):
        """Duplicate dc:identifier values across pages produce a single entry."""
        page1 = [_entry(i) for i in range(25)]                       # full page
        page2 = [_entry(0)] + [_entry(i) for i in range(25, 33)]     # 9 entries, 1 dupe

        async def fake_search(query, count, start, sort):
            return _search_resp(page1 if start == 0 else page2, total=34)

        self.client.search_scopus = fake_search
        result = await self.client.search_all('TITLE(test)', max_results=50)

        ids = [e['dc:identifier'] for e in result['search-results']['entry']]
        self.assertEqual(len(ids), len(set(ids)), "duplicates found")
        self.assertEqual(len(ids), 33)  # 25 unique from page1 + 8 unique from page2

    async def test_stops_when_page_adds_only_duplicates(self):
        """A full page of already-seen records ends start-based paging."""
        page = [_entry(i) for i in range(25)]
        call_count = 0

        async def fake_search(query, count, start, sort):
            nonlocal call_count
            call_count += 1
            self.assertLess(call_count, RUNAWAY_CALLS, "search_all did not terminate")
            return _search_resp(page, total=5000)

        self.client.search_scopus = fake_search
        result = await self.client.search_all('TITLE(test)', max_results=500)

        self.assertEqual(len(result['search-results']['entry']), 25)
        self.assertEqual(call_count, 2)  # first page, then one all-duplicate page

    async def test_page_cap_stops_start_paging_that_never_progresses(self):
        """A server dribbling one new record per full page stops at the page cap."""
        call_count = 0

        async def fake_search(query, count, start, sort):
            nonlocal call_count
            call_count += 1
            self.assertLess(call_count, RUNAWAY_CALLS, "search_all did not terminate")
            # One new record per page; the other 24 are always the same.
            fresh = _entry(10_000 + call_count)
            return _search_resp([fresh] + [_entry(i) for i in range(24)], total=100_000)

        self.client.search_scopus = fake_search
        result = await self.client.search_all('TITLE(test)', max_results=100)

        meta = result['_meta']
        expected_cap = (100 // PAGE_SIZE) * 2 + 10  # ceil(100/25)*2 + 10
        self.assertTrue(meta['hit_page_cap'])
        self.assertEqual(meta['pages_fetched'], expected_cap)
        self.assertEqual(call_count, expected_cap)
        self.assertIn('safety cap', meta['note'])

    async def test_truncation_note_when_capped_by_max_results(self):
        """Note is set when max_results < total_available."""

        async def fake_search(query, count, start, sort):
            return _search_resp(
                [_entry(i) for i in range(start, start + count)], total=1000
            )

        self.client.search_scopus = fake_search
        result = await self.client.search_all('TITLE(test)', max_results=50)

        meta = result['_meta']
        self.assertTrue(meta['truncated'])
        self.assertFalse(meta['hit_page_cap'])
        self.assertIn('50', meta['note'])
        self.assertIn('1000', meta['note'])

    # ------------------------------------------------------------------
    # Cursor-based paging (max_results > 5000)
    # ------------------------------------------------------------------

    async def test_cursor_paging_used_when_max_results_exceeds_5000(self):
        """_request is called with cursor=* when max_results > 5000."""
        page = [_entry(i) for i in range(25)]

        request_params = []

        async def fake_request(method, endpoint, params=None, use_cache=True, ttl=None):
            request_params.append(dict(params or {}))
            return _search_resp(page, total=25)  # no cursor block — single page

        self.client._request = fake_request
        await self.client.search_all('TITLE(test)', max_results=5001)

        self.assertTrue(len(request_params) >= 1)
        first = request_params[0]
        self.assertIn('cursor', first)
        self.assertEqual(first['cursor'], '*')
        self.assertNotIn('start', first)

    async def test_cursor_paging_follows_next_cursor(self):
        """Subsequent cursor pages use the @next cursor from the previous response."""
        page1 = [_entry(i) for i in range(25)]
        page2 = [_entry(i) for i in range(25, 40)]

        call_cursors = []

        async def fake_request(method, endpoint, params=None, use_cache=True, ttl=None):
            c = (params or {}).get('cursor')
            call_cursors.append(c)
            if c == '*':
                return _search_resp(page1, total=40, next_cursor='c1')
            return _search_resp(page2, total=40)  # short, last page

        self.client._request = fake_request
        result = await self.client.search_all('TITLE(test)', max_results=5001)

        self.assertEqual(call_cursors, ['*', 'c1'])
        self.assertEqual(len(result['search-results']['entry']), 40)

    async def test_cursor_paging_stops_when_exhausted_early(self):
        """Cursor paging stops cleanly when a page is shorter than requested."""
        sparse = [_entry(i) for i in range(10)]
        call_count = 0

        async def fake_request(method, endpoint, params=None, use_cache=True, ttl=None):
            nonlocal call_count
            call_count += 1
            self.assertLess(call_count, RUNAWAY_CALLS, "search_all did not terminate")
            await asyncio.sleep(0)
            return _search_resp(sparse, total=10, next_cursor='c1')

        self.client._request = fake_request
        result = await self.client.search_all('TITLE(test)', max_results=5001)

        self.assertEqual(len(result['search-results']['entry']), 10)
        self.assertEqual(call_count, 1)

    async def test_cursor_paging_stops_when_next_cursor_does_not_advance(self):
        """Regression: a full batch plus a frozen @next cursor must not loop forever.

        The API hands back a complete page every time and keeps pointing at the
        same @next value.  Nothing else in the loop can break — the page is not
        short and each page carries records — so termination rests entirely on
        the cursor-advance guard.
        """
        call_count = 0

        async def fake_request(method, endpoint, params=None, use_cache=True, ttl=None):
            nonlocal call_count
            call_count += 1
            self.assertLess(call_count, RUNAWAY_CALLS, "search_all did not terminate")
            await asyncio.sleep(0)
            base = call_count * 1000  # fresh records every page, so `added` stays > 0
            return _search_resp(
                [_entry(base + i) for i in range(PAGE_SIZE)],
                total=100_000,
                next_cursor='frozen',
            )

        self.client._request = fake_request
        result = await asyncio.wait_for(
            self.client.search_all('TITLE(test)', max_results=5001), timeout=30
        )

        # '*' -> 'frozen' advances once; the second page repeats 'frozen' and stops.
        self.assertEqual(call_count, 2)
        self.assertEqual(len(result['search-results']['entry']), 2 * PAGE_SIZE)
        self.assertEqual(result['_meta']['pages_fetched'], 2)

    async def test_cursor_paging_stops_when_page_is_all_duplicates(self):
        """An advancing cursor that keeps returning the same records still stops."""
        page = [_entry(i) for i in range(PAGE_SIZE)]
        call_count = 0

        async def fake_request(method, endpoint, params=None, use_cache=True, ttl=None):
            nonlocal call_count
            call_count += 1
            self.assertLess(call_count, RUNAWAY_CALLS, "search_all did not terminate")
            await asyncio.sleep(0)
            return _search_resp(page, total=100_000, next_cursor=f'c{call_count}')

        self.client._request = fake_request
        result = await asyncio.wait_for(
            self.client.search_all('TITLE(test)', max_results=5001), timeout=30
        )

        self.assertEqual(call_count, 2)  # first page, then one all-duplicate page
        self.assertEqual(len(result['search-results']['entry']), PAGE_SIZE)

    async def test_page_cap_stops_cursor_paging_that_never_progresses(self):
        """Slow-but-nonzero progress is bounded by the page cap, not by max_results."""
        call_count = 0

        async def fake_request(method, endpoint, params=None, use_cache=True, ttl=None):
            nonlocal call_count
            call_count += 1
            self.assertLess(call_count, RUNAWAY_CALLS, "search_all did not terminate")
            await asyncio.sleep(0)
            # One new record per page, cursor advancing — only the cap can stop this.
            fresh = _entry(10_000 + call_count)
            return _search_resp(
                [fresh] + [_entry(i) for i in range(PAGE_SIZE - 1)],
                total=100_000,
                next_cursor=f'c{call_count}',
            )

        self.client._request = fake_request
        result = await asyncio.wait_for(
            self.client.search_all('TITLE(test)', max_results=5001), timeout=60
        )

        meta = result['_meta']
        expected_cap = 201 * 2 + 10  # ceil(5001/25)*2 + 10
        self.assertTrue(meta['hit_page_cap'])
        self.assertEqual(meta['pages_fetched'], expected_cap)
        self.assertEqual(call_count, expected_cap)
        self.assertIn('safety cap', meta['note'])
