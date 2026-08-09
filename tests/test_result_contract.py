"""Tests for the file-output helpers and search_all server dispatch contract.

write_results_to_disk / should_write_to_disk: pure filesystem tests, no network.
Server dispatch: mock client.search_all at the module level.

The SCOPUS_API_KEY env var is set before the server module is imported so
that the module-level ScopusClient() instantiation succeeds without a config file.
"""
import csv
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

os.environ.setdefault('SCOPUS_API_KEY', 'test-key')

from scopus_mcp.utils import (
    write_results_to_disk,
    should_write_to_disk,
    SEARCH_ALL_INLINE_THRESHOLD,
)


# ---------------------------------------------------------------------------
# Helpers shared across test classes
# ---------------------------------------------------------------------------

def _make_records(n: int) -> list:
    return [
        {
            'scopus_id': str(i),
            'title': f'Paper {i}',
            'creator': 'A. Author',
            'publication_name': 'Test Journal',
            'cover_date': '2024-01-01',
            'doi': f'10.0/{i}',
            'cited_by_count': '0',
            'aggregation_type': 'Journal',
            'url': None,
        }
        for i in range(n)
    ]


def _raw_entry(n: int) -> dict:
    return {'dc:identifier': f'SCOPUS_ID:{n}', 'dc:title': f'Paper {n}'}


def _make_raw_response(n: int, total: int = None, truncated: bool = False, note: str = None) -> dict:
    """Build a fake search_all raw response with n real entries."""
    return {
        'search-results': {'entry': [_raw_entry(i) for i in range(n)]},
        '_meta': {
            'total_fetched': n,
            'total_available': total or n,
            'truncated': truncated,
            'note': note,
        },
    }


# ---------------------------------------------------------------------------
# write_results_to_disk
# ---------------------------------------------------------------------------

class TestWriteResultsToDisk(unittest.TestCase):

    def test_writes_valid_json(self):
        with tempfile.TemporaryDirectory() as td:
            with patch.dict(os.environ, {'SCOPUS_MCP_OUTPUT_DIR': td}):
                records = _make_records(3)
                paths = write_results_to_disk(records, 'TITLE(json test)')
                loaded = json.loads(Path(paths['json_path']).read_text(encoding='utf-8'))
                self.assertEqual(loaded, records)
                self.assertEqual(len(loaded), 3)

    def test_writes_valid_csv(self):
        with tempfile.TemporaryDirectory() as td:
            with patch.dict(os.environ, {'SCOPUS_MCP_OUTPUT_DIR': td}):
                records = _make_records(3)
                paths = write_results_to_disk(records, 'TITLE(csv test)')
                with open(paths['csv_path'], newline='', encoding='utf-8') as f:
                    rows = list(csv.DictReader(f))
                self.assertEqual(len(rows), 3)
                self.assertEqual(rows[0]['scopus_id'], '0')
                self.assertEqual(rows[0]['title'], 'Paper 0')

    def test_csv_has_all_expected_columns(self):
        expected = [
            'scopus_id', 'title', 'creator', 'publication_name',
            'cover_date', 'doi', 'cited_by_count', 'aggregation_type', 'url',
        ]
        with tempfile.TemporaryDirectory() as td:
            with patch.dict(os.environ, {'SCOPUS_MCP_OUTPUT_DIR': td}):
                paths = write_results_to_disk(_make_records(1), 'test')
                with open(paths['csv_path'], newline='', encoding='utf-8') as f:
                    reader = csv.DictReader(f)
                    self.assertEqual(reader.fieldnames, expected)

    def test_output_dir_created_if_missing(self):
        with tempfile.TemporaryDirectory() as td:
            new_dir = os.path.join(td, 'deep', 'nested', 'output')
            with patch.dict(os.environ, {'SCOPUS_MCP_OUTPUT_DIR': new_dir}):
                write_results_to_disk(_make_records(1), 'test')
                self.assertTrue(Path(new_dir).is_dir())

    def test_filename_contains_query_slug(self):
        with tempfile.TemporaryDirectory() as td:
            with patch.dict(os.environ, {'SCOPUS_MCP_OUTPUT_DIR': td}):
                paths = write_results_to_disk(_make_records(1), 'machine learning health')
                self.assertIn('machine-learning-health', Path(paths['json_path']).name)

    def test_returns_both_paths(self):
        with tempfile.TemporaryDirectory() as td:
            with patch.dict(os.environ, {'SCOPUS_MCP_OUTPUT_DIR': td}):
                paths = write_results_to_disk(_make_records(1), 'test')
                self.assertIn('json_path', paths)
                self.assertIn('csv_path', paths)
                self.assertTrue(Path(paths['json_path']).exists())
                self.assertTrue(Path(paths['csv_path']).exists())


# ---------------------------------------------------------------------------
# should_write_to_disk
# ---------------------------------------------------------------------------

class TestShouldWriteToDisk(unittest.TestCase):

    def test_above_threshold_returns_true(self):
        self.assertTrue(should_write_to_disk(_make_records(SEARCH_ALL_INLINE_THRESHOLD + 1)))

    def test_at_threshold_returns_false(self):
        self.assertFalse(should_write_to_disk(_make_records(SEARCH_ALL_INLINE_THRESHOLD)))

    def test_below_threshold_returns_false(self):
        self.assertFalse(should_write_to_disk(_make_records(SEARCH_ALL_INLINE_THRESHOLD - 1)))

    def test_custom_threshold(self):
        records = _make_records(5)
        self.assertTrue(should_write_to_disk(records, threshold=3))
        self.assertFalse(should_write_to_disk(records, threshold=5))

    def test_empty_list_returns_false(self):
        self.assertFalse(should_write_to_disk([]))


# ---------------------------------------------------------------------------
# Server dispatch: search_all inline vs. file-output behaviour
# ---------------------------------------------------------------------------

class TestServerSearchAllDispatch(unittest.IsolatedAsyncioTestCase):

    async def _dispatch(self, raw_response: dict, query: str = 'test', tmp_dir: str = None):
        """Call handle_call_tool('search_all', ...) with mocked client."""
        env = {'SCOPUS_MCP_OUTPUT_DIR': tmp_dir} if tmp_dir else {}
        with patch('scopus_mcp.server.client') as mock_client, \
             patch.dict(os.environ, env):
            mock_client.search_all = AsyncMock(return_value=raw_response)
            # Import here so the module-level client is already initialised.
            from scopus_mcp.server import handle_call_tool
            return await handle_call_tool('search_all', {'query': query, 'max_results': 200})

    # -- small result: inline --

    async def test_small_result_returns_inline(self):
        with tempfile.TemporaryDirectory() as td:
            raw = _make_raw_response(10)
            result = await self._dispatch(raw, tmp_dir=td)
            text = result[0].text
            self.assertIn('Paper 0', text)
            self.assertNotIn('JSON:', text)
            self.assertNotIn('CSV:', text)

    async def test_small_result_note_appended(self):
        note = 'Result set capped: fetched 10 of 500 total (max_results=10).'
        with tempfile.TemporaryDirectory() as td:
            raw = _make_raw_response(10, total=500, truncated=True, note=note)
            result = await self._dispatch(raw, tmp_dir=td)
            self.assertIn(note, result[0].text)

    async def test_small_result_no_note_when_not_truncated(self):
        with tempfile.TemporaryDirectory() as td:
            raw = _make_raw_response(10)
            result = await self._dispatch(raw, tmp_dir=td)
            self.assertNotIn('Note:', result[0].text)

    # -- large result: file output --

    async def test_large_result_writes_files_and_returns_summary(self):
        n = SEARCH_ALL_INLINE_THRESHOLD + 10
        with tempfile.TemporaryDirectory() as td:
            raw = _make_raw_response(n)
            result = await self._dispatch(raw, tmp_dir=td)
            text = result[0].text
            self.assertIn('JSON:', text)
            self.assertIn('CSV:', text)
            self.assertIn(f'Fetched {n}', text)

    async def test_large_result_does_not_dump_all_records_inline(self):
        n = SEARCH_ALL_INLINE_THRESHOLD + 10
        with tempfile.TemporaryDirectory() as td:
            raw = _make_raw_response(n)
            result = await self._dispatch(raw, tmp_dir=td)
            # Records beyond the first 10 should NOT appear inline
            self.assertNotIn(f'Paper {n - 1}', result[0].text)

    async def test_large_result_sample_contains_first_10(self):
        n = SEARCH_ALL_INLINE_THRESHOLD + 10
        with tempfile.TemporaryDirectory() as td:
            raw = _make_raw_response(n)
            result = await self._dispatch(raw, tmp_dir=td)
            self.assertIn('Paper 0', result[0].text)

    async def test_large_result_files_are_valid_json_and_csv(self):
        n = SEARCH_ALL_INLINE_THRESHOLD + 5
        with tempfile.TemporaryDirectory() as td:
            raw = _make_raw_response(n)
            result = await self._dispatch(raw, tmp_dir=td)
            text = result[0].text
            # Extract paths from summary text
            json_path = next(
                line.strip().removeprefix('JSON:').strip()
                for line in text.splitlines() if 'JSON:' in line
            )
            csv_path = next(
                line.strip().removeprefix('CSV:').strip()
                for line in text.splitlines() if 'CSV:' in line
            )
            loaded_json = json.loads(Path(json_path).read_text(encoding='utf-8'))
            self.assertEqual(len(loaded_json), n)
            with open(csv_path, newline='', encoding='utf-8') as f:
                rows = list(csv.DictReader(f))
            self.assertEqual(len(rows), n)

    async def test_large_result_note_appended(self):
        n = SEARCH_ALL_INLINE_THRESHOLD + 5
        note = f'Result set capped: fetched {n} of 5000 total (max_results={n}).'
        with tempfile.TemporaryDirectory() as td:
            raw = _make_raw_response(n, total=5000, truncated=True, note=note)
            result = await self._dispatch(raw, tmp_dir=td)
            self.assertIn(note, result[0].text)

    # -- empty result (Bug 1 integration) --

    async def test_empty_result_returns_cleanly(self):
        """Zero-hit response produces '[]', not a phantom record."""
        raw = {
            'search-results': {
                'opensearch:totalResults': '0',
                'entry': [{'error': 'Result set was empty'}],
            },
            '_meta': {
                'total_fetched': 0,
                'total_available': 0,
                'truncated': False,
                'note': None,
            },
        }
        with tempfile.TemporaryDirectory() as td:
            result = await self._dispatch(raw, tmp_dir=td)
            self.assertEqual(result[0].text.strip(), '[]')
