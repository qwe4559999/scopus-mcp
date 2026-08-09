import asyncio
import logging
from typing import Any, Optional

from mcp.server import Server
from mcp.server.stdio import stdio_server
import mcp.types as types

from .client import ScopusClient
from .utils import (
    clean_search_results,
    clean_abstract_details,
    clean_author_profile,
    clean_identifiers,
    clean_references,
    detect_id_type,
    to_scopus_id,
    to_eid,
    write_results_to_disk,
    should_write_to_disk,
    compute_pairwise_edges,
    write_graph_to_disk,
    _make_node_label,
    write_lineage_to_disk,
    compute_main_path,
    render_lineage_html,
    render_lineage_png,
    _query_slug,
    fetch_oa_fulltext,
    write_fulltext_to_disk,
)

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("scopus-mcp")

# Initialize Server
server = Server("scopus-mcp")
client = ScopusClient()

@server.list_tools()
async def handle_list_tools() -> list[types.Tool]:
    return [
        types.Tool(
            name="search_scopus",
            description="Search for documents in Scopus using a query string.",
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "The Scopus search query (e.g., 'TITLE(AI) AND PUBYEAR > 2020')."
                    },
                    "count": {
                        "type": "integer",
                        "description": "Number of results to return (default 5, max 25).",
                        "default": 5,
                        "maximum": 25
                    },
                    "sort": {
                        "type": "string",
                        "description": "Sort order (e.g., 'coverDate', 'relevancy').",
                        "default": "coverDate"
                    }
                },
                "required": ["query"]
            }
        ),
        types.Tool(
            name="get_abstract_details",
            description="Retrieve full details for a specific document by Scopus ID.",
            inputSchema={
                "type": "object",
                "properties": {
                    "scopus_id": {
                        "type": "string",
                        "description": "The Scopus ID of the document."
                    }
                },
                "required": ["scopus_id"]
            }
        ),
        types.Tool(
            name="get_author_profile",
            description="Retrieve an author's profile by Author ID.",
            inputSchema={
                "type": "object",
                "properties": {
                    "author_id": {
                        "type": "string",
                        "description": "The Scopus Author ID."
                    }
                },
                "required": ["author_id"]
            }
        ),
        types.Tool(
            name="get_citing_papers",
            description="Retrieve a list of papers that have cited the specified document (Forward Citations).",
            inputSchema={
                "type": "object",
                "properties": {
                    "scopus_id": {
                        "type": "string",
                        "description": "The Scopus ID of the document to find citations for."
                    },
                    "count": {
                        "type": "integer",
                        "description": "Number of results to return (default 5, max 25).",
                        "default": 5,
                        "maximum": 25
                    },
                    "sort": {
                        "type": "string",
                        "description": "Sort order (e.g., 'coverDate', 'relevancy').",
                        "default": "coverDate"
                    }
                },
                "required": ["scopus_id"]
            }
        ),
        types.Tool(
            name="get_quota_status",
            description="Get the current API quota status (remaining/limit). Note: Values are updated only after making a request.",
            inputSchema={
                "type": "object",
                "properties": {},
                "required": []
            }
        ),
        types.Tool(
            name="resolve_identifier",
            description=(
                "Resolve any document identifier (Scopus ID, EID, DOI, or PII) to "
                "the full cross-reference set (scopus_id, eid, doi, pii, title). "
                "Use this to obtain a DOI for cross-linking with OpenAlex/Crossref, "
                "or to normalize an ID before calling other tools."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "identifier": {
                        "type": "string",
                        "description": "The identifier value (e.g. '0031512927', '2-s2.0-0031512927', or a DOI)."
                    },
                    "id_type": {
                        "type": "string",
                        "description": "Optional override of the identifier type.",
                        "enum": ["scopus_id", "eid", "doi", "pii"]
                    }
                },
                "required": ["identifier"]
            }
        ),
        types.Tool(
            name="search_all",
            description=(
                "Search Scopus and automatically page through results, returning up to "
                "max_results entries in a single call. Uses STANDARD view (200 results/page). "
                "For max_results > 5,000 the tool switches to cursor-based deep paging. "
                "Large max_results values consume significant API quota and may require "
                "many requests — use conservatively."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "The Scopus search query (e.g., 'TITLE(AI) AND PUBYEAR > 2020')."
                    },
                    "max_results": {
                        "type": "integer",
                        "description": "Maximum total results to fetch across all pages (default 200). Large values consume quota.",
                        "default": 200
                    },
                    "sort": {
                        "type": "string",
                        "description": "Sort order (e.g., 'coverDate', 'relevancy').",
                        "default": "coverDate"
                    }
                },
                "required": ["query"]
            }
        ),
        types.Tool(
            name="bibliographic_coupling",
            description=(
                "Build a bibliographic-coupling graph for a set of seed papers. "
                "Two seeds are coupled when they share cited references; edge weight = "
                "count of shared references, cosine = Salton index. "
                "Maps the current research front. Requires an entitled (subscriber) key "
                "for REF-view access. Output: GraphML + CSV edge list written to disk."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "seed_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "List of Scopus IDs (bare numeric or SCOPUS_ID: prefixed)."
                    },
                    "min_shared": {
                        "type": "integer",
                        "description": "Minimum shared references for an edge to be emitted (default 2).",
                        "default": 2
                    }
                },
                "required": ["seed_ids"]
            }
        ),
        types.Tool(
            name="co_citation",
            description=(
                "Build a co-citation graph for a set of seed papers. "
                "Two seeds are co-cited when a later paper cites both; edge weight = "
                "count of co-citing papers, cosine = Salton index. "
                "Maps the intellectual base of a field. "
                "max_citing_per_seed bounds the API quota used per seed. "
                "Output: GraphML + CSV edge list written to disk."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "seed_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "List of Scopus IDs (bare numeric or SCOPUS_ID: prefixed)."
                    },
                    "min_shared": {
                        "type": "integer",
                        "description": "Minimum co-citing papers for an edge to be emitted (default 2).",
                        "default": 2
                    },
                    "max_citing_per_seed": {
                        "type": "integer",
                        "description": "Cap on citing papers fetched per seed (default 500). Limits quota usage.",
                        "default": 500
                    }
                },
                "required": ["seed_ids"]
            }
        ),
        types.Tool(
            name="get_references",
            description=(
                "Retrieve the cited-reference list of a document (Backward Citations) "
                "via the Abstract Retrieval REF view. Complements get_citing_papers, "
                "which returns forward citations. Requires an entitled (subscriber) key."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "scopus_id": {
                        "type": "string",
                        "description": "The Scopus ID (or EID) of the document whose references to retrieve."
                    },
                    "count": {
                        "type": "integer",
                        "description": "Maximum number of references to return (default 25).",
                        "default": 25
                    }
                },
                "required": ["scopus_id"]
            }
        ),
        types.Tool(
            name="get_fulltext",
            description=(
                "Retrieve the full text of a paper via a provider waterfall: "
                "(1) ScienceDirect full text (requires SCOPUS_INSTTOKEN or institutional IP), "
                "(2) open-access copy via OpenAlex + direct fetch, "
                "(3) Scopus abstract fallback. "
                "Returns provenance, character count, file path, and a ~1500-char sample. "
                "Full body is written to disk — never returned inline. "
                "ToS note: retrieval is for the user's own non-commercial text-and-data-mining; "
                "content written to local disk must not be redistributed."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "doi": {
                        "type": "string",
                        "description": "The DOI of the paper (e.g. '10.1016/j.infoandorg.2026.100608')."
                    },
                    "prefer": {
                        "type": "string",
                        "description": "Skip straight to a tier for testing: 'sciencedirect', 'oa', 'abstract'.",
                        "enum": ["sciencedirect", "oa", "abstract"]
                    }
                },
                "required": ["doi"]
            }
        ),
        types.Tool(
            name="citation_lineage",
            description=(
                "Walk the forward-citation lineage of a seed paper across multiple generations. "
                "Generation 1 = papers that directly cite the seed; generation 2 = papers that "
                "cite those; up to 3 generations. All papers are deduplicated globally across "
                "generations. Output: lineage corpus written to disk as JSON (one record per "
                "paper with scopus_id, doi, title, year, venue, generation, parents, "
                "cited_by_count). Returns a compact inline summary; never dumps the full corpus "
                "inline. Use max_per_node to bound API quota per paper."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "seed_id": {
                        "type": "string",
                        "description": "Scopus ID or EID of the seed paper."
                    },
                    "generations": {
                        "type": "integer",
                        "description": "Number of generations to walk (default 1, max 3).",
                        "default": 1,
                        "maximum": 3
                    },
                    "max_per_node": {
                        "type": "integer",
                        "description": "Cap on citing papers fetched per paper per generation (default 200).",
                        "default": 200
                    },
                    "min_citing": {
                        "type": "integer",
                        "description": (
                            "Only expand papers that have at least this many citing papers "
                            "(default 0 = expand all up to max_per_node). Pruning high "
                            "values avoids exploding on trivially-cited nodes. "
                            "Ignored for backward direction."
                        ),
                        "default": 0
                    },
                    "direction": {
                        "type": "string",
                        "description": (
                            "'forward' (default): walk citing papers via search_all + REF(). "
                            "Fan-out can be large; use max_per_node to bound quota. "
                            "'backward': walk cited references via get_references. "
                            "Fan-out is naturally bounded (~40 refs/paper); "
                            "references with neither scopus_id nor doi are skipped."
                        ),
                        "enum": ["forward", "backward"],
                        "default": "forward"
                    }
                },
                "required": ["seed_id"]
            }
        )
    ]

@server.call_tool()
async def handle_call_tool(
    name: str, arguments: dict[str, Any] | None
) -> list[types.TextContent | types.ImageContent | types.EmbeddedResource]:
    if not arguments:
        arguments = {}

    try:
        if name == "search_scopus":
            query = arguments.get("query")
            count = arguments.get("count", 5)
            sort = arguments.get("sort", "coverDate")
            
            if not query:
                raise ValueError("Query is required")

            # Await the async client method
            raw_data = await client.search_scopus(query, count=count, sort=sort)
            results = clean_search_results(raw_data)
            
            return [types.TextContent(type="text", text=str(results))]

        elif name == "get_abstract_details":
            scopus_id = arguments.get("scopus_id")
            if not scopus_id:
                raise ValueError("scopus_id is required")
                
            raw_data = await client.get_abstract(scopus_id)
            details = clean_abstract_details(raw_data)
            
            return [types.TextContent(type="text", text=str(details))]

        elif name == "get_author_profile":
            author_id = arguments.get("author_id")
            if not author_id:
                raise ValueError("author_id is required")
                
            raw_data = await client.get_author(author_id)
            profile = clean_author_profile(raw_data)
            
            return [types.TextContent(type="text", text=str(profile))]

        elif name == "get_citing_papers":
            scopus_id = arguments.get("scopus_id")
            count = arguments.get("count", 5)
            sort = arguments.get("sort", "coverDate")

            if not scopus_id:
                raise ValueError("scopus_id is required")

            # Forward citations via centralized REF(2-s2.0-<id>) construction.
            raw_data = await client.get_citing_papers(scopus_id, count=count, sort=sort)
            results = clean_search_results(raw_data)

            return [types.TextContent(type="text", text=str(results))]

        elif name == "resolve_identifier":
            identifier = arguments.get("identifier")
            if not identifier:
                raise ValueError("identifier is required")

            id_type = arguments.get("id_type") or detect_id_type(identifier)
            raw_data = await client.get_abstract_by(identifier, id_type=id_type)
            ids = clean_identifiers(raw_data)
            if not ids:
                return [types.TextContent(
                    type="text",
                    text=f"No record found for {identifier!r} (resolved as id_type={id_type})."
                )]
            return [types.TextContent(type="text", text=str(ids))]

        elif name == "get_references":
            scopus_id = arguments.get("scopus_id")
            count = arguments.get("count", 25)
            if not scopus_id:
                raise ValueError("scopus_id is required")

            raw_data = await client.get_references(scopus_id)
            references = clean_references(raw_data, limit=count)
            if not references:
                return [types.TextContent(
                    type="text",
                    text=("No references returned. The document may have no indexed "
                          "references, or your API key may lack REF-view entitlement.")
                )]
            return [types.TextContent(type="text", text=str(references))]

        elif name == "search_all":
            query = arguments.get("query")
            max_results = arguments.get("max_results", 200)
            sort = arguments.get("sort", "coverDate")

            if not query:
                raise ValueError("query is required")

            raw_data = await client.search_all(query, max_results=max_results, sort=sort)
            results = clean_search_results(raw_data)
            meta = raw_data.get('_meta', {})

            if should_write_to_disk(results):
                paths = write_results_to_disk(results, query)
                sample = results[:10]
                text = (
                    f"Fetched {meta.get('total_fetched', len(results))} records "
                    f"(total available: {meta.get('total_available', 'unknown')}, "
                    f"truncated: {meta.get('truncated', False)}).\n"
                    f"Full results written to disk:\n"
                    f"  JSON: {paths['json_path']}\n"
                    f"  CSV:  {paths['csv_path']}\n\n"
                    f"First 10 records:\n{sample}"
                )
            else:
                text = str(results)

            if meta.get('note'):
                text += f"\n\nNote: {meta['note']}"

            return [types.TextContent(type="text", text=text)]

        elif name == "bibliographic_coupling":
            seed_ids_raw = arguments.get("seed_ids", [])
            min_shared = int(arguments.get("min_shared", 2))

            if not seed_ids_raw:
                raise ValueError("seed_ids is required and must be non-empty")

            seed_sets: dict = {}
            seed_meta: dict = {}
            skipped: list = []

            for raw_id in seed_ids_raw:
                sid = to_scopus_id(str(raw_id))
                try:
                    raw = await client.get_references(sid)
                    refs = clean_references(raw)
                    ref_keys: set = set()
                    for r in refs:
                        key = r.get('scopus_id') or r.get('doi')
                        if key and key != sid:
                            ref_keys.add(key)
                    if not ref_keys:
                        skipped.append(sid)
                        logger.info(f"bibliographic_coupling: {sid} has no usable refs, skipping")
                        continue
                    seed_sets[sid] = ref_keys
                    # REF view response carries the seed's own coredata — no extra call needed
                    details = clean_abstract_details(raw)
                    authors = details.get('authors') or []
                    seed_meta[sid] = {
                        'title': details.get('title') or sid,
                        'creator': authors[0].get('name') if authors else None,
                        'year': (details.get('cover_date') or '')[:4] or None,
                        'venue': details.get('publication_name'),
                    }
                except Exception as exc:
                    logger.warning(f"bibliographic_coupling: error for {sid}: {exc}")
                    skipped.append(sid)

            edges = compute_pairwise_edges(seed_sets, min_shared=min_shared)
            nodes = [
                {
                    'id': sid,
                    'label': _make_node_label(seed_meta.get(sid, {}), node_id=sid),
                    'title': seed_meta.get(sid, {}).get('title'),
                    'creator': seed_meta.get(sid, {}).get('creator'),
                    'year': seed_meta.get(sid, {}).get('year'),
                    'venue': seed_meta.get(sid, {}).get('venue'),
                }
                for sid in seed_sets
            ]
            paths = write_graph_to_disk(nodes, edges, f'bibcoupling-{len(seed_ids_raw)}seeds')

            top10 = edges[:10]
            top10_lines = [
                f"  {seed_meta.get(e['source'], {}).get('title', e['source'])!r} → "
                f"{seed_meta.get(e['target'], {}).get('title', e['target'])!r}: "
                f"weight={e['weight']}, cosine={e['cosine']:.4f}"
                for e in top10
            ]
            text = (
                f"Bibliographic coupling: {len(seed_sets)}/{len(seed_ids_raw)} seeds processed, "
                f"{len(edges)} edges emitted (min_shared={min_shared}).\n"
                f"Graph files:\n"
                f"  GraphML: {paths['graphml_path']}\n"
                f"  CSV:     {paths['csv_path']}\n"
            )
            if paths.get('png_path'):
                text += f"  PNG:     {paths['png_path']}\n"
            text += f"\nTop {len(top10)} edges by weight:\n"
            text += '\n'.join(top10_lines) if top10_lines else '  (none)'
            if skipped:
                text += f"\n\nSkipped (no usable references or API error): {skipped}"
            return [types.TextContent(type="text", text=text)]

        elif name == "co_citation":
            seed_ids_raw = arguments.get("seed_ids", [])
            min_shared = int(arguments.get("min_shared", 2))
            max_citing = int(arguments.get("max_citing_per_seed", 500))

            if not seed_ids_raw:
                raise ValueError("seed_ids is required and must be non-empty")

            seed_sets = {}
            seed_meta = {}
            skipped = []

            for raw_id in seed_ids_raw:
                sid = to_scopus_id(str(raw_id))
                # Fetch seed metadata (one abstract call per seed)
                try:
                    raw_meta = await client.get_abstract(sid)
                    details = clean_abstract_details(raw_meta)
                    authors = details.get('authors') or []
                    seed_meta[sid] = {
                        'title': details.get('title') or sid,
                        'creator': authors[0].get('name') if authors else None,
                        'year': (details.get('cover_date') or '')[:4] or None,
                        'venue': details.get('publication_name'),
                    }
                except Exception as exc:
                    logger.warning(f"co_citation: metadata fetch failed for {sid}: {exc}")
                    seed_meta[sid] = {'title': sid, 'creator': None, 'year': None, 'venue': None}

                # Fetch citing papers via search_all with REF() query
                try:
                    raw_citers = await client.search_all(
                        f"REF({to_eid(sid)})", max_results=max_citing
                    )
                    citers = clean_search_results(raw_citers)
                    citer_ids: set = set()
                    for c in citers:
                        cid = c.get('scopus_id')
                        if cid and cid != sid:
                            citer_ids.add(cid)
                    if not citer_ids:
                        skipped.append(sid)
                        logger.info(f"co_citation: {sid} has no citing papers, skipping")
                        continue
                    seed_sets[sid] = citer_ids
                except Exception as exc:
                    logger.warning(f"co_citation: citing fetch failed for {sid}: {exc}")
                    skipped.append(sid)

            edges = compute_pairwise_edges(seed_sets, min_shared=min_shared)
            nodes = [
                {
                    'id': sid,
                    'label': _make_node_label(seed_meta.get(sid, {}), node_id=sid),
                    'title': seed_meta.get(sid, {}).get('title'),
                    'creator': seed_meta.get(sid, {}).get('creator'),
                    'year': seed_meta.get(sid, {}).get('year'),
                    'venue': seed_meta.get(sid, {}).get('venue'),
                }
                for sid in seed_sets
            ]
            paths = write_graph_to_disk(nodes, edges, f'cocitation-{len(seed_ids_raw)}seeds')

            top10 = edges[:10]
            top10_lines = [
                f"  {seed_meta.get(e['source'], {}).get('title', e['source'])!r} → "
                f"{seed_meta.get(e['target'], {}).get('title', e['target'])!r}: "
                f"weight={e['weight']}, cosine={e['cosine']:.4f}"
                for e in top10
            ]
            text = (
                f"Co-citation: {len(seed_sets)}/{len(seed_ids_raw)} seeds processed, "
                f"{len(edges)} edges emitted (min_shared={min_shared}, "
                f"max_citing_per_seed={max_citing}).\n"
                f"Graph files:\n"
                f"  GraphML: {paths['graphml_path']}\n"
                f"  CSV:     {paths['csv_path']}\n"
            )
            if paths.get('png_path'):
                text += f"  PNG:     {paths['png_path']}\n"
            text += f"\nTop {len(top10)} edges by weight:\n"
            text += '\n'.join(top10_lines) if top10_lines else '  (none)'
            if skipped:
                text += f"\n\nSkipped (no citing papers or API error): {skipped}"
            return [types.TextContent(type="text", text=text)]

        elif name == "citation_lineage":
            seed_id_raw = arguments.get("seed_id")
            if not seed_id_raw:
                raise ValueError("seed_id is required")

            generations = min(int(arguments.get("generations", 1)), 3)
            max_per_node = int(arguments.get("max_per_node", 200))
            min_citing = int(arguments.get("min_citing", 0))
            direction = (arguments.get("direction") or "forward").lower()
            if direction not in ("forward", "backward"):
                raise ValueError("direction must be 'forward' or 'backward'")

            seed_id = to_scopus_id(str(seed_id_raw))

            # Fetch seed metadata (generation 0)
            try:
                raw_meta = await client.get_abstract(seed_id)
                details = clean_abstract_details(raw_meta)
            except Exception as exc:
                raise ValueError(
                    f"Could not fetch seed metadata for {seed_id}: {exc}"
                ) from exc

            seed_paper = {
                'scopus_id': seed_id,
                'doi': details.get('doi'),
                'title': details.get('title'),
                'year': (details.get('cover_date') or '')[:4] or None,
                'venue': details.get('publication_name'),
                'generation': 0,
                'parents': [],
                'cited_by_count': details.get('cited_by_count'),
            }

            seen: set = {seed_id}
            all_papers: dict = {seed_id: seed_paper}
            to_expand = [seed_id]
            api_calls = 1  # the get_abstract above
            gen_counts: dict = {0: 1}

            async def _fetch_next(parent_id: str) -> list:
                """Return a flat list of candidate-paper dicts for the next generation.

                Each dict carries: key (stable dedup id), scopus_id, doi, title,
                year, venue, cited_by_count.  The only difference between directions
                is which client method is called and how the raw result is mapped.
                """
                if direction == 'forward':
                    raw = await client.search_all(
                        f"REF({to_eid(parent_id)})", max_results=max_per_node
                    )
                    return [
                        {
                            'key': p['scopus_id'],
                            'scopus_id': p['scopus_id'],
                            'doi': p.get('doi'),
                            'title': p.get('title'),
                            'year': (p.get('cover_date') or '')[:4] or None,
                            'venue': p.get('publication_name'),
                            'cited_by_count': p.get('cited_by_count') or '0',
                        }
                        for p in clean_search_results(raw)
                        if p.get('scopus_id')
                    ]
                else:  # backward — walk cited references
                    raw = await client.get_references(parent_id)
                    result = []
                    for r in clean_references(raw, limit=max_per_node):
                        sid = r.get('scopus_id')
                        doi = r.get('doi')
                        if not sid and not doi:
                            continue
                        # Prefer scopus_id as key; fall back to doi: prefix to avoid collisions
                        key = sid if sid else f'doi:{doi}'
                        result.append({
                            'key': key,
                            'scopus_id': sid,
                            'doi': doi,
                            'title': r.get('title'),
                            'year': r.get('year'),
                            'venue': r.get('source'),
                            'cited_by_count': None,
                        })
                    return result

            fetch_attempts = 0
            fetch_errors: list = []  # list of (parent_id, error_message)

            for gen in range(1, generations + 1):
                next_to_expand = []
                for parent_id in to_expand:
                    if not parent_id:
                        continue
                    fetch_attempts += 1
                    try:
                        papers = await _fetch_next(parent_id)
                        api_calls += 1
                    except Exception as exc:
                        err_msg = str(exc)
                        logger.warning(
                            f"citation_lineage ({direction}): fetch failed for "
                            f"{parent_id}: {err_msg}"
                        )
                        fetch_errors.append((parent_id, err_msg))
                        continue

                    for p in papers:
                        key = p['key']
                        if key in seen:
                            # Record an additional parent only when it is from the
                            # immediately preceding generation (parent_gen == child_gen - 1).
                            # Skipping same-generation parents prevents within-generation
                            # edges from entering the lineage DAG and derailing the SPC
                            # main-path (the backward-walk bug: a gen-1 paper's references
                            # can include other gen-1 papers, which would otherwise create
                            # gen1→gen1 edges that the SPC greedy walk traverses sideways).
                            if key in all_papers and parent_id not in all_papers[key]['parents']:
                                child_gen = all_papers[key]['generation']
                                parent_gen = all_papers.get(parent_id, {}).get('generation')
                                if parent_gen is not None and parent_gen == child_gen - 1:
                                    all_papers[key]['parents'].append(parent_id)
                            continue
                        seen.add(key)
                        cbc_str = p.get('cited_by_count')
                        try:
                            cbc = int(cbc_str or 0)
                        except (ValueError, TypeError):
                            cbc = 0
                        all_papers[key] = {
                            'scopus_id': p['scopus_id'],
                            'doi': p['doi'],
                            'title': p['title'],
                            'year': p['year'],
                            'venue': p['venue'],
                            'generation': gen,
                            'parents': [parent_id],
                            'cited_by_count': cbc_str,
                        }
                        gen_counts[gen] = gen_counts.get(gen, 0) + 1

                        # Expansion criteria for the next generation.
                        # Only expand papers we can fetch by Scopus ID.
                        # min_citing applies to forward only (backward refs lack cbc).
                        if gen < generations and p['scopus_id']:
                            if direction == 'backward' or cbc >= min_citing:
                                next_to_expand.append(p['scopus_id'])

                to_expand = next_to_expand
                if not to_expand:
                    break

            records_list = list(all_papers.values())

            # Main-path analysis (SPC)
            mp_result = compute_main_path(records_list)
            main_path_ids = mp_result['main_path']
            spc_edges = mp_result['edges']

            # Consistent base filename for all three output files
            from datetime import datetime
            _ts = datetime.now().strftime('%Y%m%dT%H%M%S')
            _slug = _query_slug(f'lineage-{seed_id}')
            base_fname = f'scopus-{_slug}-{_ts}'

            json_path = write_lineage_to_disk(
                records_list, seed_id,
                main_path=main_path_ids, spc_edges=spc_edges,
                base_filename=base_fname,
            )
            html_path = render_lineage_html(records_list, main_path_ids, seed_id, base_fname)
            png_path = render_lineage_png(records_list, main_path_ids, seed_id, base_fname)

            non_seed = [r for r in records_list if r['generation'] > 0]
            top10 = sorted(
                non_seed,
                key=lambda r: int(r.get('cited_by_count') or 0),
                reverse=True,
            )[:10]

            gen_summary = ', '.join(
                f"gen {g}: {c}"
                for g, c in sorted(gen_counts.items())
                if g > 0
            )

            # Main-path labels: first-author surname + year if available
            mp_labels = []
            for nid in main_path_ids:
                rec = all_papers.get(nid, {})
                mp_labels.append(_make_node_label(rec, nid))

            all_fetches_failed = (
                fetch_attempts > 0
                and len(fetch_errors) == fetch_attempts
            )
            some_fetches_failed = fetch_errors and not all_fetches_failed

            if all_fetches_failed:
                # Don't report "no papers found" — report the real cause.
                first_err = fetch_errors[0][1]
                text = (
                    f"Citation lineage ({direction}) for {seed_id} "
                    f"({seed_paper.get('title', seed_id)!r}).\n"
                    f"Walk FAILED: all {len(fetch_errors)} node fetch(es) returned API errors.\n"
                    f"API calls: {api_calls}.\n"
                    f"Error: {first_err}\n"
                )
                if len(fetch_errors) > 1:
                    text += f"(and {len(fetch_errors) - 1} more failures)\n"
            else:
                text = (
                    f"Citation lineage ({direction}) for {seed_id} "
                    f"({seed_paper.get('title', seed_id)!r}).\n"
                    f"Generations walked: {generations}. "
                    f"Papers per generation: {gen_summary or 'none (no papers found)'}.\n"
                    f"Total unique papers (excl. seed): {len(non_seed)}. "
                    f"API calls: {api_calls}.\n"
                    f"Corpus written to: {json_path}\n"
                )
                if html_path:
                    text += f"Interactive HTML: {html_path}\n"
                if png_path:
                    text += f"PNG: {png_path}\n"
                if main_path_ids:
                    text += (
                        f"Main path ({len(main_path_ids)} nodes): "
                        + " → ".join(mp_labels) + "\n"
                    )
                elif mp_result.get('note'):
                    text += f"Main path: {mp_result['note']}\n"

                text += "\nTop 10 most-cited papers in lineage:\n"
                for r in top10:
                    text += (
                        f"  [gen {r['generation']}] {r.get('title', r['scopus_id'])!r} "
                        f"({r.get('year', '?')}, {r.get('venue', '?')}) "
                        f"— {r.get('cited_by_count', '?')} citations\n"
                    )
                if not top10:
                    text += "  (no papers found)\n"

            if some_fetches_failed:
                text += (
                    f"\nWarning: {len(fetch_errors)} node fetch(es) failed with API errors "
                    f"(partial walk — results may be incomplete). "
                    f"First error: {fetch_errors[0][1]}\n"
                )

            return [types.TextContent(type="text", text=text)]

        elif name == "get_fulltext":
            doi = (arguments.get("doi") or "").strip()
            if not doi:
                raise ValueError("doi is required")
            prefer = arguments.get("prefer")  # None → full waterfall

            text_body: Optional[str] = None
            provenance: str = "none"
            source_url: Optional[str] = None

            # ── Tier 1: ScienceDirect ────────────────────────────────────────
            if prefer in (None, "sciencedirect"):
                try:
                    sd_data = await client.get_sciencedirect_fulltext(doi)
                    if sd_data:
                        root = sd_data.get('full-text-retrieval-response') or {}
                        candidate = (root.get('originalText') or '').strip()
                        if len(candidate) > 500:
                            text_body = candidate
                            provenance = "sciencedirect-fulltext"
                except Exception as exc:
                    logger.warning(f"get_fulltext: SD tier error for doi={doi}: {exc}")

            # ── Tier 2: Open-access ──────────────────────────────────────────
            if text_body is None and prefer in (None, "oa"):
                oa_result = await fetch_oa_fulltext(doi)
                source_url = oa_result.get('source_url')
                if oa_result.get('text'):
                    text_body = oa_result['text']
                    provenance = "oa-fulltext"

            # ── Tier 3: Abstract fallback ────────────────────────────────────
            if text_body is None and prefer in (None, "abstract"):
                try:
                    raw_abs = await client.get_abstract_by(doi, id_type='doi')
                    details = clean_abstract_details(raw_abs)
                    description = details.get('description') or ''
                    if description:
                        text_body = description
                        provenance = "scopus-abstract"
                except Exception as exc:
                    logger.warning(f"get_fulltext: abstract tier error for doi={doi}: {exc}")

            # ── Build response ───────────────────────────────────────────────
            char_count = len(text_body) if text_body else 0
            file_path: Optional[str] = None

            # SD and OA full text always written to disk (can be large).
            # Short abstracts (<= 3000 chars) are returned inline.
            ABSTRACT_INLINE_MAX = 3000
            write_to_disk = text_body and provenance in ('sciencedirect-fulltext', 'oa-fulltext')
            if not write_to_disk and text_body and char_count > ABSTRACT_INLINE_MAX:
                write_to_disk = True
            if write_to_disk:
                file_path = write_fulltext_to_disk(doi, text_body)
                sample = text_body[:1500]
            else:
                sample = text_body or ''

            summary: dict = {
                'doi': doi,
                'provenance': provenance,
                'char_count': char_count,
            }
            if source_url:
                summary['source_url'] = source_url
            if file_path:
                summary['file_path'] = file_path
            summary['sample'] = sample

            import json as _json
            return [types.TextContent(type="text", text=_json.dumps(summary, ensure_ascii=False, indent=2))]

        elif name == "get_quota_status":
            quota = await client.get_quota_status()
            if not quota:
                return [types.TextContent(type="text", text="No quota information available yet. Please make a request to initialize.")]
            
            return [types.TextContent(type="text", text=str(quota))]

        else:
            raise ValueError(f"Unknown tool: {name}")

    except Exception as e:
        logger.error(f"Error executing tool {name}: {e}")
        return [types.TextContent(type="text", text=f"Error: {str(e)}")]

@server.list_prompts()
async def handle_list_prompts() -> list[types.Prompt]:
    return [
        types.Prompt(
            name="research-summary",
            description="Search for papers on a topic and generate a research summary",
            arguments=[
                types.PromptArgument(
                    name="topic",
                    description="The research topic (e.g., 'machine learning healthcare')",
                    required=True
                )
            ]
        ),
        types.Prompt(
            name="author-analysis",
            description="Analyze an author's research impact and recent work",
            arguments=[
                types.PromptArgument(
                    name="author_id",
                    description="The Scopus Author ID",
                    required=True
                )
            ]
        )
    ]

@server.get_prompt()
async def handle_get_prompt(
    name: str, arguments: dict[str, str] | None
) -> types.GetPromptResult:
    if not arguments:
        arguments = {}

    if name == "research-summary":
        topic = arguments.get("topic", "unknown topic")
        return types.GetPromptResult(
            description=f"Research summary for {topic}",
            messages=[
                types.PromptMessage(
                    role="user",
                    content=types.TextContent(
                        type="text",
                        text=f"Please search specifically for high-cited papers related to '{topic}' published in the last 5 years using the search_scopus tool. Sort by cited references if possible. After retrieving the results, please summarize the key trends and findings in this field."
                    )
                )
            ]
        )

    if name == "author-analysis":
        author_id = arguments.get("author_id", "")
        return types.GetPromptResult(
            description=f"Analysis of author {author_id}",
            messages=[
                types.PromptMessage(
                    role="user",
                    content=types.TextContent(
                        type="text",
                        text=f"Please call the get_author_profile tool for Author ID '{author_id}'. Based on the returned data, analyze their research impact (citations, h-index if available), identify their main affiliation, and summarize their academic standing."
                    )
                )
            ]
        )

    raise ValueError(f"Unknown prompt: {name}")

async def main():
    try:
        async with stdio_server() as (read_stream, write_stream):
            await server.run(
                read_stream,
                write_stream,
                server.create_initialization_options()
            )
    finally:
        # Ensure client is closed on shutdown
        await client.close()

def start():
    """Entry point for the package script."""
    asyncio.run(main())

if __name__ == "__main__":
    start()
