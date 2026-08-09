"""Tests for the citation-network builder tools.

compute_pairwise_edges and write_graph_to_disk: pure unit tests, no network.
Server dispatch (bibliographic_coupling, co_citation): client methods mocked.

SCOPUS_API_KEY set before server import to allow module-level ScopusClient().
"""
import csv
import math
import os
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from unittest.mock import AsyncMock, patch

os.environ.setdefault('SCOPUS_API_KEY', 'test-key')

from scopus_mcp.utils import (
    compute_pairwise_edges,
    write_graph_to_disk,
)

# GraphML namespace used by the writer
_NS = 'http://graphml.graphdrawing.org/graphml'


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _nodes(ids):
    return [{'id': i, 'label': f'Paper {i}', 'year': '2020', 'venue': 'J'} for i in ids]


def _edges_from_sets(seed_sets, min_shared=1):
    return compute_pairwise_edges(seed_sets, min_shared=min_shared)


# ---------------------------------------------------------------------------
# compute_pairwise_edges
# ---------------------------------------------------------------------------

class TestComputePairwiseEdges(unittest.TestCase):

    def test_correct_weight_and_cosine(self):
        sets = {
            'A': {'r1', 'r2', 'r3', 'r4'},  # |A|=4
            'B': {'r2', 'r3', 'r5'},         # |B|=3, shared={r2,r3}=2
        }
        edges = compute_pairwise_edges(sets, min_shared=1)
        self.assertEqual(len(edges), 1)
        e = edges[0]
        self.assertEqual(e['weight'], 2)
        expected_cosine = round(2 / math.sqrt(4 * 3), 6)
        self.assertAlmostEqual(e['cosine'], expected_cosine, places=5)

    def test_pair_appears_once(self):
        """Each unordered pair (a, b) must appear exactly once."""
        sets = {'A': {'r1', 'r2'}, 'B': {'r1', 'r2'}, 'C': {'r1', 'r2'}}
        edges = compute_pairwise_edges(sets, min_shared=1)
        pairs = {(e['source'], e['target']) for e in edges}
        self.assertEqual(len(pairs), 3)  # AB, AC, BC — no duplicates

    def test_min_shared_filters_weak_edges(self):
        sets = {
            'A': {'r1', 'r2', 'r3'},
            'B': {'r1', 'r4', 'r5'},       # 1 shared
            'C': {'r1', 'r2', 'r6'},       # 2 shared with A
        }
        edges_1 = compute_pairwise_edges(sets, min_shared=1)
        edges_2 = compute_pairwise_edges(sets, min_shared=2)
        self.assertEqual(len(edges_1), 3)   # AB(1), AC(2), BC(1)
        self.assertEqual(len(edges_2), 1)   # only AC(2)
        self.assertEqual(edges_2[0]['weight'], 2)

    def test_empty_seed_skipped(self):
        sets = {'A': {'r1', 'r2'}, 'B': set(), 'C': {'r1', 'r2'}}
        edges = compute_pairwise_edges(sets, min_shared=1)
        # B has empty set — only A↔C pair should appear
        self.assertEqual(len(edges), 1)
        sources = {e['source'] for e in edges}
        targets = {e['target'] for e in edges}
        self.assertNotIn('B', sources | targets)

    def test_no_edges_when_all_below_threshold(self):
        sets = {'A': {'r1'}, 'B': {'r2'}}
        edges = compute_pairwise_edges(sets, min_shared=2)
        self.assertEqual(edges, [])

    def test_sorted_by_weight_descending(self):
        sets = {
            'A': {'r1', 'r2', 'r3', 'r4'},
            'B': {'r1', 'r2', 'r3'},        # 3 shared with A
            'C': {'r1', 'r2'},              # 2 shared with A, 2 shared with B
        }
        edges = compute_pairwise_edges(sets, min_shared=1)
        weights = [e['weight'] for e in edges]
        self.assertEqual(weights, sorted(weights, reverse=True))

    def test_single_seed_produces_no_edges(self):
        sets = {'A': {'r1', 'r2', 'r3'}}
        self.assertEqual(compute_pairwise_edges(sets, min_shared=1), [])

    def test_cosine_is_one_for_identical_sets(self):
        sets = {'A': {'r1', 'r2', 'r3'}, 'B': {'r1', 'r2', 'r3'}}
        edges = compute_pairwise_edges(sets, min_shared=1)
        self.assertEqual(len(edges), 1)
        self.assertAlmostEqual(edges[0]['cosine'], 1.0, places=5)


# ---------------------------------------------------------------------------
# write_graph_to_disk
# ---------------------------------------------------------------------------

class TestWriteGraphToDisk(unittest.TestCase):

    def _write(self, tmp_dir, n_nodes=3, n_edges=2):
        nodes = _nodes(range(n_nodes))
        edges = [
            {'source': str(i), 'target': str(i + 1), 'weight': i + 1, 'cosine': 0.5}
            for i in range(n_edges)
        ]
        with patch.dict(os.environ, {'SCOPUS_MCP_OUTPUT_DIR': tmp_dir}):
            return write_graph_to_disk(nodes, edges, 'test-graph'), nodes, edges

    def test_graphml_is_valid_xml(self):
        with tempfile.TemporaryDirectory() as td:
            paths, _, _ = self._write(td)
            tree = ET.parse(paths['graphml_path'])
            self.assertIsNotNone(tree.getroot())

    def test_graphml_has_correct_node_count(self):
        with tempfile.TemporaryDirectory() as td:
            paths, nodes, _ = self._write(td, n_nodes=4)
            root = ET.parse(paths['graphml_path']).getroot()
            graph_el = root.find(f'{{{_NS}}}graph')
            found_nodes = graph_el.findall(f'{{{_NS}}}node')
            self.assertEqual(len(found_nodes), 4)

    def test_graphml_has_correct_edge_count(self):
        with tempfile.TemporaryDirectory() as td:
            paths, _, edges = self._write(td, n_nodes=4, n_edges=3)
            root = ET.parse(paths['graphml_path']).getroot()
            graph_el = root.find(f'{{{_NS}}}graph')
            found_edges = graph_el.findall(f'{{{_NS}}}edge')
            self.assertEqual(len(found_edges), 3)

    def test_graphml_node_label_data(self):
        with tempfile.TemporaryDirectory() as td:
            paths, _, _ = self._write(td)
            root = ET.parse(paths['graphml_path']).getroot()
            graph_el = root.find(f'{{{_NS}}}graph')
            first_node = graph_el.find(f'{{{_NS}}}node')
            data_els = first_node.findall(f'{{{_NS}}}data')
            keys = {d.get('key') for d in data_els}
            self.assertIn('d_label', keys)

    def test_graphml_has_key_declarations(self):
        with tempfile.TemporaryDirectory() as td:
            paths, _, _ = self._write(td)
            root = ET.parse(paths['graphml_path']).getroot()
            key_ids = {k.get('id') for k in root.findall(f'{{{_NS}}}key')}
            self.assertIn('d_label', key_ids)
            self.assertIn('d_weight', key_ids)
            self.assertIn('d_cosine', key_ids)

    def test_graphml_edge_weight_data(self):
        with tempfile.TemporaryDirectory() as td:
            paths, _, _ = self._write(td, n_nodes=2, n_edges=1)
            root = ET.parse(paths['graphml_path']).getroot()
            graph_el = root.find(f'{{{_NS}}}graph')
            edge_el = graph_el.find(f'{{{_NS}}}edge')
            data_keys = {d.get('key'): d.text for d in edge_el.findall(f'{{{_NS}}}data')}
            self.assertIn('d_weight', data_keys)
            self.assertIn('d_cosine', data_keys)
            self.assertEqual(data_keys['d_weight'], '1')

    def test_csv_has_correct_columns(self):
        with tempfile.TemporaryDirectory() as td:
            paths, _, _ = self._write(td)
            with open(paths['csv_path'], newline='', encoding='utf-8') as f:
                reader = csv.DictReader(f)
                self.assertEqual(reader.fieldnames, ['source', 'target', 'weight', 'cosine'])

    def test_csv_has_correct_row_count(self):
        with tempfile.TemporaryDirectory() as td:
            paths, _, _ = self._write(td, n_nodes=5, n_edges=4)
            with open(paths['csv_path'], newline='', encoding='utf-8') as f:
                rows = list(csv.DictReader(f))
            self.assertEqual(len(rows), 4)

    def test_csv_edge_values(self):
        with tempfile.TemporaryDirectory() as td:
            nodes = _nodes([10, 20])
            edges = [{'source': '10', 'target': '20', 'weight': 7, 'cosine': 0.816}]
            with patch.dict(os.environ, {'SCOPUS_MCP_OUTPUT_DIR': td}):
                paths = write_graph_to_disk(nodes, edges, 'vals-test')
            with open(paths['csv_path'], newline='', encoding='utf-8') as f:
                rows = list(csv.DictReader(f))
            self.assertEqual(rows[0]['source'], '10')
            self.assertEqual(rows[0]['target'], '20')
            self.assertEqual(rows[0]['weight'], '7')

    def test_output_dir_created_if_missing(self):
        with tempfile.TemporaryDirectory() as td:
            new_dir = os.path.join(td, 'graphs', 'output')
            with patch.dict(os.environ, {'SCOPUS_MCP_OUTPUT_DIR': new_dir}):
                write_graph_to_disk(_nodes([1]), [], 'slug')
            self.assertTrue(Path(new_dir).is_dir())

    def test_none_node_attributes_omitted_from_graphml(self):
        """Nodes with None year/venue must not emit empty <data> elements."""
        with tempfile.TemporaryDirectory() as td:
            nodes = [{'id': '1', 'label': 'Paper', 'year': None, 'venue': None}]
            with patch.dict(os.environ, {'SCOPUS_MCP_OUTPUT_DIR': td}):
                paths = write_graph_to_disk(nodes, [], 'null-test')
            root = ET.parse(paths['graphml_path']).getroot()
            graph_el = root.find(f'{{{_NS}}}graph')
            node_el = graph_el.find(f'{{{_NS}}}node')
            data_keys = {d.get('key') for d in node_el.findall(f'{{{_NS}}}data')}
            # label present, year and venue absent
            self.assertIn('d_label', data_keys)
            self.assertNotIn('d_year', data_keys)
            self.assertNotIn('d_venue', data_keys)


# ---------------------------------------------------------------------------
# Server dispatch — bibliographic_coupling
# ---------------------------------------------------------------------------

def _make_ref_raw(seed_id, ref_ids, title='Seed Paper', year='2021', venue='Journal A'):
    """Build a fake get_references raw response (REF view carries seed coredata)."""
    refs = [
        {
            '@id': str(i),
            'scopus-id': rid,
            'title': f'Reference {i}',
            'sourcetitle': 'Some Journal',
            'prism:coverDate': '2020-01-01',
            'author-list': {'author': [{'ce:indexed-name': 'Author A.', '@auid': f'a{i}'}]},
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
            'authors': {'author': [{'ce:indexed-name': 'Author A.', '@auid': '111'}]},
            'references': {'reference': refs},
        }
    }


def _make_abstract_raw(seed_id, title='Seed Paper', year='2021', venue='Journal A'):
    return {
        'abstracts-retrieval-response': {
            'coredata': {
                'dc:identifier': f'SCOPUS_ID:{seed_id}',
                'dc:title': title,
                'prism:coverDate': f'{year}-01-01',
                'prism:publicationName': venue,
                'link': [],
            },
            'authors': {'author': [{'ce:indexed-name': 'Author A.', '@auid': '111'}]},
        }
    }


def _make_search_raw(citing_ids):
    """Build a fake search_all raw response with the given citing IDs."""
    return {
        'search-results': {
            'opensearch:totalResults': str(len(citing_ids)),
            'entry': [
                {'dc:identifier': f'SCOPUS_ID:{cid}', 'dc:title': f'Citer {cid}'}
                for cid in citing_ids
            ],
        },
        '_meta': {
            'total_fetched': len(citing_ids),
            'total_available': len(citing_ids),
            'truncated': False,
            'note': None,
        },
    }


class TestBibliographicCouplingDispatch(unittest.IsolatedAsyncioTestCase):

    async def _dispatch(self, args, tmp_dir):
        with patch('scopus_mcp.server.client') as mock_client, \
             patch.dict(os.environ, {'SCOPUS_MCP_OUTPUT_DIR': tmp_dir}):
            # Each call to get_references returns the fake REF-view response
            mock_client.get_references = AsyncMock(side_effect=lambda sid: _make_ref_raw(
                sid,
                ref_ids=['ref1', 'ref2', 'ref3'] if sid == '111' else ['ref2', 'ref3', 'ref4'],
                title=f'Paper {sid}',
            ))
            from scopus_mcp.server import handle_call_tool
            return await handle_call_tool('bibliographic_coupling', args)

    async def test_summary_contains_file_paths(self):
        with tempfile.TemporaryDirectory() as td:
            result = await self._dispatch({'seed_ids': ['111', '222'], 'min_shared': 1}, td)
            text = result[0].text
            self.assertIn('GraphML:', text)
            self.assertIn('CSV:', text)

    async def test_summary_reports_seed_and_edge_counts(self):
        with tempfile.TemporaryDirectory() as td:
            result = await self._dispatch({'seed_ids': ['111', '222'], 'min_shared': 1}, td)
            text = result[0].text
            self.assertIn('2/2 seeds processed', text)
            self.assertIn('1 edges emitted', text)

    async def test_summary_contains_top_edge(self):
        with tempfile.TemporaryDirectory() as td:
            result = await self._dispatch({'seed_ids': ['111', '222'], 'min_shared': 1}, td)
            text = result[0].text
            self.assertIn('weight=2', text)

    async def test_graphml_file_is_valid(self):
        with tempfile.TemporaryDirectory() as td:
            result = await self._dispatch({'seed_ids': ['111', '222'], 'min_shared': 1}, td)
            gml_line = next(l for l in result[0].text.splitlines() if 'GraphML:' in l)
            gml_path = gml_line.split('GraphML:')[1].strip()
            root = ET.parse(gml_path).getroot()
            graph_el = root.find(f'{{{_NS}}}graph')
            self.assertEqual(len(graph_el.findall(f'{{{_NS}}}node')), 2)
            self.assertEqual(len(graph_el.findall(f'{{{_NS}}}edge')), 1)

    async def test_seed_with_empty_refs_skipped_with_note(self):
        with tempfile.TemporaryDirectory() as td:
            with patch('scopus_mcp.server.client') as mock_client, \
                 patch.dict(os.environ, {'SCOPUS_MCP_OUTPUT_DIR': td}):
                # '999' returns no references
                def side_effect(sid):
                    if sid == '999':
                        return _make_ref_raw(sid, ref_ids=[])
                    return _make_ref_raw(sid, ref_ids=['ref1', 'ref2', 'ref3'])
                mock_client.get_references = AsyncMock(side_effect=side_effect)
                from scopus_mcp.server import handle_call_tool
                result = await handle_call_tool(
                    'bibliographic_coupling',
                    {'seed_ids': ['111', '999'], 'min_shared': 1},
                )
            text = result[0].text
            self.assertIn('Skipped', text)
            self.assertIn('999', text)

    async def test_min_shared_filters_edges(self):
        with tempfile.TemporaryDirectory() as td:
            # With min_shared=3, the 2-shared pair should be filtered out
            result = await self._dispatch({'seed_ids': ['111', '222'], 'min_shared': 3}, td)
            text = result[0].text
            self.assertIn('0 edges emitted', text)
            self.assertIn('(none)', text)


# ---------------------------------------------------------------------------
# Server dispatch — co_citation
# ---------------------------------------------------------------------------

class TestCoCitationDispatch(unittest.IsolatedAsyncioTestCase):

    async def _dispatch(self, args, tmp_dir, citing_map=None):
        """citing_map: {sid: [list of citing paper IDs]}"""
        if citing_map is None:
            citing_map = {
                '111': ['c1', 'c2', 'c3'],
                '222': ['c2', 'c3', 'c4'],
            }

        with patch('scopus_mcp.server.client') as mock_client, \
             patch.dict(os.environ, {'SCOPUS_MCP_OUTPUT_DIR': tmp_dir}):
            mock_client.get_abstract = AsyncMock(
                side_effect=lambda sid: _make_abstract_raw(sid, title=f'Paper {sid}')
            )
            mock_client.search_all = AsyncMock(
                side_effect=lambda q, max_results: _make_search_raw(
                    citing_map.get(next(
                        (k for k in citing_map if k in q), None
                    ), [])
                )
            )
            from scopus_mcp.server import handle_call_tool
            return await handle_call_tool('co_citation', args)

    async def test_summary_contains_file_paths(self):
        with tempfile.TemporaryDirectory() as td:
            result = await self._dispatch({'seed_ids': ['111', '222'], 'min_shared': 1}, td)
            text = result[0].text
            self.assertIn('GraphML:', text)
            self.assertIn('CSV:', text)

    async def test_summary_reports_seed_and_edge_counts(self):
        with tempfile.TemporaryDirectory() as td:
            result = await self._dispatch({'seed_ids': ['111', '222'], 'min_shared': 1}, td)
            text = result[0].text
            self.assertIn('2/2 seeds processed', text)
            self.assertIn('1 edges emitted', text)

    async def test_top_edge_weight_correct(self):
        with tempfile.TemporaryDirectory() as td:
            result = await self._dispatch({'seed_ids': ['111', '222'], 'min_shared': 1}, td)
            self.assertIn('weight=2', result[0].text)

    async def test_seed_with_no_citers_skipped(self):
        with tempfile.TemporaryDirectory() as td:
            citing_map = {'111': ['c1', 'c2'], '333': []}
            result = await self._dispatch(
                {'seed_ids': ['111', '333'], 'min_shared': 1}, td, citing_map=citing_map
            )
            text = result[0].text
            self.assertIn('Skipped', text)
            self.assertIn('333', text)

    async def test_graphml_file_is_valid(self):
        with tempfile.TemporaryDirectory() as td:
            result = await self._dispatch({'seed_ids': ['111', '222'], 'min_shared': 1}, td)
            gml_line = next(l for l in result[0].text.splitlines() if 'GraphML:' in l)
            gml_path = gml_line.split('GraphML:')[1].strip()
            ET.parse(gml_path)  # raises if invalid XML

    async def test_does_not_include_seed_in_own_citing_set(self):
        """If seed A appears as a citing paper of A, it must be filtered out."""
        with tempfile.TemporaryDirectory() as td:
            # seed '111' appears in its own citing list
            citing_map = {'111': ['111', 'c1', 'c2'], '222': ['c1', 'c2', 'c3']}
            result = await self._dispatch(
                {'seed_ids': ['111', '222'], 'min_shared': 1}, td, citing_map=citing_map
            )
            text = result[0].text
            # Edge should exist: 111 and 222 share c1,c2 → weight=2
            self.assertIn('weight=2', text)
