"""Tests for feat/lineage-mainpath-render:
  - compute_main_path: SPC weights and main-path extraction on hand-built graphs
  - render_lineage_html: file written, D3 script tag present, data inlined
  - render_lineage_png: file written, failure isolated
  - citation_lineage dispatch: JSON contains main_path + spc_edges, summary has HTML/PNG paths

No live API calls — client methods are mocked throughout.
"""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

os.environ.setdefault('SCOPUS_API_KEY', 'test-key')

from scopus_mcp.utils import (
    compute_main_path,
    render_lineage_html,
    render_lineage_png,
)

# ---------------------------------------------------------------------------
# Graph fixtures
# ---------------------------------------------------------------------------

def _make_records_graph():
    """Return the 6-node graph from Hummon & Doreian (1989) for SPC verification.

    Topology (edges as parent → child):
      1 → 2, 1 → 3
      2 → 4, 3 → 4, 3 → 5
      4 → 6, 5 → 6

    Hand-computed SPC:
      spc_f: 1=1, 2=1, 3=1, 4=2, 5=1, 6=3
      spc_b: 6=1, 5=1, 4=1, 3=2, 2=1, 1=3

    Edge weights (spc_f[u] * spc_b[v]):
      1→2 = 1*1=1, 1→3 = 1*2=2
      2→4 = 1*1=1, 3→4 = 1*1=1, 3→5 = 1*1=1
      4→6 = 2*1=2, 5→6 = 1*1=1

    Main path (greedy, pick max-weight edge):
      1→3 (weight 2), then 3→{4 or 5} (tied at 1), then →6.
    """
    return [
        {'scopus_id': '1', 'doi': None, 'parents': [],        'generation': 0,
         'title': 'Root', 'year': '2000', 'venue': 'J', 'cited_by_count': '3'},
        {'scopus_id': '2', 'doi': None, 'parents': ['1'],     'generation': 1,
         'title': 'Node2', 'year': '2001', 'venue': 'J', 'cited_by_count': '1'},
        {'scopus_id': '3', 'doi': None, 'parents': ['1'],     'generation': 1,
         'title': 'Node3', 'year': '2001', 'venue': 'J', 'cited_by_count': '2'},
        {'scopus_id': '4', 'doi': None, 'parents': ['2', '3'],'generation': 2,
         'title': 'Node4', 'year': '2002', 'venue': 'J', 'cited_by_count': '2'},
        {'scopus_id': '5', 'doi': None, 'parents': ['3'],     'generation': 2,
         'title': 'Node5', 'year': '2002', 'venue': 'J', 'cited_by_count': '1'},
        {'scopus_id': '6', 'doi': None, 'parents': ['4', '5'],'generation': 3,
         'title': 'Leaf', 'year': '2003', 'venue': 'J', 'cited_by_count': '0'},
    ]


# ---------------------------------------------------------------------------
# Part 1: compute_main_path
# ---------------------------------------------------------------------------

class TestComputeMainPath(unittest.TestCase):

    def setUp(self):
        self.records = _make_records_graph()
        self.result = compute_main_path(self.records)

    def test_returns_required_keys(self):
        assert 'edges' in self.result
        assert 'main_path' in self.result
        assert 'note' in self.result

    def test_high_spc_edges_identified(self):
        """Edges 1→3 and 4→6 must have weight 2; lower edges must have weight 1."""
        ew = {(e['source'], e['target']): e['spc_weight'] for e in self.result['edges']}
        assert ew[('1', '3')] == 2, f"1→3 expected weight 2, got {ew.get(('1','3'))}"
        assert ew[('4', '6')] == 2, f"4→6 expected weight 2, got {ew.get(('4','6'))}"
        assert ew[('1', '2')] == 1, f"1→2 expected weight 1, got {ew.get(('1','2'))}"
        assert ew[('5', '6')] == 1, f"5→6 expected weight 1, got {ew.get(('5','6'))}"

    def test_all_seven_edges_present(self):
        assert len(self.result['edges']) == 7

    def test_main_path_starts_at_source(self):
        assert self.result['main_path'][0] == '1'

    def test_main_path_ends_at_sink(self):
        assert self.result['main_path'][-1] == '6'

    def test_main_path_traverses_high_spc_chain(self):
        """Greedy selection must pick 1→3 (weight 2) over 1→2 (weight 1)."""
        mp = self.result['main_path']
        assert '3' in mp, f"Node 3 must be in main path (high SPC edge 1→3=2); got {mp}"

    def test_main_path_length(self):
        mp = self.result['main_path']
        assert len(mp) == 4, f"Expected path length 4 (1→3→{{4 or 5}}→6), got {mp}"

    def test_single_node_no_crash(self):
        """Single seed record with no parents: returns empty main path, no exception."""
        r = compute_main_path([{
            'scopus_id': 'seed', 'doi': None, 'parents': [],
            'generation': 0, 'title': 'S', 'year': '2000',
            'venue': 'J', 'cited_by_count': '5',
        }])
        assert r['main_path'] == []
        assert r['note'] is not None  # explains why it's empty

    def test_single_generation_no_crash(self):
        """Seed + gen-1 papers with no edges between gen-1 nodes."""
        records = [
            {'scopus_id': 'seed', 'doi': None, 'parents': [],      'generation': 0, 'title': 'S', 'year': '2020', 'venue': 'J', 'cited_by_count': '10'},
            {'scopus_id': 'c1',   'doi': None, 'parents': ['seed'], 'generation': 1, 'title': 'C1','year': '2021', 'venue': 'J', 'cited_by_count': '5'},
            {'scopus_id': 'c2',   'doi': None, 'parents': ['seed'], 'generation': 1, 'title': 'C2','year': '2021', 'venue': 'J', 'cited_by_count': '3'},
        ]
        r = compute_main_path(records)
        assert isinstance(r['main_path'], list)
        assert r['main_path'][0] == 'seed'

    def test_disconnected_records_no_crash(self):
        """Records with no parent links produce empty path without crashing."""
        records = [
            {'scopus_id': 'a', 'doi': None, 'parents': [], 'generation': 0, 'title': 'A', 'year': '2020', 'venue': 'J', 'cited_by_count': '0'},
            {'scopus_id': 'b', 'doi': None, 'parents': [], 'generation': 0, 'title': 'B', 'year': '2020', 'venue': 'J', 'cited_by_count': '0'},
        ]
        r = compute_main_path(records)
        assert isinstance(r['main_path'], list)

    def test_doi_keyed_node_handled(self):
        """Records with only doi (no scopus_id) must be included without crash."""
        records = [
            {'scopus_id': 'seed', 'doi': None,         'parents': [],       'generation': 0, 'title': 'S', 'year': '2020', 'venue': 'J', 'cited_by_count': '5'},
            {'scopus_id': None,   'doi': '10.1/child', 'parents': ['seed'], 'generation': 1, 'title': 'C', 'year': '2021', 'venue': 'J', 'cited_by_count': '2'},
        ]
        r = compute_main_path(records)
        assert isinstance(r['main_path'], list)


# ---------------------------------------------------------------------------
# Part 2: render_lineage_html
# ---------------------------------------------------------------------------

class TestRenderLineageHtml(unittest.TestCase):

    def _records(self):
        return _make_records_graph()

    def test_writes_html_file(self):
        with tempfile.TemporaryDirectory() as td:
            with patch.dict(os.environ, {'SCOPUS_MCP_OUTPUT_DIR': td}):
                path = render_lineage_html(self._records(), ['1', '3', '4', '6'], 'seed1', 'test-lin')
            assert path is not None
            assert Path(path).exists()

    def test_file_is_nontrivial_size(self):
        with tempfile.TemporaryDirectory() as td:
            with patch.dict(os.environ, {'SCOPUS_MCP_OUTPUT_DIR': td}):
                path = render_lineage_html(self._records(), ['1', '3', '4', '6'], 'seed1', 'test-lin')
            assert Path(path).stat().st_size > 4000, "HTML file suspiciously small"

    def test_contains_d3_script_tag(self):
        with tempfile.TemporaryDirectory() as td:
            with patch.dict(os.environ, {'SCOPUS_MCP_OUTPUT_DIR': td}):
                path = render_lineage_html(self._records(), ['1', '3', '4', '6'], 'seed1', 'test-lin')
            content = Path(path).read_text(encoding='utf-8')
        assert 'd3' in content.lower()
        assert '<script src=' in content or '<script src="' in content

    def test_contains_inlined_data(self):
        with tempfile.TemporaryDirectory() as td:
            with patch.dict(os.environ, {'SCOPUS_MCP_OUTPUT_DIR': td}):
                path = render_lineage_html(self._records(), ['1', '3', '4', '6'], 'seed1', 'test-lin')
            content = Path(path).read_text(encoding='utf-8')
        # The data literal should contain node ids and a nodes key
        assert '"nodes"' in content
        assert '"seed_id"' in content
        assert 'seed1' in content

    def test_main_path_ids_appear_in_data(self):
        main_path = ['1', '3', '4', '6']
        with tempfile.TemporaryDirectory() as td:
            with patch.dict(os.environ, {'SCOPUS_MCP_OUTPUT_DIR': td}):
                path = render_lineage_html(self._records(), main_path, 'seed1', 'test-lin')
            content = Path(path).read_text(encoding='utf-8')
        # main_path array should appear in the inlined DATA
        assert '"main_path"' in content

    def test_render_failure_returns_none(self):
        """If output dir is unwriteable, render_lineage_html must return None, not raise."""
        with patch('scopus_mcp.utils._output_dir', side_effect=OSError("disk full")):
            result = render_lineage_html(self._records(), [], 'seed1', 'test-lin')
        assert result is None


# ---------------------------------------------------------------------------
# Part 3: render_lineage_png
# ---------------------------------------------------------------------------

class TestRenderLineagePng(unittest.TestCase):

    def _records(self):
        return _make_records_graph()

    def test_writes_png_file(self):
        with tempfile.TemporaryDirectory() as td:
            with patch.dict(os.environ, {'SCOPUS_MCP_OUTPUT_DIR': td}):
                path = render_lineage_png(self._records(), ['1', '3', '4', '6'], 'seed1', 'test-lin')
            assert path is not None
            assert Path(path).exists()
            assert path.endswith('.png')

    def test_png_nontrivial_size(self):
        with tempfile.TemporaryDirectory() as td:
            with patch.dict(os.environ, {'SCOPUS_MCP_OUTPUT_DIR': td}):
                path = render_lineage_png(self._records(), ['1', '3', '4', '6'], 'seed1', 'test-lin')
            assert Path(path).stat().st_size > 5000

    def test_render_failure_returns_none(self):
        with patch('scopus_mcp.utils._output_dir', side_effect=OSError("disk full")):
            result = render_lineage_png(self._records(), [], 'seed1', 'test-lin')
        assert result is None

    def test_empty_records_returns_none(self):
        with tempfile.TemporaryDirectory() as td:
            with patch.dict(os.environ, {'SCOPUS_MCP_OUTPUT_DIR': td}):
                result = render_lineage_png([], [], 'seed1', 'test-empty')
        assert result is None


# ---------------------------------------------------------------------------
# Part 4: citation_lineage dispatch integration
# ---------------------------------------------------------------------------

def _make_abstract_raw(sid, title='Test Paper', year='2020', cbc='100'):
    return {
        'abstracts-retrieval-response': {
            'coredata': {
                'dc:identifier': f'SCOPUS_ID:{sid}',
                'dc:title': title,
                'prism:coverDate': f'{year}-01-01',
                'prism:publicationName': 'Test Journal',
                'citedby-count': cbc,
                'prism:doi': f'10.1/{sid}',
                'link': [],
            },
            'authors': {'author': [{'ce:indexed-name': 'Doe J.', '@auid': '1'}]},
        }
    }


def _make_search_raw(items):
    entries = [
        {
            'dc:identifier': f'SCOPUS_ID:{it["scopus_id"]}',
            'dc:title': it.get('title', f'Paper {it["scopus_id"]}'),
            'prism:coverDate': it.get('year', '2021') + '-01-01',
            'prism:publicationName': 'J',
            'citedby-count': str(it.get('cbc', 15)),
            'prism:doi': f'10.1/{it["scopus_id"]}',
            'link': [],
        }
        for it in items
    ]
    return {
        'search-results': {'entry': entries},
        '_meta': {'total_fetched': len(entries), 'total_available': len(entries),
                  'truncated': False, 'note': None},
    }


class TestLineageDispatchWithMainPath(unittest.IsolatedAsyncioTestCase):

    async def _run(self, seed_id, items, tmp_dir, generations=1):
        async def search_fn(query, max_results=200, **kw):
            return _make_search_raw(items)

        with patch('scopus_mcp.server.client') as mock_client, \
             patch.dict(os.environ, {'SCOPUS_MCP_OUTPUT_DIR': tmp_dir}):
            mock_client.get_abstract = AsyncMock(
                return_value=_make_abstract_raw(seed_id)
            )
            mock_client.search_all = AsyncMock(side_effect=search_fn)
            from scopus_mcp.server import handle_call_tool
            return await handle_call_tool(
                'citation_lineage',
                {'seed_id': seed_id, 'direction': 'forward',
                 'generations': generations, 'max_per_node': 10},
            )

    def _read_json(self, text: str) -> dict:
        path_line = next(l for l in text.splitlines() if 'Corpus written to:' in l)
        json_path = path_line.split(':', 1)[1].strip()
        return json.loads(Path(json_path).read_text())

    async def test_json_contains_main_path_key(self):
        items = [{'scopus_id': 'c1', 'cbc': '20'}, {'scopus_id': 'c2', 'cbc': '5'}]
        with tempfile.TemporaryDirectory() as td:
            result = await self._run('seed1', items, td)
            data = self._read_json(result[0].text)
        assert 'main_path' in data

    async def test_json_contains_spc_edges_key(self):
        items = [{'scopus_id': 'c1', 'cbc': '20'}, {'scopus_id': 'c2', 'cbc': '5'}]
        with tempfile.TemporaryDirectory() as td:
            result = await self._run('seed1', items, td)
            data = self._read_json(result[0].text)
        assert 'spc_edges' in data

    async def test_json_records_still_present(self):
        items = [{'scopus_id': 'c1', 'cbc': '20'}]
        with tempfile.TemporaryDirectory() as td:
            result = await self._run('seed1', items, td)
            data = self._read_json(result[0].text)
        assert isinstance(data['records'], list)
        assert len(data['records']) == 2  # seed + c1

    async def test_summary_includes_html_path(self):
        items = [{'scopus_id': 'c1', 'cbc': '20'}]
        with tempfile.TemporaryDirectory() as td:
            result = await self._run('seed1', items, td)
        assert 'Interactive HTML:' in result[0].text

    async def test_summary_includes_png_path(self):
        items = [{'scopus_id': 'c1', 'cbc': '20'}]
        with tempfile.TemporaryDirectory() as td:
            result = await self._run('seed1', items, td)
        assert 'PNG:' in result[0].text

    async def test_html_file_exists_on_disk(self):
        items = [{'scopus_id': 'c1', 'cbc': '20'}]
        with tempfile.TemporaryDirectory() as td:
            result = await self._run('seed1', items, td)
            text = result[0].text
            html_line = next((l for l in text.splitlines() if 'Interactive HTML:' in l), None)
            assert html_line is not None
            html_path = html_line.split(':', 1)[1].strip()
            assert Path(html_path).exists(), f"HTML file not found: {html_path}"

    async def test_png_file_exists_on_disk(self):
        items = [{'scopus_id': 'c1', 'cbc': '20'}]
        with tempfile.TemporaryDirectory() as td:
            result = await self._run('seed1', items, td)
            text = result[0].text
            png_line = next((l for l in text.splitlines() if l.startswith('PNG:')), None)
            assert png_line is not None
            png_path = png_line.split(':', 1)[1].strip()
            assert Path(png_path).exists(), f"PNG file not found: {png_path}"

    async def test_main_path_in_summary_when_present(self):
        """When papers exist, summary must include a 'Main path' line."""
        items = [{'scopus_id': 'c1', 'cbc': '20'}, {'scopus_id': 'c2', 'cbc': '5'}]
        with tempfile.TemporaryDirectory() as td:
            result = await self._run('seed1', items, td)
        assert 'Main path' in result[0].text


if __name__ == '__main__':
    unittest.main()
