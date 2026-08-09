"""Tests for feat/lineage-and-render:
  - GraphML nodes carry title/creator/label metadata (Piece 1)
  - render_graph_png writes a PNG; write_graph_to_disk survives render failure (Piece 2)
  - citation_lineage correctly assigns generations, dedupes, respects caps (Piece 3)

No live API calls — client methods are mocked throughout.
"""
import json
import os
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from unittest.mock import AsyncMock, patch

os.environ.setdefault('SCOPUS_API_KEY', 'test-key')

from scopus_mcp.utils import (
    _make_node_label,
    write_graph_to_disk,
    render_graph_png,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

GRAPHML_NS = {'g': 'http://graphml.graphdrawing.org/graphml'}


def _node_data(graphml_path: str, node_id: str) -> dict:
    """Parse a GraphML file and return {key: text} for a given node id."""
    tree = ET.parse(graphml_path)
    root = tree.getroot()
    node_el = root.find(f'.//g:node[@id="{node_id}"]', GRAPHML_NS)
    assert node_el is not None, f"Node {node_id!r} not found in GraphML"
    return {d.get('key'): d.text for d in node_el.findall('g:data', GRAPHML_NS)}


def _make_coupling_ref_raw(seed_id, ref_ids, title='Seed Paper', year='2021',
                            venue='Journal A', creator='Author A.'):
    """Build a fake get_references REF-view response using the real flat structure."""
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
                'dc:title': title,
                'prism:coverDate': f'{year}-01-01',
                'prism:publicationName': venue,
                'link': [],
            },
            'authors': {'author': [{'ce:indexed-name': creator, '@auid': '111'}]},
            'references': {'reference': refs},
        }
    }


def _make_abstract_raw(sid, title='Test Paper', year='2020', venue='Test Journal',
                       cbc='100', creator='Doe J.'):
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
            'authors': {'author': [{'ce:indexed-name': creator, '@auid': '1'}]},
        }
    }


def _make_search_raw(items):
    entries = [
        {
            'dc:identifier': f'SCOPUS_ID:{it["scopus_id"]}',
            'dc:title': it.get('title', f'Paper {it["scopus_id"]}'),
            'prism:coverDate': it.get('year', '2020') + '-01-01',
            'prism:publicationName': it.get('venue', 'Journal'),
            'citedby-count': str(it.get('cbc', 10)),
            'prism:doi': f'10.1/{it["scopus_id"]}',
            'link': [],
        }
        for it in items
    ]
    return {
        'search-results': {'entry': entries},
        '_meta': {
            'total_fetched': len(entries), 'total_available': len(entries),
            'truncated': False, 'note': None,
        },
    }


def _read_corpus(text: str) -> list:
    """Extract corpus JSON path from summary text and read the records list."""
    path_line = next(l for l in text.splitlines() if 'Corpus written to:' in l)
    json_path = path_line.split(':', 1)[1].strip()
    data = json.loads(Path(json_path).read_text())
    # Corpus is now a dict with a 'records' key
    if isinstance(data, list):
        return data
    return data.get('records', [])


# ---------------------------------------------------------------------------
# Piece 1: _make_node_label
# ---------------------------------------------------------------------------

class TestMakeNodeLabel(unittest.TestCase):

    def test_surname_year_when_both_present(self):
        assert _make_node_label({'creator': 'Swanson E.B.', 'year': '1997'}) == 'Swanson 1997'

    def test_surname_only_when_no_year(self):
        assert _make_node_label({'creator': 'Swanson E.B.'}) == 'Swanson'

    def test_strips_trailing_punctuation_from_surname(self):
        assert _make_node_label({'creator': 'Jones,', 'year': '2010'}) == 'Jones 2010'

    def test_falls_back_to_title_when_no_creator(self):
        label = _make_node_label({'title': 'Organizing Vision: A Concept', 'year': '1997'})
        assert label == 'Organizing Vision: A Concept'[:40]

    def test_truncates_long_titles(self):
        assert _make_node_label({'title': 'A' * 60}) == 'A' * 40

    def test_falls_back_to_node_id(self):
        assert _make_node_label({}, node_id='85186605555') == '85186605555'

    def test_empty_meta_empty_node_id(self):
        assert _make_node_label({}) == ''


# ---------------------------------------------------------------------------
# Piece 1: GraphML metadata — title + creator written onto nodes
# ---------------------------------------------------------------------------

class TestGraphMLMetadata(unittest.TestCase):

    def _nodes_and_edges(self):
        nodes = [
            {
                'id': '111',
                'label': 'Swanson 1997',
                'title': 'Organizing Vision',
                'creator': 'Swanson E.B.',
                'year': '1997',
                'venue': 'MIS Quarterly',
            },
            {
                'id': '222',
                'label': 'Karimi 2000',
                'title': 'Another Paper',
                'creator': 'Karimi J.',
                'year': '2000',
                'venue': 'MISQ',
            },
        ]
        edges = [{'source': '111', 'target': '222', 'weight': 3, 'cosine': 0.5}]
        return nodes, edges

    def test_node_has_title_in_graphml(self):
        nodes, edges = self._nodes_and_edges()
        with tempfile.TemporaryDirectory() as td:
            with patch.dict(os.environ, {'SCOPUS_MCP_OUTPUT_DIR': td}):
                paths = write_graph_to_disk(nodes, edges, 'meta-test')
                data = _node_data(paths['graphml_path'], '111')
        assert data.get('d_title') == 'Organizing Vision', \
            f"d_title: {data.get('d_title')!r}"

    def test_node_has_readable_label_not_raw_id(self):
        nodes, edges = self._nodes_and_edges()
        with tempfile.TemporaryDirectory() as td:
            with patch.dict(os.environ, {'SCOPUS_MCP_OUTPUT_DIR': td}):
                paths = write_graph_to_disk(nodes, edges, 'label-test')
                data = _node_data(paths['graphml_path'], '111')
        assert data.get('d_label') == 'Swanson 1997'

    def test_node_has_creator_in_graphml(self):
        nodes, edges = self._nodes_and_edges()
        with tempfile.TemporaryDirectory() as td:
            with patch.dict(os.environ, {'SCOPUS_MCP_OUTPUT_DIR': td}):
                paths = write_graph_to_disk(nodes, edges, 'creator-test')
                data = _node_data(paths['graphml_path'], '111')
        assert data.get('d_creator') == 'Swanson E.B.'

    def test_node_has_year_in_graphml(self):
        nodes, edges = self._nodes_and_edges()
        with tempfile.TemporaryDirectory() as td:
            with patch.dict(os.environ, {'SCOPUS_MCP_OUTPUT_DIR': td}):
                paths = write_graph_to_disk(nodes, edges, 'year-test')
                data = _node_data(paths['graphml_path'], '111')
        assert data.get('d_year') == '1997'

    def test_graphml_is_valid_xml(self):
        nodes, edges = self._nodes_and_edges()
        with tempfile.TemporaryDirectory() as td:
            with patch.dict(os.environ, {'SCOPUS_MCP_OUTPUT_DIR': td}):
                paths = write_graph_to_disk(nodes, edges, 'valid-xml-test')
                tree = ET.parse(paths['graphml_path'])
        assert tree.getroot() is not None


# ---------------------------------------------------------------------------
# Piece 2: render_graph_png + write_graph_to_disk failure isolation
# ---------------------------------------------------------------------------

class TestRenderGraphPng(unittest.TestCase):

    def test_render_produces_png_file(self):
        nodes = [
            {'id': 'a', 'label': 'Paper A'},
            {'id': 'b', 'label': 'Paper B'},
            {'id': 'c', 'label': 'Paper C'},
        ]
        edges = [
            {'source': 'a', 'target': 'b', 'weight': 5, 'cosine': 0.7},
            {'source': 'b', 'target': 'c', 'weight': 3, 'cosine': 0.4},
        ]
        with tempfile.TemporaryDirectory() as td:
            with patch.dict(os.environ, {'SCOPUS_MCP_OUTPUT_DIR': td}):
                result = render_graph_png(nodes, edges, 'test-render')
                exists = Path(result).exists() if result else False
        assert result is not None, "render_graph_png returned None unexpectedly"
        assert exists, f"PNG file not found: {result}"
        assert result.endswith('.png')

    def test_render_empty_graph_returns_none(self):
        with tempfile.TemporaryDirectory() as td:
            with patch.dict(os.environ, {'SCOPUS_MCP_OUTPUT_DIR': td}):
                result = render_graph_png([], [], 'test-empty')
        assert result is None

    def test_write_graph_survives_render_failure(self):
        """write_graph_to_disk must complete (GraphML + CSV) even when render raises."""
        nodes = [
            {
                'id': 'x', 'label': 'X', 'title': 'X paper',
                'creator': None, 'year': '2020', 'venue': None,
            }
        ]
        edges = []
        with tempfile.TemporaryDirectory() as td:
            with patch.dict(os.environ, {'SCOPUS_MCP_OUTPUT_DIR': td}), \
                 patch('scopus_mcp.utils.render_graph_png',
                       side_effect=RuntimeError('simulated render failure')):
                paths = write_graph_to_disk(nodes, edges, 'fail-render-test')
                graphml_exists = Path(paths['graphml_path']).exists()
                csv_exists = Path(paths['csv_path']).exists()

        assert graphml_exists, "GraphML missing after render failure"
        assert csv_exists, "CSV missing after render failure"
        assert paths.get('png_path') is None, "png_path should be None on failure"


# ---------------------------------------------------------------------------
# Piece 1+2: bibliographic_coupling dispatch — metadata labels + PNG in summary
# ---------------------------------------------------------------------------

class TestCouplingMetadataAndPng(unittest.IsolatedAsyncioTestCase):

    async def _dispatch(self, args, tmp_dir):
        with patch('scopus_mcp.server.client') as mock_client, \
             patch.dict(os.environ, {'SCOPUS_MCP_OUTPUT_DIR': tmp_dir}):
            mock_client.get_references = AsyncMock(
                side_effect=lambda sid: _make_coupling_ref_raw(
                    sid,
                    ref_ids=[f'shared{i}' for i in range(5)],
                    title=f'Paper {sid}',
                    creator='Swanson E.B.',
                    year='1997',
                )
            )
            from scopus_mcp.server import handle_call_tool
            return await handle_call_tool('bibliographic_coupling', args)

    async def test_summary_includes_png_path(self):
        with tempfile.TemporaryDirectory() as td:
            result = await self._dispatch({'seed_ids': ['111', '222'], 'min_shared': 2}, td)
        text = result[0].text
        assert 'PNG:' in text, f"PNG path not in summary:\n{text[:500]}"

    async def test_render_failure_does_not_crash_tool(self):
        """A render error must not prevent the tool from returning a valid result."""
        with patch('scopus_mcp.utils.render_graph_png',
                   side_effect=RuntimeError('render broken')):
            with tempfile.TemporaryDirectory() as td:
                result = await self._dispatch({'seed_ids': ['111', '222'], 'min_shared': 2}, td)
        text = result[0].text
        assert 'GraphML:' in text, "Tool crashed on render failure"
        assert 'PNG:' not in text, "PNG path should be absent when render fails"


# ---------------------------------------------------------------------------
# Piece 3: citation_lineage
# ---------------------------------------------------------------------------

class TestCitationLineageDispatch(unittest.IsolatedAsyncioTestCase):

    async def _dispatch(self, args, tmp_dir, get_abstract_fn,
                        search_all_fn=None, get_refs_fn=None):
        with patch('scopus_mcp.server.client') as mock_client, \
             patch.dict(os.environ, {'SCOPUS_MCP_OUTPUT_DIR': tmp_dir}):
            mock_client.get_abstract = AsyncMock(side_effect=get_abstract_fn)
            if search_all_fn is not None:
                mock_client.search_all = AsyncMock(side_effect=search_all_fn)
            if get_refs_fn is not None:
                mock_client.get_references = AsyncMock(side_effect=get_refs_fn)
            from scopus_mcp.server import handle_call_tool
            return await handle_call_tool('citation_lineage', args)

    async def test_generation_assignment(self):
        """Papers fetched in gen 1 must have generation=1 in the corpus."""
        with tempfile.TemporaryDirectory() as td:
            result = await self._dispatch(
                {'seed_id': 'seed1', 'generations': 1},
                td,
                lambda sid: _make_abstract_raw(sid),
                AsyncMock(return_value=_make_search_raw([
                    {'scopus_id': 'c1', 'cbc': '50'},
                    {'scopus_id': 'c2', 'cbc': '30'},
                ])),
            )
            text = result[0].text
            corpus = _read_corpus(text)

        assert 'gen 1: 2' in text
        gen1 = [r for r in corpus if r['generation'] == 1]
        assert len(gen1) == 2
        assert all(r['generation'] == 1 for r in gen1)

    async def test_dedup_across_generations(self):
        """A paper seen in gen 1 must not reappear in gen 2."""
        call_count = [0]

        async def search_side(query, max_results=200, **kw):
            call_count[0] += 1
            if call_count[0] == 1:
                return _make_search_raw([{'scopus_id': 'c1', 'cbc': '100'}])
            return _make_search_raw([
                {'scopus_id': 'c1', 'cbc': '100'},  # already seen
                {'scopus_id': 'c3', 'cbc': '5'},    # new
            ])

        with tempfile.TemporaryDirectory() as td:
            result = await self._dispatch(
                {'seed_id': 'seed1', 'generations': 2},
                td,
                lambda sid: _make_abstract_raw(sid),
                search_side,
            )
            text = result[0].text
            corpus = _read_corpus(text)

        assert 'gen 1: 1' in text
        assert 'gen 2: 1' in text
        non_seed = [r for r in corpus if r['generation'] > 0]
        assert len(non_seed) == 2
        assert {r['scopus_id'] for r in non_seed} == {'c1', 'c3'}

    async def test_max_per_node_cap(self):
        """search_all must receive max_results=max_per_node."""
        captured = {}

        async def search_side(query, max_results=200, **kw):
            captured['max_results'] = max_results
            return _make_search_raw([])

        with tempfile.TemporaryDirectory() as td:
            await self._dispatch(
                {'seed_id': 'seed1', 'generations': 1, 'max_per_node': 50},
                td,
                lambda sid: _make_abstract_raw(sid),
                search_side,
            )
        assert captured.get('max_results') == 50

    async def test_generations_cap_limits_expansion(self):
        """With generations=1, only one search_all call must be made."""
        call_count = [0]

        async def search_side(query, max_results=200, **kw):
            call_count[0] += 1
            return _make_search_raw([{'scopus_id': f'c{call_count[0]}', 'cbc': '500'}])

        with tempfile.TemporaryDirectory() as td:
            await self._dispatch(
                {'seed_id': 'seed1', 'generations': 1, 'max_per_node': 200},
                td,
                lambda sid: _make_abstract_raw(sid),
                search_side,
            )
        assert call_count[0] == 1, \
            f"Expected 1 search_all call for generations=1, got {call_count[0]}"

    async def test_min_citing_prunes_low_cited_nodes(self):
        """Papers with cbc < min_citing must not be expanded in the next gen."""
        call_count = [0]

        async def search_side(query, max_results=200, **kw):
            call_count[0] += 1
            if call_count[0] == 1:
                return _make_search_raw([
                    {'scopus_id': 'hi', 'cbc': '200'},
                    {'scopus_id': 'lo', 'cbc': '1'},
                ])
            return _make_search_raw([{'scopus_id': 'c_hi_child', 'cbc': '5'}])

        with tempfile.TemporaryDirectory() as td:
            await self._dispatch(
                {'seed_id': 'seed1', 'generations': 2, 'min_citing': 10},
                td,
                lambda sid: _make_abstract_raw(sid),
                search_side,
            )
        # gen1 call + gen2 expansion of 'hi' only ('lo' pruned by min_citing)
        assert call_count[0] == 2

    async def test_empty_branch_stops_cleanly(self):
        """Zero citing papers must produce a valid summary without raising."""
        with tempfile.TemporaryDirectory() as td:
            result = await self._dispatch(
                {'seed_id': 'seed1', 'generations': 2},
                td,
                lambda sid: _make_abstract_raw(sid),
                AsyncMock(return_value=_make_search_raw([])),
            )
        text = result[0].text
        assert 'Citation lineage' in text
        assert 'Corpus written to:' in text

    async def test_corpus_json_is_written(self):
        """The corpus JSON must exist and contain records for seed + gen 1 papers."""
        with tempfile.TemporaryDirectory() as td:
            result = await self._dispatch(
                {'seed_id': 'seed1', 'generations': 1},
                td,
                lambda sid: _make_abstract_raw(sid, title='The Seed'),
                AsyncMock(return_value=_make_search_raw([
                    {'scopus_id': 'p1', 'title': 'Child Paper', 'cbc': '20'},
                ])),
            )
            text = result[0].text
            corpus = _read_corpus(text)

        assert isinstance(corpus, list)
        assert len(corpus) == 2  # seed (gen 0) + child (gen 1)
        seed_rec = next(r for r in corpus if r['scopus_id'] == 'seed1')
        assert seed_rec['generation'] == 0
        child_rec = next(r for r in corpus if r['scopus_id'] == 'p1')
        assert child_rec['generation'] == 1
        assert child_rec['title'] == 'Child Paper'


    async def test_forward_query_uses_eid_format(self):
        """_fetch_next forward must call search_all with REF(2-s2.0-<id>), not bare id."""
        captured_queries = []

        async def search_side(query, max_results=200, **kw):
            captured_queries.append(query)
            return _make_search_raw([{'scopus_id': 'c1', 'cbc': '5'}])

        with tempfile.TemporaryDirectory() as td:
            await self._dispatch(
                {'seed_id': '0031512927', 'generations': 1, 'max_per_node': 10},
                td,
                lambda sid: _make_abstract_raw(sid),
                search_side,
            )

        assert len(captured_queries) == 1, (
            f"Expected 1 search_all call, got {len(captured_queries)}"
        )
        assert captured_queries[0] == 'REF(2-s2.0-0031512927)', (
            f"Wrong query: {captured_queries[0]!r}"
        )

    async def test_forward_query_strips_eid_prefix_in_seed(self):
        """search_all query must not double-prefix when seed_id already has 2-s2.0-."""
        captured_queries = []

        async def search_side(query, max_results=200, **kw):
            captured_queries.append(query)
            return _make_search_raw([])

        with tempfile.TemporaryDirectory() as td:
            await self._dispatch(
                {'seed_id': '2-s2.0-0031512927', 'generations': 1},
                td,
                lambda sid: _make_abstract_raw('0031512927'),
                search_side,
            )

        assert len(captured_queries) == 1
        assert captured_queries[0] == 'REF(2-s2.0-0031512927)', (
            f"Double-prefixed query: {captured_queries[0]!r}"
        )


# ---------------------------------------------------------------------------
# Piece 3 continued: backward direction
# ---------------------------------------------------------------------------

class TestCitationLineageBackward(unittest.IsolatedAsyncioTestCase):
    """Backward direction walks cited references via get_references."""

    async def _dispatch(self, args, tmp_dir, get_abstract_fn, get_refs_fn):
        with patch('scopus_mcp.server.client') as mock_client, \
             patch.dict(os.environ, {'SCOPUS_MCP_OUTPUT_DIR': tmp_dir}):
            mock_client.get_abstract = AsyncMock(side_effect=get_abstract_fn)
            mock_client.get_references = AsyncMock(side_effect=get_refs_fn)
            from scopus_mcp.server import handle_call_tool
            return await handle_call_tool('citation_lineage', args)

    async def test_backward_calls_get_references_not_search_all(self):
        """Backward direction must call get_references, not search_all."""
        with patch('scopus_mcp.server.client') as mock_client, \
             tempfile.TemporaryDirectory() as td, \
             patch.dict(os.environ, {'SCOPUS_MCP_OUTPUT_DIR': td}):
            mock_client.get_abstract = AsyncMock(
                return_value=_make_abstract_raw('seed1')
            )
            mock_client.get_references = AsyncMock(
                return_value=_make_coupling_ref_raw('seed1', ['ref1', 'ref2'])
            )
            # search_all should NOT be called
            mock_client.search_all = AsyncMock(
                side_effect=AssertionError("search_all must not be called for backward")
            )
            from scopus_mcp.server import handle_call_tool
            result = await handle_call_tool(
                'citation_lineage',
                {'seed_id': 'seed1', 'generations': 1, 'direction': 'backward'},
            )
        assert 'Citation lineage (backward)' in result[0].text

    async def test_backward_assigns_generations(self):
        """Refs fetched in gen 1 must have generation=1 in corpus."""
        with tempfile.TemporaryDirectory() as td:
            result = await self._dispatch(
                {'seed_id': 'seed1', 'generations': 1, 'direction': 'backward'},
                td,
                lambda sid: _make_abstract_raw(sid),
                lambda sid: _make_coupling_ref_raw(
                    sid, ['r1', 'r2', 'r3'], title=f'Paper {sid}'
                ),
            )
            text = result[0].text
            corpus = _read_corpus(text)

        assert 'gen 1: 3' in text
        gen1 = [r for r in corpus if r['generation'] == 1]
        assert len(gen1) == 3
        assert all(r['generation'] == 1 for r in gen1)

    async def test_backward_dedup_across_generations(self):
        """A ref seen in gen 1 must not reappear in gen 2 (same dedup machinery)."""
        call_count = [0]

        def refs_side(sid):
            call_count[0] += 1
            if call_count[0] == 1:
                # seed's refs: r1 only
                return _make_coupling_ref_raw(sid, ['r1'])
            # r1's refs: r1 already seen, r2 new
            return _make_coupling_ref_raw(sid, ['r1', 'r2'])

        with tempfile.TemporaryDirectory() as td:
            result = await self._dispatch(
                {'seed_id': 'seed1', 'generations': 2, 'direction': 'backward'},
                td,
                lambda sid: _make_abstract_raw(sid),
                refs_side,
            )
            text = result[0].text
            corpus = _read_corpus(text)

        assert 'gen 1: 1' in text
        assert 'gen 2: 1' in text
        non_seed = [r for r in corpus if r['generation'] > 0]
        assert len(non_seed) == 2
        assert {r['scopus_id'] for r in non_seed} == {'r1', 'r2'}

    async def test_backward_skips_refs_with_no_id(self):
        """References with neither scopus_id nor doi must be silently skipped."""
        raw_with_blanks = {
            'abstracts-retrieval-response': {
                'coredata': {
                    'dc:identifier': 'SCOPUS_ID:seed1',
                    'dc:title': 'Seed', 'prism:coverDate': '2020-01-01',
                    'prism:publicationName': 'J', 'link': [],
                },
                'authors': {'author': [{'ce:indexed-name': 'A.', '@auid': '1'}]},
                'references': {
                    'reference': [
                        # good ref
                        {
                            '@id': '1', 'scopus-id': 'goodref',
                            'title': 'Good', 'sourcetitle': 'J',
                            'prism:coverDate': '2000-01-01',
                            'author-list': {'author': []},
                        },
                        # no scopus-id, no doi → must be skipped
                        {
                            '@id': '2', 'title': 'No IDs', 'sourcetitle': 'J',
                            'prism:coverDate': '2001-01-01',
                            'author-list': {'author': []},
                        },
                    ]
                },
            }
        }
        with tempfile.TemporaryDirectory() as td:
            result = await self._dispatch(
                {'seed_id': 'seed1', 'generations': 1, 'direction': 'backward'},
                td,
                lambda sid: _make_abstract_raw(sid),
                lambda sid: raw_with_blanks,
            )
            corpus = _read_corpus(result[0].text)

        non_seed = [r for r in corpus if r['generation'] > 0]
        assert len(non_seed) == 1
        assert non_seed[0]['scopus_id'] == 'goodref'

    async def test_forward_is_default_direction(self):
        """Without explicit direction, the tool must use forward (calls search_all)."""
        search_called = [False]

        async def search_side(query, **kw):
            search_called[0] = True
            return _make_search_raw([])

        with tempfile.TemporaryDirectory() as td:
            await TestCitationLineageDispatch()._dispatch(
                {'seed_id': 'seed1', 'generations': 1},
                td,
                lambda sid: _make_abstract_raw(sid),
                search_side,
            )
        assert search_called[0], "forward direction should call search_all"


if __name__ == '__main__':
    unittest.main()
