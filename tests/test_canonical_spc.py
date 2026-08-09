"""
Canonical SPC validation — Batagelj (2003) / Wikipedia worked example.

This file encodes the complete 17-edge graph from the Batagelj/Wikipedia
figure and asserts that compute_main_path reproduces every published edge-weight
label exactly.  This is the ground-truth regression suite for the canonical SPC
implementation: all 17 SPC weights match the figure, the global main path is
B→D→F→I→M→N with edge-weight sum = 15, and the Kirchhoff flow-conservation
property holds at every branch node.

Graph edges (cited→citing) with figure-labeled SPC weights:
  A→C=2, B→C=2, B→D=5, B→J=1,
  C→E=2, C→H=2,
  E→G=2, G→H=2,
  D→F=3, D→I=2,
  F→H=1, F→I=2,
  I→L=2, I→M=2, J→M=1,
  M→N=3,
  H→K=5

Sources: {A, B}.  Sinks: {K, L, N}.

Longest-path-from-source generation layering (assigns generations by the
longest path from any source to each node — this produces multi-generation-
spanning edges such as C(1)→H(4), D(1)→I(3), F(2)→H(4), J(1)→M(4), which
exercise and confirm the Bug-1 fix: the same-gen-only edge filter passes all
legitimate edges regardless of the generation distance they span):
  A=0, B=0
  C=1, D=1, J=1
  E=2, F=2
  G=3, I=3
  H=4, L=4, M=4
  K=5, N=5
"""

import unittest
from scopus_mcp.utils import compute_main_path

# ---------------------------------------------------------------------------
# Canonical graph construction
# ---------------------------------------------------------------------------

# All 17 edges as (parent, child) — direction used by the 'parents' field
_EDGES = [
    ('A', 'C'), ('B', 'C'), ('B', 'D'), ('B', 'J'),
    ('C', 'E'), ('C', 'H'),
    ('E', 'G'), ('G', 'H'),
    ('D', 'F'), ('D', 'I'),
    ('F', 'H'), ('F', 'I'),
    ('I', 'L'), ('I', 'M'), ('J', 'M'),
    ('M', 'N'),
    ('H', 'K'),
]

# Published SPC weight for each edge (figure ground truth)
_EXPECTED_SPC = {
    ('A', 'C'): 2, ('B', 'C'): 2, ('B', 'D'): 5, ('B', 'J'): 1,
    ('C', 'E'): 2, ('C', 'H'): 2,
    ('E', 'G'): 2, ('G', 'H'): 2,
    ('D', 'F'): 3, ('D', 'I'): 2,
    ('F', 'H'): 1, ('F', 'I'): 2,
    ('I', 'L'): 2, ('I', 'M'): 2, ('J', 'M'): 1,
    ('M', 'N'): 3,
    ('H', 'K'): 5,
}

# Longest-path generation for each node
_GENERATION = {
    'A': 0, 'B': 0,
    'C': 1, 'D': 1, 'J': 1,
    'E': 2, 'F': 2,
    'G': 3, 'I': 3,
    'H': 4, 'L': 4, 'M': 4,
    'K': 5, 'N': 5,
}


def _build_records():
    """Build records in the server format used by compute_main_path."""
    parents_of = {n: [] for n in _GENERATION}
    for p, c in _EDGES:
        parents_of[c].append(p)

    return [
        {
            'scopus_id': node,
            'generation': gen,
            'parents': parents_of[node],
            'title': node,
        }
        for node, gen in _GENERATION.items()
    ]


_RECORDS = _build_records()


def _run():
    return compute_main_path(_RECORDS)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestCanonicalEdgesPresent(unittest.TestCase):
    """All 17 edges must appear in the output — guards the Bug-1 regression."""

    def test_all_17_edges_present(self):
        result = _run()
        edge_pairs = {(e['source'], e['target']) for e in result['edges']}
        for p, c in _EDGES:
            self.assertIn(
                (p, c), edge_pairs,
                f"Edge ({p},{c}) missing from output — same-gen-only filter incorrectly deleted it"
            )

    def test_exactly_17_edges(self):
        result = _run()
        self.assertEqual(len(result['edges']), 17,
            f"Expected 17 edges, got {len(result['edges'])}")


class TestCanonicalSPCValues(unittest.TestCase):
    """Every edge's spc_weight must match the published Batagelj/Wikipedia figure label.

    The implementation reproduces the canonical Batagelj (2003) / Wikipedia
    worked example exactly: all 17 edge weights match the figure, confirming
    that the pseudo-terminal SPC algorithm is correct.
    """

    def _spc_map(self):
        result = _run()
        return {(e['source'], e['target']): e['spc_weight'] for e in result['edges']}

    def test_all_17_edge_weights_match_figure(self):
        """Assert every one of the 17 published SPC weights explicitly."""
        spc = self._spc_map()
        for (u, v), expected in _EXPECTED_SPC.items():
            actual = spc.get((u, v))
            self.assertEqual(
                actual, expected,
                f"spc({u},{v}): expected {expected}, got {actual}"
            )

    def test_bd_equals_5(self):
        """Primary canonical anchor: spc(B,D) == 5 (Batagelj 2003 / Wikipedia)."""
        spc = self._spc_map()
        self.assertEqual(spc.get(('B', 'D')), 5)

    def test_hk_equals_5(self):
        """H→K is the convergence node: spc(H,K) == 5 (n_minus[H]=5, n_plus[K]=1)."""
        spc = self._spc_map()
        self.assertEqual(spc.get(('H', 'K')), 5)

    def test_mn_equals_3(self):
        """M→N: n_minus[M]=3 (paths from A,B through I and J), n_plus[N]=1. spc=3."""
        spc = self._spc_map()
        self.assertEqual(spc.get(('M', 'N')), 3)

    def test_kirchhoff_node_c(self):
        """C: in = spc(A,C)+spc(B,C) = 2+2=4; out = spc(C,E)+spc(C,H) = 2+2=4."""
        spc = self._spc_map()
        in_sum  = spc[('A','C')] + spc[('B','C')]
        out_sum = spc[('C','E')] + spc[('C','H')]
        self.assertEqual(in_sum, out_sum,
            f"Kirchhoff violated at C: in={in_sum}, out={out_sum}")

    def test_kirchhoff_node_d(self):
        """D: in = spc(B,D)=5; out = spc(D,F)+spc(D,I) = 3+2=5."""
        spc = self._spc_map()
        in_sum  = spc[('B','D')]
        out_sum = spc[('D','F')] + spc[('D','I')]
        self.assertEqual(in_sum, out_sum,
            f"Kirchhoff violated at D: in={in_sum}, out={out_sum}")

    def test_kirchhoff_node_f(self):
        """F: in = spc(D,F)=3; out = spc(F,H)+spc(F,I) = 1+2=3."""
        spc = self._spc_map()
        in_sum  = spc[('D','F')]
        out_sum = spc[('F','H')] + spc[('F','I')]
        self.assertEqual(in_sum, out_sum,
            f"Kirchhoff violated at F: in={in_sum}, out={out_sum}")

    def test_kirchhoff_node_i(self):
        """I: in = spc(D,I)+spc(F,I) = 2+2=4; out = spc(I,L)+spc(I,M) = 2+2=4."""
        spc = self._spc_map()
        in_sum  = spc[('D','I')] + spc[('F','I')]
        out_sum = spc[('I','L')] + spc[('I','M')]
        self.assertEqual(in_sum, out_sum,
            f"Kirchhoff violated at I: in={in_sum}, out={out_sum}")

    def test_kirchhoff_node_h(self):
        """H: in = spc(C,H)+spc(G,H)+spc(F,H) = 2+2+1=5; out = spc(H,K)=5."""
        spc = self._spc_map()
        in_sum  = spc[('C','H')] + spc[('G','H')] + spc[('F','H')]
        out_sum = spc[('H','K')]
        self.assertEqual(in_sum, out_sum,
            f"Kirchhoff violated at H: in={in_sum}, out={out_sum}")


class TestGlobalMainPath(unittest.TestCase):
    """Canonical global main path: B→D→F→I→M→N with edge-weight sum = 15."""

    def test_global_path_equals_canonical(self):
        result = _run()
        self.assertEqual(
            result['global_main_path'],
            ['B', 'D', 'F', 'I', 'M', 'N'],
            f"Global main path mismatch: {result['global_main_path']}"
        )

    def test_global_path_sum_equals_15(self):
        """spc(B,D)+spc(D,F)+spc(F,I)+spc(I,M)+spc(M,N) = 5+3+2+2+3 = 15."""
        result = _run()
        spc = {(e['source'], e['target']): e['spc_weight'] for e in result['edges']}
        path = result['global_main_path']
        total = sum(spc.get((path[i], path[i+1]), 0) for i in range(len(path)-1))
        self.assertEqual(total, 15,
            f"Global path weight sum = {total}, expected 15. Path: {path}")

    def test_global_main_path_key_present(self):
        result = _run()
        self.assertIn('global_main_path', result)


class TestSameGenEdgeStillFiltered(unittest.TestCase):
    """v0.7.2 backward-bug invariant: same-generation edges are still excluded."""

    def test_same_gen_edge_not_in_dag(self):
        """Inject a gen-1→gen-1 edge; it must not appear in SPC output."""
        records = [
            {'scopus_id': 'A', 'generation': 0, 'parents': [],            'title': 'A'},
            {'scopus_id': 'B', 'generation': 1, 'parents': ['A'],          'title': 'B'},
            # C has legitimate parent A (gen0) plus injected same-gen parent B (gen1)
            {'scopus_id': 'C', 'generation': 1, 'parents': ['A', 'B'],     'title': 'C'},
            {'scopus_id': 'D', 'generation': 2, 'parents': ['B', 'C'],     'title': 'D'},
        ]
        result = compute_main_path(records)
        edge_pairs = [(e['source'], e['target']) for e in result['edges']]
        self.assertNotIn(('B', 'C'), edge_pairs,
            "Same-generation edge B→C (gen1→gen1) must be filtered")

    def test_multi_gen_spanning_edge_preserved(self):
        """An edge that spans more than 1 generation layer must NOT be removed."""
        # B (gen0) → D (gen2): skips gen1 layer — legitimate in a DAG
        records = [
            {'scopus_id': 'A', 'generation': 0, 'parents': [],         'title': 'A'},
            {'scopus_id': 'B', 'generation': 0, 'parents': [],         'title': 'B'},
            {'scopus_id': 'C', 'generation': 1, 'parents': ['A'],      'title': 'C'},
            {'scopus_id': 'D', 'generation': 2, 'parents': ['B', 'C'], 'title': 'D'},
        ]
        result = compute_main_path(records)
        edge_pairs = [(e['source'], e['target']) for e in result['edges']]
        self.assertIn(('B', 'D'), edge_pairs,
            "Multi-layer-spanning edge B(gen0)→D(gen2) must be preserved")


if __name__ == '__main__':
    unittest.main(verbosity=2)
