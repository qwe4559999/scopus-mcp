"""
Tests for the backward main-path generation-adjacency bug fix.

Root cause: when a paper is already in `seen`, the server was appending
parent_id to all_papers[key]['parents'] without verifying that the parent
is from the immediately preceding generation.  This allowed same-generation
edges into the lineage DAG, which the SPC greedy walk could then traverse
sideways instead of crossing generations.

Two-layer fix:
1. server.py: generation-adjacency guard before appending parent_id
2. utils.py compute_main_path: defensive filter — skip edges where
   parent_gen != child_gen - 1 when both generations are known
"""

import unittest
from scopus_mcp.utils import compute_main_path


def _make_records(edges, gen_map, seed_key='seed'):
    """
    Build a minimal list of record dicts suitable for compute_main_path.

    edges      : list of (parent_key, child_key) tuples
    gen_map    : dict mapping key → generation number
    seed_key   : the node with generation 0 (no parents)
    """
    # Collect parents per node
    parents_of = {k: [] for k in gen_map}
    for parent, child in edges:
        parents_of.setdefault(child, []).append(parent)

    records = []
    for k, gen in gen_map.items():
        records.append({
            'scopus_id': k,
            'generation': gen,
            'parents': parents_of.get(k, []),
            'title': f'Paper {k}',
        })
    return records


class TestSameGenEdgesFiltered(unittest.TestCase):
    """compute_main_path must not include same-generation edges in the result."""

    def _records_with_same_gen_edge(self):
        """
        Topology (backward walk scenario):
          gen0: A  (seed)
          gen1: B, C  (A's references)
          gen2: D     (B's and C's common reference)

        Injected same-gen edge: B → C (gen1 → gen1).
        Without the fix this would show up in the SPC and could become
        part of the main path.
        """
        records = [
            {'scopus_id': 'A', 'generation': 0, 'parents': [],          'title': 'Seed'},
            {'scopus_id': 'B', 'generation': 1, 'parents': ['A'],        'title': 'B'},
            # C has both a legitimate gen0 parent A and an injected same-gen parent B
            {'scopus_id': 'C', 'generation': 1, 'parents': ['A', 'B'],   'title': 'C'},
            {'scopus_id': 'D', 'generation': 2, 'parents': ['B', 'C'],   'title': 'D'},
        ]
        return records

    def test_same_gen_edge_not_in_edges(self):
        """B→C must not appear in the returned edge set."""
        result = compute_main_path(self._records_with_same_gen_edge())
        edge_pairs = [(e['source'], e['target']) for e in result['edges']]
        self.assertNotIn(('B', 'C'), edge_pairs)

    def test_main_path_no_within_gen_step(self):
        """Every consecutive pair in the main path must span exactly one generation."""
        records = self._records_with_same_gen_edge()
        node_gen = {r['scopus_id']: r['generation'] for r in records}
        result = compute_main_path(records)
        path = result['main_path']
        self.assertGreater(len(path), 0)
        for u, v in zip(path, path[1:]):
            u_gen = node_gen.get(u)
            v_gen = node_gen.get(v)
            self.assertIsNotNone(u_gen)
            self.assertIsNotNone(v_gen)
            self.assertEqual(
                v_gen - u_gen, 1,
                f"Non-adjacent step in main path: {u}(gen{u_gen}) → {v}(gen{v_gen})"
            )

    def test_all_returned_edges_cross_generations(self):
        """No returned edge may be within the same generation."""
        records = self._records_with_same_gen_edge()
        node_gen = {r['scopus_id']: r['generation'] for r in records}
        result = compute_main_path(records)
        for e in result['edges']:
            u, v = e['source'], e['target']
            u_gen = node_gen.get(u)
            v_gen = node_gen.get(v)
            if u_gen is not None and v_gen is not None:
                self.assertEqual(
                    v_gen - u_gen, 1,
                    f"Same-gen edge leaked into result: {u}(gen{u_gen}) → {v}(gen{v_gen})"
                )


class TestNodeAtTwoDepthsGetsShallower(unittest.TestCase):
    """
    If a paper can be reached at gen1 or gen2, its generation should be the
    first (shallowest) one assigned — same-gen edges from deeper traversal
    must not pollute its parents list.

    Topology:
      gen0: A (seed)
      gen1: B  (A's reference)
      gen1: C  (also A's reference)
      gen2: D  (B's reference)

    The bug scenario: D is also C's reference, but C is gen1.
    Without the fix, C (gen1) would be added as a parent of D even though
    D is also gen1 in some discovery order.  In our controlled test we can
    encode this directly in the parents list.
    """

    def test_main_path_crosses_generations(self):
        records = [
            {'scopus_id': 'A', 'generation': 0, 'parents': [],           'title': 'A'},
            {'scopus_id': 'B', 'generation': 1, 'parents': ['A'],         'title': 'B'},
            {'scopus_id': 'C', 'generation': 1, 'parents': ['A'],         'title': 'C'},
            # D is gen2 but has a spurious gen1 parent C injected
            {'scopus_id': 'D', 'generation': 2, 'parents': ['B', 'C'],    'title': 'D'},
        ]
        node_gen = {r['scopus_id']: r['generation'] for r in records}
        result = compute_main_path(records)
        path = result['main_path']
        self.assertGreater(len(path), 0)
        for u, v in zip(path, path[1:]):
            self.assertEqual(
                node_gen[v] - node_gen[u], 1,
                f"Main path step not adjacent: {u}→{v}"
            )


class TestLegitimateMultiParentPreserved(unittest.TestCase):
    """
    A child that has two legitimate (adjacent-generation) parents must
    still receive both edges after the fix.

      gen0: A
      gen1: B, C  (both children of A)
      gen2: D     (child of both B and C — diamond)
    """

    def test_diamond_edges_present(self):
        records = [
            {'scopus_id': 'A', 'generation': 0, 'parents': [],         'title': 'A'},
            {'scopus_id': 'B', 'generation': 1, 'parents': ['A'],       'title': 'B'},
            {'scopus_id': 'C', 'generation': 1, 'parents': ['A'],       'title': 'C'},
            {'scopus_id': 'D', 'generation': 2, 'parents': ['B', 'C'],  'title': 'D'},
        ]
        result = compute_main_path(records)
        edge_pairs = {(e['source'], e['target']) for e in result['edges']}
        self.assertIn(('B', 'D'), edge_pairs)
        self.assertIn(('C', 'D'), edge_pairs)

    def test_diamond_path_length(self):
        records = [
            {'scopus_id': 'A', 'generation': 0, 'parents': [],         'title': 'A'},
            {'scopus_id': 'B', 'generation': 1, 'parents': ['A'],       'title': 'B'},
            {'scopus_id': 'C', 'generation': 1, 'parents': ['A'],       'title': 'C'},
            {'scopus_id': 'D', 'generation': 2, 'parents': ['B', 'C'],  'title': 'D'},
        ]
        result = compute_main_path(records)
        # Main path should be 3 nodes: A → (B or C) → D
        self.assertEqual(len(result['main_path']), 3)


class TestSingleNodeNoEdges(unittest.TestCase):
    """Single-node lineage must not crash and must return empty path/edges."""

    def test_single_node(self):
        records = [{'scopus_id': 'A', 'generation': 0, 'parents': [], 'title': 'A'}]
        result = compute_main_path(records)
        self.assertEqual(result['edges'], [])
        self.assertEqual(result['main_path'], [])


class TestForwardRegressionGuard(unittest.TestCase):
    """
    Forward walk (citing papers): gen0 = seed, gen1 = papers that cite seed.
    Edges run gen0→gen1→gen2.  Same-gen edges must be filtered here too.

    Encode a same-gen edge: gen1 paper X also appears as a parent of gen1
    paper Y (X cites Y, both are gen1).
    """

    def test_forward_same_gen_edge_filtered(self):
        records = [
            {'scopus_id': 'S', 'generation': 0, 'parents': [],          'title': 'Seed'},
            {'scopus_id': 'X', 'generation': 1, 'parents': ['S'],        'title': 'X'},
            # Y has both S (gen0, legitimate) and X (gen1, injected same-gen)
            {'scopus_id': 'Y', 'generation': 1, 'parents': ['S', 'X'],   'title': 'Y'},
            {'scopus_id': 'Z', 'generation': 2, 'parents': ['X', 'Y'],   'title': 'Z'},
        ]
        result = compute_main_path(records)
        edge_pairs = [(e['source'], e['target']) for e in result['edges']]
        self.assertNotIn(('X', 'Y'), edge_pairs, "Forward same-gen edge X→Y should be filtered")

    def test_forward_main_path_strictly_crosses_gen(self):
        records = [
            {'scopus_id': 'S', 'generation': 0, 'parents': [],          'title': 'Seed'},
            {'scopus_id': 'X', 'generation': 1, 'parents': ['S'],        'title': 'X'},
            {'scopus_id': 'Y', 'generation': 1, 'parents': ['S', 'X'],   'title': 'Y'},
            {'scopus_id': 'Z', 'generation': 2, 'parents': ['X', 'Y'],   'title': 'Z'},
        ]
        node_gen = {r['scopus_id']: r['generation'] for r in records}
        result = compute_main_path(records)
        path = result['main_path']
        self.assertGreater(len(path), 0)
        for u, v in zip(path, path[1:]):
            self.assertEqual(
                node_gen[v] - node_gen[u], 1,
                f"Forward main path non-adjacent: {u}(gen{node_gen[u]}) → {v}(gen{node_gen[v]})"
            )


if __name__ == '__main__':
    unittest.main()
