import csv
import json
import logging
import math
import os
import re
import xml.etree.ElementTree as ET
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Any, Optional, Set

import httpx

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Output-file helpers (reusable by any tool that returns large result sets)
# ---------------------------------------------------------------------------

CSV_COLUMNS = [
    'scopus_id', 'title', 'creator', 'publication_name',
    'cover_date', 'doi', 'cited_by_count', 'aggregation_type', 'url',
]

# Inline-vs-file threshold: result sets larger than this are written to disk.
SEARCH_ALL_INLINE_THRESHOLD = 50


def _output_dir() -> Path:
    """Resolve the directory for large result dumps.

    Order: SCOPUS_MCP_OUTPUT_DIR env var → ~/scopus-mcp-output.
    Creates the directory (and parents) if absent.
    """
    d = os.environ.get('SCOPUS_MCP_OUTPUT_DIR')
    path = Path(d).expanduser() if d else Path.home() / 'scopus-mcp-output'
    path.mkdir(parents=True, exist_ok=True)
    return path


def _query_slug(query: str) -> str:
    """Derive a short, filesystem-safe slug from a Scopus query string."""
    slug = re.sub(r'[^\w\s-]', '', query.lower())
    slug = re.sub(r'[\s_-]+', '-', slug).strip('-')
    return slug[:50] or 'query'


def write_fulltext_to_disk(doi: str, text: str) -> str:
    """Write retrieved full text to disk; return absolute path.

    Filename: fulltext-<doi-slug>.txt under SCOPUS_MCP_OUTPUT_DIR.
    """
    out = _output_dir()
    slug = re.sub(r'[^\w-]', '-', doi)[:60].strip('-')
    path = out / f'fulltext-{slug}.txt'
    path.write_text(text, encoding='utf-8')
    return str(path)


def _extract_pdf_text(content: bytes) -> Optional[str]:
    """Extract plain text from PDF bytes via pymupdf (fitz)."""
    try:
        import fitz  # pymupdf
        with fitz.open(stream=content, filetype='pdf') as doc:
            parts = [page.get_text() for page in doc]
        text = '\n'.join(parts).strip()
        return text or None
    except Exception as exc:
        logger.warning(f'PDF text extraction failed: {exc}')
        return None


def _extract_html_text(html: str) -> Optional[str]:
    """Extract readable text from HTML via stdlib html.parser."""
    from html.parser import HTMLParser

    class _Extractor(HTMLParser):
        def __init__(self):
            super().__init__()
            self._chunks: List[str] = []
            self._skip = 0

        def handle_starttag(self, tag, attrs):
            if tag in ('script', 'style', 'nav', 'header', 'footer', 'aside'):
                self._skip += 1

        def handle_endtag(self, tag):
            if tag in ('script', 'style', 'nav', 'header', 'footer', 'aside'):
                self._skip = max(0, self._skip - 1)

        def handle_data(self, data):
            if not self._skip:
                stripped = data.strip()
                if stripped:
                    self._chunks.append(stripped)

    parser = _Extractor()
    try:
        parser.feed(html)
    except Exception:
        pass
    text = ' '.join(parser._chunks).strip()
    return text or None


async def fetch_oa_fulltext(doi: str) -> Dict[str, Any]:
    """Query OpenAlex for an OA copy of *doi*, fetch it, and extract text.

    Returns a dict with keys:
      text       – extracted plain text, or None if unavailable
      source_url – the OA URL attempted (or None if no OA copy found)

    Never raises; failures are logged and surfaced as text=None.
    """
    POLITE_HEADERS = {
        'User-Agent': 'ScopusMCP/0.7.0 (mailto:hron@hey.com)',
        'Accept': '*/*',
    }

    oa_url: Optional[str] = None

    # 1. OpenAlex lookup
    try:
        async with httpx.AsyncClient(timeout=20.0, follow_redirects=True) as http:
            r = await http.get(
                f'https://api.openalex.org/works/doi:{doi}',
                params={'mailto': 'hron@hey.com'},
                headers=POLITE_HEADERS,
            )
        if r.status_code == 200:
            data = r.json()
            oa = data.get('open_access') or {}
            if oa.get('is_oa'):
                oa_url = (
                    oa.get('oa_url')
                    or (data.get('best_oa_location') or {}).get('pdf_url')
                    or (data.get('best_oa_location') or {}).get('url')
                )
    except Exception as exc:
        logger.warning(f'OpenAlex lookup failed for doi={doi}: {exc}')

    if not oa_url:
        return {'text': None, 'source_url': None}

    # 2. Fetch OA URL and extract text
    try:
        async with httpx.AsyncClient(timeout=60.0, follow_redirects=True) as http:
            r = await http.get(oa_url, headers=POLITE_HEADERS)
        r.raise_for_status()
        content_type = r.headers.get('content-type', '').lower()
        if 'pdf' in content_type or oa_url.lower().endswith('.pdf'):
            text = _extract_pdf_text(r.content)
        else:
            text = _extract_html_text(r.text)
        return {'text': text, 'source_url': oa_url}
    except Exception as exc:
        logger.warning(f'OA fetch/extract failed for doi={doi} url={oa_url}: {exc}')
        return {'text': None, 'source_url': oa_url}


def write_results_to_disk(records: List[Dict[str, Any]], query: str) -> Dict[str, str]:
    """Write cleaned records to JSON and CSV files; return their absolute paths.

    Output directory: SCOPUS_MCP_OUTPUT_DIR if set, else ~/scopus-mcp-output.
    CSV columns: scopus_id, title, creator, publication_name, cover_date,
                 doi, cited_by_count, aggregation_type, url.
    """
    out = _output_dir()
    ts = datetime.now().strftime('%Y%m%dT%H%M%S')
    base = f'scopus-{_query_slug(query)}-{ts}'

    json_path = out / f'{base}.json'
    csv_path = out / f'{base}.csv'

    json_path.write_text(
        json.dumps(records, ensure_ascii=False, indent=2),
        encoding='utf-8',
    )
    with csv_path.open('w', newline='', encoding='utf-8') as f:
        writer = csv.DictWriter(f, fieldnames=CSV_COLUMNS, extrasaction='ignore')
        writer.writeheader()
        writer.writerows(records)

    return {'json_path': str(json_path), 'csv_path': str(csv_path)}


def should_write_to_disk(
    records: List[Dict[str, Any]],
    threshold: int = SEARCH_ALL_INLINE_THRESHOLD,
) -> bool:
    """Return True when the result count exceeds the inline-return threshold."""
    return len(records) > threshold


# ---------------------------------------------------------------------------
# Citation-network graph helpers
# ---------------------------------------------------------------------------

EDGE_CSV_COLUMNS = ['source', 'target', 'weight', 'cosine']


def _make_node_label(meta: Dict[str, Any], node_id: str = '') -> str:
    """Derive a short readable label for a graph node.

    Priority: 'Surname YYYY' (from ce:indexed-name + year) → title[:40] → node_id.
    """
    creator = (meta.get('creator') or '').strip()
    year = (meta.get('year') or '').strip()
    if creator:
        # ce:indexed-name is 'Surname I.' — first token is the surname
        surname = creator.split()[0].rstrip('.,')
        return f'{surname} {year}' if year else surname
    title = (meta.get('title') or '').strip()
    if title:
        return title[:40]
    return node_id or str(meta.get('id', ''))


def compute_pairwise_edges(
    seed_sets: Dict[str, Set[str]],
    min_shared: int = 2,
) -> List[Dict[str, Any]]:
    """Compute pairwise overlap between seed sets; return a weighted edge list.

    Args:
        seed_sets: mapping of seed_id → set of reference or citing-paper IDs.
                   Seeds with empty sets are silently skipped.
        min_shared: minimum shared items required for an edge to be emitted.

    Returns:
        List of {'source', 'target', 'weight' (int), 'cosine' (float)},
        sorted by weight descending. Each unordered pair (a, b) appears once.
    """
    seeds = [s for s, refs in seed_sets.items() if refs]
    edges = []
    for i in range(len(seeds)):
        for j in range(i + 1, len(seeds)):
            a, b = seeds[i], seeds[j]
            set_a, set_b = seed_sets[a], seed_sets[b]
            shared = len(set_a & set_b)
            if shared < min_shared:
                continue
            denom = math.sqrt(len(set_a) * len(set_b))
            cosine = round(shared / denom, 6) if denom > 0 else 0.0
            edges.append({'source': a, 'target': b, 'weight': shared, 'cosine': cosine})
    edges.sort(key=lambda e: e['weight'], reverse=True)
    return edges


def write_graph_to_disk(
    nodes: List[Dict[str, Any]],
    edges: List[Dict[str, Any]],
    slug: str,
) -> Dict[str, str]:
    """Write graph data to GraphML + CSV edge list + PNG; return their absolute paths.

    Node dicts: {id, label, title, creator, year, venue} — all except id may be None.
    Edge dicts: {source, target, weight, cosine}

    GraphML carries explicit <key> declarations (label, title, creator, year, venue)
    so it opens cleanly in Gephi and VOSviewer. PNG is rendered via render_graph_png
    alongside the other files; render failures are non-fatal (png_path=None in result).
    Output directory follows the SCOPUS_MCP_OUTPUT_DIR convention.
    """
    out = _output_dir()
    ts = datetime.now().strftime('%Y%m%dT%H%M%S')
    base = f'scopus-{_query_slug(slug)}-{ts}'

    graphml_path = out / f'{base}.graphml'
    csv_path = out / f'{base}-edges.csv'

    # ---- GraphML ----
    root = ET.Element('graphml', {
        'xmlns': 'http://graphml.graphdrawing.org/graphml',
        'xmlns:xsi': 'http://www.w3.org/2001/XMLSchema-instance',
        'xsi:schemaLocation': (
            'http://graphml.graphdrawing.org/graphml '
            'http://graphml.graphdrawing.org/graphml/graphml.xsd'
        ),
    })
    for kid, fname, ffor, ftype in [
        ('d_label',   'label',   'node', 'string'),
        ('d_title',   'title',   'node', 'string'),
        ('d_creator', 'creator', 'node', 'string'),
        ('d_year',    'year',    'node', 'string'),
        ('d_venue',   'venue',   'node', 'string'),
        ('d_weight',  'weight',  'edge', 'double'),
        ('d_cosine',  'cosine',  'edge', 'double'),
    ]:
        ET.SubElement(root, 'key', {
            'id': kid, 'for': ffor,
            'attr.name': fname, 'attr.type': ftype,
        })
    graph_el = ET.SubElement(root, 'graph', {'id': 'G', 'edgedefault': 'undirected'})
    for node in nodes:
        n_el = ET.SubElement(graph_el, 'node', {'id': str(node['id'])})
        for key_id, field in [
            ('d_label', 'label'), ('d_title', 'title'), ('d_creator', 'creator'),
            ('d_year', 'year'), ('d_venue', 'venue'),
        ]:
            val = node.get(field)
            if val is not None:
                d = ET.SubElement(n_el, 'data', {'key': key_id})
                d.text = str(val)
    for idx, edge in enumerate(edges):
        e_el = ET.SubElement(graph_el, 'edge', {
            'id': f'e{idx}',
            'source': str(edge['source']),
            'target': str(edge['target']),
        })
        dw = ET.SubElement(e_el, 'data', {'key': 'd_weight'})
        dw.text = str(edge['weight'])
        dc = ET.SubElement(e_el, 'data', {'key': 'd_cosine'})
        dc.text = str(edge['cosine'])

    tree = ET.ElementTree(root)
    ET.indent(tree, space='  ')
    with graphml_path.open('wb') as f:
        tree.write(f, encoding='utf-8', xml_declaration=True)

    # ---- CSV edge list ----
    with csv_path.open('w', newline='', encoding='utf-8') as f:
        writer = csv.DictWriter(f, fieldnames=EDGE_CSV_COLUMNS, extrasaction='ignore')
        writer.writeheader()
        writer.writerows(edges)

    # ---- PNG (non-fatal) ----
    png_path = None
    try:
        png_path = render_graph_png(nodes, edges, base)
    except Exception as exc:
        logger.warning(f'write_graph_to_disk: render_graph_png non-fatal error: {exc}')

    return {
        'graphml_path': str(graphml_path),
        'csv_path': str(csv_path),
        'png_path': png_path,
    }


def render_graph_png(
    nodes: List[Dict[str, Any]],
    edges: List[Dict[str, Any]],
    base_filename: str,
) -> Optional[str]:
    """Render the graph to PNG via networkx + matplotlib (Agg backend).

    Returns the absolute path to the PNG, or None if the graph is empty or
    rendering fails for any reason. Never raises — errors are logged and
    the caller continues with GraphML/CSV as the primary artifacts.
    """
    try:
        import matplotlib
        matplotlib.use('Agg')
        import matplotlib.pyplot as plt
        import networkx as nx

        out = _output_dir()
        png_path = out / f'{base_filename}.png'

        G = nx.Graph()
        for node in nodes:
            G.add_node(str(node['id']), label=node.get('label', str(node['id'])))
        for edge in edges:
            G.add_edge(
                str(edge['source']), str(edge['target']),
                weight=float(edge.get('weight', 1)),
            )

        if len(G.nodes) == 0:
            return None

        fig, ax = plt.subplots(figsize=(12, 8))
        pos = nx.spring_layout(G, seed=42, k=2.0 / math.sqrt(max(len(G.nodes), 1)))

        degrees = dict(G.degree())
        node_sizes = [max(200, 150 * degrees.get(n, 1)) for n in G.nodes]

        weights = [G[u][v].get('weight', 1) for u, v in G.edges]
        max_w = max(weights) if weights else 1
        edge_widths = [0.5 + 3.5 * w / max_w for w in weights]

        nx.draw_networkx_nodes(G, pos, node_size=node_sizes, ax=ax, alpha=0.85)
        nx.draw_networkx_edges(G, pos, width=edge_widths, ax=ax, alpha=0.5)
        nx.draw_networkx_labels(
            G, pos,
            labels={n: G.nodes[n]['label'] for n in G.nodes},
            ax=ax, font_size=7,
        )

        ax.set_axis_off()
        plt.tight_layout()
        plt.savefig(str(png_path), dpi=150, bbox_inches='tight')
        plt.close(fig)

        return str(png_path)
    except Exception as exc:
        logger.warning(f'render_graph_png failed (non-fatal): {exc}')
        return None


def write_lineage_to_disk(
    records: List[Dict[str, Any]],
    seed_id: str,
    main_path: Optional[List[str]] = None,
    spc_edges: Optional[List[Dict[str, Any]]] = None,
    base_filename: Optional[str] = None,
) -> str:
    """Write citation lineage corpus to JSON; return absolute path.

    Writes a dict with keys: seed_id, records, main_path, spc_edges.
    Output directory follows the SCOPUS_MCP_OUTPUT_DIR convention.
    """
    out = _output_dir()
    if base_filename:
        json_path = out / f'{base_filename}.json'
    else:
        ts = datetime.now().strftime('%Y%m%dT%H%M%S')
        slug = _query_slug(f'lineage-{seed_id}')
        json_path = out / f'scopus-{slug}-{ts}.json'
    payload = {
        'seed_id': seed_id,
        'records': records,
        'main_path': main_path or [],
        'spc_edges': spc_edges or [],
    }
    json_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding='utf-8',
    )
    return str(json_path)


# ---------------------------------------------------------------------------
# Main-path analysis (Hummon & Doreian SPC)
# ---------------------------------------------------------------------------

def _rec_key(r: Dict[str, Any]) -> Optional[str]:
    """Stable key for a lineage record: scopus_id if present, else doi:…"""
    sid = r.get('scopus_id')
    if sid:
        return sid
    doi = r.get('doi')
    if doi:
        return f'doi:{doi}'
    return None


def compute_main_path(records: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Compute canonical Batagelj (2003) SPC weights and the main path.

    Builds a directed acyclic graph from the ``parents`` field of each record
    (edge direction: parent → child).  Uses Kahn's topological sort so cycles
    are silently excluded rather than crashing.

    SPC is computed with canonical pseudo-terminals (Batagelj 2003 / Hummon &
    Doreian 1989):
      * A virtual pseudo-source ``s`` is connected to every real source (in-degree 0).
      * A virtual pseudo-sink ``t`` is connected from every real sink (out-degree 0).
      * ``n_minus[v]`` = paths from ``s`` to ``v`` (forward DP over topo order).
      * ``n_plus[v]``  = paths from ``v`` to ``t`` (backward DP).
      * ``spc(u,v)``   = ``n_minus[u] * n_plus[v]``.

    This ensures correct weighting on multi-source / multi-sink graphs.

    Edge filtering: skip an edge when ``parent_gen == child_gen`` (both generation
    values known and equal).  Same-generation edges are invalid in a lineage DAG
    and arise from the backward-walk server bug (a gen-N paper may reference another
    gen-N paper).  Edges that span MORE than one generation are legitimate — a
    node's depth depends on its longest/shortest path from the seed, so direct
    edges can skip generation layers.

    Returns a dict:
    {
        'edges':            [{'source': id, 'target': id, 'spc_weight': int}, …],
        'main_path':        [id, …],   # greedy local (start from best source,
                                       # follow max-weight edge)
        'global_main_path': [id, …],  # canonical global: max-sum-weight path
                                       # from any source to any sink via DP
        'note':             str | None,
    }
    """
    # Build node index
    node_map: Dict[str, Dict] = {}
    for r in records:
        k = _rec_key(r)
        if k and k not in node_map:
            node_map[k] = r

    if not node_map:
        return {'edges': [], 'main_path': [], 'global_main_path': [], 'note': 'No nodes in lineage.'}

    # Build adjacency (succ / pred maps)
    succ: Dict[str, List[str]] = {k: [] for k in node_map}
    pred: Dict[str, List[str]] = {k: [] for k in node_map}

    for r in records:
        child = _rec_key(r)
        if not child or child not in node_map:
            continue
        child_gen = node_map[child].get('generation')
        for parent in (r.get('parents') or []):
            if parent in node_map and parent != child:
                parent_gen = node_map[parent].get('generation')
                # Skip same-generation edges only (not multi-generation-spanning edges).
                # A gen-N paper's references can include other gen-N papers (backward-walk
                # bug), producing invalid same-gen edges.  Edges spanning more than one
                # generation are legitimate (a node's depth is longest/shortest-path depth
                # from the seed, so direct edges can cross multiple layer boundaries).
                if child_gen is not None and parent_gen is not None:
                    if parent_gen == child_gen:
                        continue
                if child not in succ[parent]:
                    succ[parent].append(child)
                if parent not in pred[child]:
                    pred[child].append(parent)

    # Kahn's topological sort (cycle-safe — nodes in cycles are silently excluded)
    in_degree = {k: len(pred[k]) for k in node_map}
    queue: deque = deque([k for k in node_map if in_degree[k] == 0])
    topo: List[str] = []
    while queue:
        node = queue.popleft()
        topo.append(node)
        for v in succ[node]:
            in_degree[v] -= 1
            if in_degree[v] == 0:
                queue.append(v)

    topo_set = set(topo)

    # Collect valid edges (only between topo nodes, deduped)
    valid_edges: List[tuple] = []
    seen_e: set = set()
    for u in topo:
        for v in succ[u]:
            if v in topo_set and (u, v) not in seen_e:
                seen_e.add((u, v))
                valid_edges.append((u, v))

    if not valid_edges:
        return {'edges': [], 'main_path': [], 'global_main_path': [], 'note': 'No edges in lineage graph.'}

    # Canonical SPC (Batagelj 2003) via pseudo-terminals
    # ------------------------------------------------------------------
    # Identify real sources (no in-edges within topo_set) and real sinks
    real_sources = [k for k in topo if not any(p in topo_set for p in pred[k])]
    real_sinks   = [k for k in topo if not any(s in topo_set for s in succ[k])]

    # n_minus[v] = # distinct paths from pseudo-source to v
    # Forward pass: pseudo-source → real sources (each counts 1)
    n_minus: Dict[str, int] = {}
    for k in topo:
        live_preds = [p for p in pred[k] if p in topo_set]
        if not live_preds:
            # Real source: connected from pseudo-source by a single edge → 1
            n_minus[k] = 1
        else:
            n_minus[k] = sum(n_minus.get(p, 0) for p in live_preds)

    # n_plus[v] = # distinct paths from v to pseudo-sink
    # Backward pass: real sinks → pseudo-sink (each counts 1)
    n_plus: Dict[str, int] = {}
    for k in reversed(topo):
        live_succs = [s for s in succ[k] if s in topo_set]
        if not live_succs:
            # Real sink: connected to pseudo-sink → 1
            n_plus[k] = 1
        else:
            n_plus[k] = sum(n_plus.get(s, 0) for s in live_succs)

    # Edge SPC weights: spc(u,v) = n_minus[u] * n_plus[v]
    edge_weights = [
        {'source': u, 'target': v, 'spc_weight': n_minus[u] * n_plus[v]}
        for u, v in valid_edges
    ]
    spc_map = {(e['source'], e['target']): e['spc_weight'] for e in edge_weights}

    # ------------------------------------------------------------------
    # Greedy local main path (preserved for backward compatibility):
    # start from the source with highest n_plus, follow max-weight edge
    # ------------------------------------------------------------------
    if not real_sources:
        greedy_path: List[str] = []
    else:
        start = max(real_sources, key=lambda s: n_plus.get(s, 0))
        greedy_path = [start]
        visited: set = {start}
        current = start
        while True:
            live_succs = [s for s in succ[current] if s in topo_set]
            if not live_succs:
                break
            best = max(live_succs, key=lambda v: spc_map.get((current, v), 0))
            if best in visited:
                break
            greedy_path.append(best)
            visited.add(best)
            current = best

    # ------------------------------------------------------------------
    # Canonical global main path:
    # longest-weighted path (max sum of SPC edge weights) from any real
    # source to any real sink, computed via DP over topological order.
    # ------------------------------------------------------------------
    # dp_val[v] = best total SPC weight of any path ending at v
    # dp_prev[v] = predecessor of v on that best path
    dp_val: Dict[str, int] = {k: 0 for k in topo}
    dp_prev: Dict[str, Any] = {k: None for k in topo}
    for k in topo:
        live_preds = [p for p in pred[k] if p in topo_set]
        for p in live_preds:
            w = spc_map.get((p, k), 0)
            candidate = dp_val[p] + w
            if candidate > dp_val[k]:
                dp_val[k] = candidate
                dp_prev[k] = p

    # Trace back from the sink with the highest dp_val
    if not real_sinks:
        global_path: List[str] = []
    else:
        end = max(real_sinks, key=lambda s: dp_val.get(s, 0))
        global_path = []
        cur: Any = end
        while cur is not None:
            global_path.append(cur)
            cur = dp_prev[cur]
        global_path.reverse()

    return {
        'edges': edge_weights,
        'main_path': greedy_path,
        'global_main_path': global_path,
        'note': None,
    }


# ---------------------------------------------------------------------------
# Interactive D3 lineage visualization (HTML)
# ---------------------------------------------------------------------------

_D3_HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>Citation Lineage &mdash; __SEED_ID__</title>
<script src="https://cdnjs.cloudflare.com/ajax/libs/d3/7.8.5/d3.min.js"></script>
<style>
* { box-sizing: border-box; margin: 0; padding: 0; }
body { background: #111827; font-family: ui-sans-serif, system-ui, sans-serif; overflow: hidden; color: #e5e7eb; }
svg { width: 100vw; height: 100vh; display: block; }
.link { fill: none; stroke: #374151; stroke-width: 1.3; }
.link.mp { stroke: #f59e0b; stroke-width: 3; }
.node-circle { stroke-width: 1.5; cursor: pointer; }
.node-label { font-size: 9px; fill: #9ca3af; pointer-events: none; text-anchor: middle; }
.gen-guide { stroke: #1f2937; stroke-width: 1; stroke-dasharray: 4 4; }
.gen-tag { fill: #4b5563; font-size: 11px; }
#tooltip {
  position: fixed; background: rgba(17,24,39,0.97); border: 1px solid #374151;
  border-radius: 8px; padding: 12px 14px; max-width: 320px; font-size: 12px;
  pointer-events: none; opacity: 0; transition: opacity 0.1s; z-index: 100; line-height: 1.6;
}
.tt-title { font-weight: 600; color: #f9fafb; margin-bottom: 4px; }
.tt-row { color: #9ca3af; }
.tt-row span { color: #d1d5db; }
#legend {
  position: fixed; bottom: 16px; left: 16px; background: rgba(17,24,39,0.85);
  border: 1px solid #374151; border-radius: 6px; padding: 8px 12px; font-size: 11px;
}
.lrow { display: flex; align-items: center; gap: 6px; margin-bottom: 3px; }
.ldot { width: 10px; height: 10px; border-radius: 50%; flex-shrink: 0; }
#hint {
  position: fixed; top: 10px; right: 16px; font-size: 11px; color: #4b5563;
}
</style>
</head>
<body>
<div id="tooltip"></div>
<svg id="viz"></svg>
<div id="legend"></div>
<div id="hint">Scroll to zoom &bull; Drag to pan &bull; Hover for details</div>
<script>
const DATA = __DATA__;
const MP_EDGE_IDS = new Set(__MP_EDGE_IDS__);

const GEN_COLORS = [
  "#3b82f6","#ef4444","#10b981","#f59e0b","#8b5cf6","#06b6d4","#f97316","#84cc16",
  "#ec4899","#a3e635"
];
function genColor(g) { return GEN_COLORS[g % GEN_COLORS.length]; }

const W = window.innerWidth, H = window.innerHeight;
const LAYER_GAP = Math.max(90, Math.min(200, (H - 120) / Math.max(DATA.max_gen, 1)));
const PAD_X = 80;

// Group and sort nodes by generation
const byGen = new Map();
DATA.nodes.forEach(n => {
  if (!byGen.has(n.generation)) byGen.set(n.generation, []);
  byGen.get(n.generation).push(n);
});
byGen.forEach(arr => arr.sort((a, b) => b.cited_by_count - a.cited_by_count));

// Assign x/y positions (layered layout)
DATA.nodes.forEach(n => {
  const arr = byGen.get(n.generation);
  const idx = arr.indexOf(n);
  const count = arr.length;
  n.x = PAD_X + (W - PAD_X * 2) * (count > 1 ? idx / (count - 1) : 0.5);
  n.y = 60 + n.generation * LAYER_GAP;
});

const nodeById = new Map(DATA.nodes.map(n => [n.id, n]));

const maxCBC = Math.max(...DATA.nodes.map(n => n.cited_by_count), 1);
function nodeRadius(cbc) {
  return 5 + 23 * Math.log1p(cbc) / Math.log1p(maxCBC);
}

// SVG setup
const svg = d3.select("#viz");
const g = svg.append("g");

const zoom = d3.zoom()
  .scaleExtent([0.1, 8])
  .on("zoom", e => g.attr("transform", e.transform));
svg.call(zoom);

// Generation guide lines
for (let gen = 0; gen <= DATA.max_gen; gen++) {
  const y = 60 + gen * LAYER_GAP;
  g.append("line")
    .attr("class", "gen-guide")
    .attr("x1", 0).attr("x2", W * 2).attr("y1", y).attr("y2", y);
  g.append("text")
    .attr("class", "gen-tag")
    .attr("x", 8).attr("y", y - 5)
    .text("gen " + gen);
}

// Links (regular first, then main-path on top)
function linkPath(d) {
  const s = nodeById.get(d.source), t = nodeById.get(d.target);
  if (!s || !t) return "";
  const sr = nodeRadius(s.cited_by_count), tr = nodeRadius(t.cited_by_count);
  const x1 = s.x, y1 = s.y + sr, x2 = t.x, y2 = t.y - tr;
  const cy = (y1 + y2) / 2;
  return `M${x1},${y1} C${x1},${cy} ${x2},${cy} ${x2},${y2}`;
}

g.selectAll(".link.regular")
  .data(DATA.edges.filter(e => !MP_EDGE_IDS.has(e.id)))
  .enter().append("path")
  .attr("class", "link regular")
  .attr("d", linkPath);

g.selectAll(".link.mp")
  .data(DATA.edges.filter(e => MP_EDGE_IDS.has(e.id)))
  .enter().append("path")
  .attr("class", "link mp")
  .attr("d", linkPath);

// Tooltip
const tip = document.getElementById("tooltip");

// Nodes
const node = g.selectAll(".node")
  .data(DATA.nodes)
  .enter().append("g")
  .attr("class", "node")
  .attr("transform", d => `translate(${d.x},${d.y})`);

const mpSet = new Set(DATA.main_path);

node.append("circle")
  .attr("class", "node-circle")
  .attr("r", d => nodeRadius(d.cited_by_count))
  .attr("fill", d => genColor(d.generation))
  .attr("stroke", d => mpSet.has(d.id) ? "#f59e0b" : "#1f2937")
  .attr("stroke-width", d => mpSet.has(d.id) ? 3 : 1.5)
  .attr("opacity", 0.9)
  .on("mousemove", function(event, d) {
    tip.innerHTML =
      `<div class="tt-title">${d.title}</div>` +
      `<div class="tt-row">Year: <span>${d.year || "?"}</span></div>` +
      `<div class="tt-row">Venue: <span>${d.venue || "?"}</span></div>` +
      `<div class="tt-row">Citations: <span>${d.cited_by_count}</span></div>` +
      `<div class="tt-row">Generation: <span>${d.generation}</span></div>` +
      `<div class="tt-row">ID: <span>${d.id}</span></div>`;
    tip.style.opacity = 1;
    const tx = Math.min(event.clientX + 14, W - 340);
    tip.style.left = tx + "px";
    tip.style.top = Math.max(event.clientY - 10, 0) + "px";
  })
  .on("mouseleave", () => { tip.style.opacity = 0; });

node.append("text")
  .attr("class", "node-label")
  .attr("y", d => nodeRadius(d.cited_by_count) + 13)
  .text(d => d.label);

// Legend
const legendEl = document.getElementById("legend");
let lhtml = "";
for (let gen = 0; gen <= DATA.max_gen; gen++) {
  lhtml += `<div class="lrow"><div class="ldot" style="background:${genColor(gen)}"></div>Gen ${gen}</div>`;
}
lhtml += `<div class="lrow"><div class="ldot" style="background:#f59e0b;border:2px solid #f59e0b"></div>Main path</div>`;
legendEl.innerHTML = lhtml;

// Auto-fit initial view
const xs = DATA.nodes.map(n => n.x);
const ys = DATA.nodes.map(n => n.y);
if (xs.length) {
  const minX = Math.min(...xs) - 60, maxX = Math.max(...xs) + 60;
  const minY = Math.min(...ys) - 60, maxY = Math.max(...ys) + 60;
  const bW = maxX - minX, bH = maxY - minY;
  if (bW > 0 && bH > 0) {
    const scale = Math.min((W / bW) * 0.88, (H / bH) * 0.88, 2);
    const tx = (W - bW * scale) / 2 - minX * scale;
    const ty = (H - bH * scale) / 2 - minY * scale;
    svg.call(zoom.transform, d3.zoomIdentity.translate(tx, ty).scale(scale));
  }
}
</script>
</body>
</html>"""


def render_lineage_html(
    records: List[Dict[str, Any]],
    main_path: List[str],
    seed_id: str,
    base_filename: str,
) -> Optional[str]:
    """Write a self-contained interactive D3 v7 HTML visualization of the citation lineage.

    Returns the absolute path to the HTML file, or None if rendering fails (non-fatal).
    The file is fully self-contained: D3 loaded from CDN, all data inlined as JSON.
    """
    try:
        out = _output_dir()
        html_path = out / f'{base_filename}.html'

        # Build node data
        nodes_data = []
        nodes_by_key: Dict[str, dict] = {}
        for r in records:
            k = _rec_key(r)
            if not k:
                continue
            cbc = 0
            try:
                cbc = int(r.get('cited_by_count') or 0)
            except (ValueError, TypeError):
                pass
            nd = {
                'id': k,
                'title': r.get('title') or k,
                'year': r.get('year') or '',
                'venue': r.get('venue') or '',
                'cited_by_count': cbc,
                'generation': r.get('generation', 0),
                'label': _make_node_label(r, k),
            }
            nodes_data.append(nd)
            nodes_by_key[k] = nd

        # Build edge data from parents
        edges_data = []
        seen_e: set = set()
        for r in records:
            child_key = _rec_key(r)
            if not child_key or child_key not in nodes_by_key:
                continue
            for parent_key in (r.get('parents') or []):
                if parent_key in nodes_by_key:
                    eid = f'{parent_key}→{child_key}'
                    if eid not in seen_e:
                        seen_e.add(eid)
                        edges_data.append({'source': parent_key, 'target': child_key, 'id': eid})

        # Main-path edge ids for highlight
        mp_edge_ids = []
        for i in range(len(main_path) - 1):
            mp_edge_ids.append(f'{main_path[i]}→{main_path[i + 1]}')

        max_gen = max((nd['generation'] for nd in nodes_data), default=0)

        # Inline data — replace </script with <\/script to prevent tag injection
        def _safe_json(obj: Any) -> str:
            return json.dumps(obj, ensure_ascii=True).replace('</', '<\\/')

        data_payload = {
            'nodes': nodes_data,
            'edges': edges_data,
            'max_gen': max_gen,
            'seed_id': seed_id,
            'main_path': main_path,
        }

        html = _D3_HTML_TEMPLATE \
            .replace('__SEED_ID__', seed_id) \
            .replace('__DATA__', _safe_json(data_payload)) \
            .replace('__MP_EDGE_IDS__', _safe_json(mp_edge_ids))

        html_path.write_text(html, encoding='utf-8')
        return str(html_path)

    except Exception as exc:
        logger.warning(f'render_lineage_html failed (non-fatal): {exc}')
        return None


# ---------------------------------------------------------------------------
# Static PNG for the lineage (layered DAG, matplotlib + networkx)
# ---------------------------------------------------------------------------

def render_lineage_png(
    records: List[Dict[str, Any]],
    main_path: List[str],
    seed_id: str,
    base_filename: str,
) -> Optional[str]:
    """Render the citation lineage as a static layered-DAG PNG.

    Uses networkx.multipartite_layout (gen on y-axis), nodes sized by citation
    count, colored by generation.  Labels only the top-3 most-cited nodes per
    generation to keep the image readable.  Returns None on failure (non-fatal).
    """
    try:
        import matplotlib
        matplotlib.use('Agg')
        import matplotlib.pyplot as plt
        import networkx as nx

        out = _output_dir()
        png_path = out / f'{base_filename}.png'

        G = nx.DiGraph()
        node_meta: Dict[str, dict] = {}
        for r in records:
            k = _rec_key(r)
            if not k:
                continue
            gen = r.get('generation', 0)
            cbc = 0
            try:
                cbc = int(r.get('cited_by_count') or 0)
            except (ValueError, TypeError):
                pass
            G.add_node(k, generation=gen)
            node_meta[k] = {
                'gen': gen,
                'cbc': cbc,
                'label': _make_node_label(r, k),
            }

        for r in records:
            child = _rec_key(r)
            if not child or not G.has_node(child):
                continue
            for parent in (r.get('parents') or []):
                if G.has_node(parent):
                    G.add_edge(parent, child)

        if len(G.nodes) == 0:
            return None

        # multipartite_layout: subset_key='generation', align='horizontal' →
        # each generation has the same y coordinate; negate y so gen 0 is at top.
        pos_raw = nx.multipartite_layout(G, subset_key='generation', align='horizontal')
        pos = {k: (x, -y) for k, (x, y) in pos_raw.items()}

        max_cbc = max((node_meta[k]['cbc'] for k in G.nodes), default=1)

        def _node_size(k: str) -> float:
            cbc = node_meta[k]['cbc']
            return max(40, 1800 * math.log1p(cbc) / math.log1p(max(max_cbc, 1)))

        # Generation colours (Set2 palette, wraps for >8 gens)
        import matplotlib.cm as cm
        palette = plt.cm.Set2.colors  # 8 colours
        node_colors = [palette[node_meta[k]['gen'] % len(palette)] for k in G.nodes]
        node_sizes = [_node_size(k) for k in G.nodes]

        # Main-path edges
        mp_edge_set = set()
        for i in range(len(main_path) - 1):
            mp_edge_set.add((main_path[i], main_path[i + 1]))
        regular_edges = [(u, v) for u, v in G.edges() if (u, v) not in mp_edge_set]
        mp_edges = [(u, v) for u, v in G.edges() if (u, v) in mp_edge_set]

        # Labels: top-3 most-cited per generation
        by_gen: Dict[int, List[str]] = {}
        for k in G.nodes:
            g_idx = node_meta[k]['gen']
            by_gen.setdefault(g_idx, []).append(k)
        labeled: set = set()
        for _, nds in by_gen.items():
            for k in sorted(nds, key=lambda k: node_meta[k]['cbc'], reverse=True)[:3]:
                labeled.add(k)
        labels = {k: node_meta[k]['label'] for k in labeled}

        fig, ax = plt.subplots(figsize=(14, 10))
        fig.patch.set_facecolor('#111827')
        ax.set_facecolor('#111827')

        nx.draw_networkx_nodes(
            G, pos, node_size=node_sizes, node_color=node_colors, ax=ax, alpha=0.9,
        )
        if regular_edges:
            nx.draw_networkx_edges(
                G, pos, edgelist=regular_edges, edge_color='#4b5563',
                ax=ax, arrows=True, arrowsize=10, width=0.8, alpha=0.6,
            )
        if mp_edges:
            nx.draw_networkx_edges(
                G, pos, edgelist=mp_edges, edge_color='#f59e0b',
                ax=ax, arrows=True, arrowsize=15, width=2.5, alpha=0.95,
            )
        if labels:
            nx.draw_networkx_labels(
                G, pos, labels=labels, ax=ax, font_size=7, font_color='white',
            )

        ax.set_axis_off()
        plt.tight_layout()
        plt.savefig(str(png_path), dpi=150, bbox_inches='tight', facecolor='#111827')
        plt.close(fig)

        return str(png_path)

    except Exception as exc:
        logger.warning(f'render_lineage_png failed (non-fatal): {exc}')
        return None


# ---------------------------------------------------------------------------
# Search result cleaner
# ---------------------------------------------------------------------------

def clean_search_results(data: Dict[str, Any]) -> List[Dict[str, Any]]:
    """
    Extracts and normalizes search results from the Scopus Search API response.

    Args:
        data: The raw JSON response from Scopus API.

    Returns:
        A list of simplified dictionaries containing key article metadata.
        Returns [] when there are zero results or when the API returns an
        error-sentinel entry (no dc:identifier, carries an 'error' key).
    """
    if not data or 'search-results' not in data:
        return []

    sr = data['search-results']

    # Scopus signals an empty result set with totalResults "0".
    if str(sr.get('opensearch:totalResults', '')).strip() == '0':
        return []

    entries = sr.get('entry', [])
    cleaned_entries = []

    for entry in entries:
        # Skip error-sentinel entries: they carry an 'error' key and no real ID.
        if entry.get('error') and not entry.get('dc:identifier'):
            continue
        cleaned = {
            'scopus_id': entry.get('dc:identifier', '').replace('SCOPUS_ID:', ''),
            'title': entry.get('dc:title'),
            'creator': entry.get('dc:creator'),
            'publication_name': entry.get('prism:publicationName'),
            'cover_date': entry.get('prism:coverDate'),
            'doi': entry.get('prism:doi'),
            'cited_by_count': entry.get('citedby-count'),
            'aggregation_type': entry.get('prism:aggregationType'),
            'url': next((link['@href'] for link in entry.get('link', []) if link.get('@ref') == 'scopus'), None)
        }
        cleaned_entries.append(cleaned)

    return cleaned_entries

def clean_abstract_details(data: Dict[str, Any]) -> Dict[str, Any]:
    """
    Extracts relevant details from the Scopus Abstract Retrieval API response.

    Args:
        data: The raw JSON response from Scopus API.

    Returns:
        A simplified dictionary containing abstract details.
    """
    # The API key can be singular or plural depending on endpoint version/context,
    # though usually 'abstracts-retrieval-response'.
    root = data.get('abstracts-retrieval-response') or data.get('abstract-retrieval-response')
    
    if not root:
        return {}

    coredata = root.get('coredata', {})
    authors_data = root.get('authors', {}).get('author', [])
    
    # Normalize authors to a list even if single author (API inconsistency)
    if isinstance(authors_data, dict):
        authors_data = [authors_data]
        
    authors = []
    for auth in authors_data:
        authors.append({
            'auth_id': auth.get('@auid'),
            'name': auth.get('ce:indexed-name'),
            'surname': auth.get('ce:surname'),
            'initials': auth.get('ce:initials')
        })

    return {
        'scopus_id': coredata.get('dc:identifier', '').replace('SCOPUS_ID:', ''),
        'doi': coredata.get('prism:doi'),
        'title': coredata.get('dc:title'),
        'description': coredata.get('dc:description'), # This is the abstract text
        'publication_name': coredata.get('prism:publicationName'),
        'cover_date': coredata.get('prism:coverDate'),
        'cited_by_count': coredata.get('citedby-count'),
        'authors': authors,
        'url': next((link['@href'] for link in coredata.get('link', []) if link.get('@ref') == 'scopus'), None)
    }

def clean_author_profile(data: Dict[str, Any]) -> Dict[str, Any]:
    """
    Extracts details from the Scopus Author Retrieval API response.

    Args:
        data: The raw JSON response from Scopus API.

    Returns:
        A simplified dictionary containing author profile information.
    """
    if not data or 'author-retrieval-response' not in data:
        return {}
    
    root = data['author-retrieval-response']
    core = root.get('coredata', {})
    profile = root.get('author-profile', {})
    
    name_variant = profile.get('preferred-name', {})
    
    return {
        'author_id': core.get('dc:identifier', '').replace('AUTHOR_ID:', ''),
        'orcid': core.get('orcid'),
        'document_count': core.get('document-count'),
        'cited_by_count': core.get('cited-by-count'),
        'citation_count': core.get('citation-count'),
        'name': {
            'surname': name_variant.get('surname'),
            'given_name': name_variant.get('given-name'),
            'initials': name_variant.get('initials')
        },
        'current_affiliation': _extract_affiliation(profile),
        'url': next((link['@href'] for link in core.get('link', []) if link.get('@ref') == 'scopus-author'), None)
    }

def _extract_affiliation(profile: Dict[str, Any]) -> Optional[str]:
    """Helper to extract current affiliation name."""
    affil = profile.get('affiliation-current', {}).get('affiliation', {})
    # Sometimes it's a list if multiple affiliations
    if isinstance(affil, list):
        affil = affil[0] if affil else {}
        
    return affil.get('ip-doc', {}).get('afdispname')


# ---------------------------------------------------------------------------
# Identifier normalization helpers
# ---------------------------------------------------------------------------
# Scopus exposes the same document under several identifiers. The Search API's
# REF() field expects the EID form (2-s2.0-<id>), while the Abstract Retrieval
# endpoints key on the bare Scopus ID. Centralizing the conversion here keeps
# every tool consistent and prevents the REFEID/REF class of bug.

def to_scopus_id(value: str) -> str:
    """Return the bare numeric Scopus ID, stripping a SCOPUS_ID: prefix or
    2-s2.0- EID prefix if present."""
    clean = str(value).strip().replace('SCOPUS_ID:', '')
    if clean.lower().startswith('2-s2.0-'):
        clean = clean[len('2-s2.0-'):]
    return clean


def to_eid(value: str) -> str:
    """Return the EID form (2-s2.0-<id>) used by the Search API's REF() field."""
    return f'2-s2.0-{to_scopus_id(value)}'


def detect_id_type(value: str) -> str:
    """Best-effort detection of an identifier's type.

    Returns one of: 'eid', 'doi', 'pii', 'scopus_id'. Callers may override.
    """
    v = str(value).strip()
    if v.lower().startswith('2-s2.0-'):
        return 'eid'
    if v.startswith('10.') or '/' in v:
        return 'doi'
    bare = v.replace('SCOPUS_ID:', '')
    if bare.isdigit():
        return 'scopus_id'
    # PII is 17 chars, alphanumeric, often starting with S or B. Loose heuristic.
    if bare and bare[0] in ('S', 'B') and bare.replace('-', '').isalnum():
        return 'pii'
    return 'scopus_id'


def clean_identifiers(data: Dict[str, Any]) -> Dict[str, Any]:
    """Extract the cross-reference identifier set from an Abstract Retrieval
    response. This is the join layer for cross-linking with OpenAlex/Crossref,
    which key on DOI."""
    root = data.get('abstracts-retrieval-response') or data.get('abstract-retrieval-response')
    if not root:
        return {}

    core = root.get('coredata', {})
    sid = core.get('dc:identifier', '').replace('SCOPUS_ID:', '')
    eid = core.get('eid') or (f'2-s2.0-{sid}' if sid else None)

    return {
        'scopus_id': sid or None,
        'eid': eid,
        'doi': core.get('prism:doi'),
        'pii': core.get('pii'),
        'pubmed_id': core.get('pubmed-id'),
        'title': core.get('dc:title'),
        'publication_name': core.get('prism:publicationName'),
        'cover_date': core.get('prism:coverDate'),
        'cited_by_count': core.get('citedby-count'),
    }


def clean_references(data: Dict[str, Any], limit: Optional[int] = None) -> List[Dict[str, Any]]:
    """Extract the cited-reference list (backward citations) from an Abstract
    Retrieval REF-view response.

    The REF view returns each reference as a flat object with top-level keys:
    'title', 'sourcetitle', 'scopus-id', 'ce:doi', 'prism:coverDate',
    'author-list', '@id'.  Unresolved or partially matched references may omit
    some fields; the parser degrades gracefully to None for missing values.
    Authors are deduplicated by @auid to collapse multi-affiliation duplicates.
    """
    root = data.get('abstracts-retrieval-response') or data.get('abstract-retrieval-response')
    if not root:
        return []

    ref_block = root.get('references')
    if not isinstance(ref_block, dict):
        return []
    refs = ref_block.get('reference')
    if refs is None:
        return []
    if isinstance(refs, dict):
        refs = [refs]
    if not isinstance(refs, list):
        return []

    cleaned: List[Dict[str, Any]] = []
    for r in refs:
        if not isinstance(r, dict):
            continue

        # Year from ISO cover date (e.g. "1996-01-01" → "1996")
        cover = r.get('prism:coverDate') or ''
        year = cover[:4] if cover else None

        # Authors — deduplicate by @auid to collapse multi-affiliation entries
        raw_authors = r.get('author-list', {})
        if isinstance(raw_authors, dict):
            raw_authors = raw_authors.get('author', [])
        if isinstance(raw_authors, dict):
            raw_authors = [raw_authors]
        seen_auids: set = set()
        authors: List[str] = []
        for a in (raw_authors or []):
            if not isinstance(a, dict):
                continue
            auid = a.get('@auid')
            if auid and auid in seen_auids:
                continue
            if auid:
                seen_auids.add(auid)
            name = a.get('ce:indexed-name') or a.get('ce:surname')
            if name:
                authors.append(name)

        cleaned.append({
            'position': r.get('@id'),
            'title': r.get('title'),
            'authors': authors,
            'source': r.get('sourcetitle'),
            'year': year or None,
            'scopus_id': r.get('scopus-id'),
            'doi': r.get('ce:doi'),
            'fulltext': None,
        })

    if limit is not None:
        return cleaned[:limit]
    return cleaned
