# Scopus MCP — Roadmap & Cross-Source Design

Living plan for the `michalhron/scopus-mcp` fork and how it feeds the
`mechanism-inheritance-audit` skill. Drop this in the repo root so it
survives independent of any chat history.

_Last updated: 2026-06-24, end of build session._

## Current state (verified live unless noted)

Branch: `feat/network-builders` @ `e8b634b`, version `0.5.1`, pushed to origin.

Built and live-verified:

- **`resolve_identifier`** — Scopus ID / EID / DOI / PII → cross-reference set.
  DOI is the join key for all cross-source work.
- **`get_references`** — backward citations via Abstract Retrieval REF view.
  Parser fixed in 0.5.1 (see "Lessons" below); 40/40 refs extract cleanly.
- **`get_citing_papers`** — forward citations, `REF(2-s2.0-<id>)` (the original
  `REFEID(<id>)` 400'd).
- **`search_all`** — transparent multi-page fetch. Verified: 5,001 records,
  start-paging <=5,000 then `cursor=*` beyond, deduped by identifier.
- **File-output contract** — `write_results_to_disk` / `should_write_to_disk`
  in `utils.py`; sets >50 records write JSON+CSV to `SCOPUS_MCP_OUTPUT_DIR`
  (else `~/scopus-mcp-output`) and return a compact summary + paths + sample.
- **`bibliographic_coupling`** — backward: shared references -> research front.
  `compute_pairwise_edges` (Salton cosine + raw count), `write_graph_to_disk`
  (GraphML + CSV). Built; NOT yet run live on a real seed set (see "Next").
- **`co_citation`** — forward: shared citing papers -> intellectual base.
  Same helpers. Built; NOT yet run live.
- Centralized EID normalization, empty-set fix (returns `[]`, no phantom),
  informative API error surfacing.

## NEXT SESSION — start here

1. **Repin** Claude Desktop `scopus-assistant` to `@e8b634b` (new config block,
   just swap the SHA), full Cmd+Q restart.
2. **Run the first real networks** on the organizing-vision seed set:
   - `bibliographic_coupling(seed_ids=SEEDS, min_shared=2)`
   - `co_citation(seed_ids=SEEDS, min_shared=2)`
   - Same seeds, two directions — compare how they cluster.
3. **Open the GraphML in VOSviewer** — the one check no unit test can do
   (proves the XML is well-formed for picky parsers, not just `xml.etree`).
4. If clusters look right, **Session A is done** -> move to Session D.

Organizing-vision seed set (14 papers, 2020–2026, pulled live this session):
```
85186605555  105014870625  85151965536  85097715634  85216793920
85195678374  85192371054   105026555771 85098247765   105029501243
85208706582  105029252067  105011354071 105033695390
```

## Lessons (the recurring tax)

Three times now, Claude Code's mocked tests passed while the live Scopus
contract failed: (1) cursor paging cap, (2) the 1 MB tool-return limit,
(3) the REF parser. Mocked tests prove *logic*; only a live call proves the
*shape*. **Rule: every new tool gets a live smoke test before we trust it.**

The REF parser bug specifically: real REF view uses FLAT keys
(`scopus-id`, `ce:doi`, `title`, `sourcetitle`, `prism:coverDate`,
`author-list.author[].ce:indexed-name`, dedup by `@auid`), NOT the nested
`ref-info`/`refd-itemidlist` structure originally assumed. The 0.5.1 test now
asserts a parsed ref has `scopus_id` or `doi`, so this can't regress silently.

## Housekeeping (not urgent)

Branch stack has grown: `feat/search-all` -> `fix/result-return-contract` ->
`feat/network-builders`, chained. Open PRs and merge down to `main` before
this sprawls; then pin Desktop to `main` rather than a feature branch.

## Cross-source principle: Scopus = structure, Springer = content

`resolve_identifier` -> DOI -> SpringerLink (`get_article`,
`get_open_access_fulltext`). Scopus knows the citation graph; Springer knows
what papers *say* (real abstracts, OA full text). Citation tracing has never
seen inside the papers — this pairing does.

**Coverage caveat that shapes which ideas are real:** Springer's IS footprint is
partial. Basket of Eight is mostly INFORMS/Wiley/T&F, not Springer — for
MISQ/ISR, fall back to Scopus abstracts or triangulate via OpenAlex/Crossref.
Springer lands well on BISE, Electronic Markets, IS Frontiers, and the
LNCS/LNBIP conference world (e.g. the PoEM paper, the DPP papers in Electronic
Markets). Full-text-dependent ideas work best on the design-science / Euro-IS /
conference slice and degrade gracefully elsewhere.

### Ideas on top of the foundation

- **Annotated network maps.** Name coupling/co-citation clusters by their shared
  Springer abstract content, not raw keywords.
- **Citation-context / predicate test.** With OA full text, check whether a
  citing paper engages the seed construct substantively (load-bearing in the
  theory section) or just name-drops it. Scopus says "X cited Y"; Springer text
  says *how* — real inheritance vs. ritual citation.
- **Bridge-paper reading pipeline.** Map network (Scopus) -> high-betweenness
  papers -> full text (Springer) for just those -> summarize. Read 8 pivotal
  papers, not 200 abstracts.

## How this feeds `mechanism-inheritance-audit` (the convergence point)

Today the audit runs on the handful of papers Michal has personally read. The
pipeline makes it corpus-scale and empirical:

1. **Seed** a paper or construct (organizing vision, swift trust).
2. **`search_all`** the forward-citation lineage across generations (no 25-cap).
3. **`resolve_identifier`** each citing paper -> DOI.
4. **SpringerLink** pull abstracts / OA full text per citing cohort.
5. **Feed the text** into the inheritance classifier to judge
   *survives / reinterprets / breaks* per cohort — on content, not titles.
6. **Predicate test** uses OA full text to confirm the mechanism's predicates
   actually survive in the citing work, not just the label.

Scopus supplies the lineage skeleton; Springer supplies the flesh. The
difference between an anecdote and a corpus-scale finding for **"What Inherits?
A Forcing Audit for the IS Cumulative Tradition"** (CAIS Debate, Sept 2026).

## Build order (do not reorder)

Session 0 (file contract) DONE -> A (network builders) BUILT, needs live run ->
D (orchestrator skill) -> B / C anytime. Each later tool reuses the file-output
helper; design it once.

---

## SESSION D — orchestrator skill prompt (ready to paste into Claude Code)

> Note: build the lineage-walker tool (multi-generation forward traversal) as
> part of this, OR as a small Session A.5 first — the orchestrator needs it.
> The current `get_citing_papers` / `search_all` are single-hop only.

---

I want to build a cross-source bibliometric orchestrator as a SKILL (not server
code), in my skills directory, that chains my Scopus MCP server and my
SpringerLink MCP server to run mechanism-inheritance audits at corpus scale.
Read my existing `mechanism-inheritance-audit` skill first to match its
structure, vocabulary (survives / reinterprets / breaks, predicate test,
forcing analogy), and output format — this skill orchestrates data collection
and hands the assembled corpus to that existing classifier; it does not
reimplement the judgment.

The skill's pipeline, given a seed paper (Scopus ID or DOI) and a construct name:

1. Resolve the seed via `resolve_identifier` to get its DOI and metadata.
2. Walk the forward-citation lineage with `search_all` on `REF(2-s2.0-<id>)`.
   Support N generations (default 1, optional 2): generation 1 = direct citers;
   generation 2 = citers of those, deduped against generation 1. Tag every
   citing paper with its generation, year, and venue. Cap per generation to
   bound quota (default 300).
3. For each citing paper, resolve its DOI.
4. For papers whose DOI is in Springer's coverage, pull the abstract via the
   SpringerLink MCP (`get_article`), and OA full text via
   `get_open_access_fulltext` where available. For non-Springer DOIs, fall back
   to the Scopus abstract. Record provenance (springer-fulltext /
   springer-abstract / scopus-abstract / none) per paper.
5. Group citing papers into cohorts by generation x (year band or venue tier).
6. Assemble a corpus file (reuse the Scopus file-output convention:
   `SCOPUS_MCP_OUTPUT_DIR`, JSON) with one record per citing paper:
   scopus_id, doi, title, year, venue, generation, provenance, abstract/text.
7. Hand each cohort's text to the `mechanism-inheritance-audit` classifier to
   judge survives / reinterprets / breaks for the named construct, and where OA
   full text exists, run the predicate test (does the citing paper engage the
   construct substantively or name-drop it).
8. Return: a cohort-by-cohort inheritance table (generation, cohort, verdict,
   evidence-strength flag based on provenance), the corpus file path, and a list
   of the highest-leverage papers (full-text-available + high citation) to read.

Be explicit about the coverage caveat: Springer IS coverage is partial, so most
verdicts on Basket-of-Eight-heavy lineages will rest on abstracts, not full
text — flag evidence strength accordingly and never present an abstract-only
verdict as if it had full-text backing.

This is a design-and-scaffold task; don't run the full pipeline live yet — build
the skill, stub the tool calls with the real signatures, and give me a dry-run
plan on the organizing-vision seed before we spend quota on a real corpus.

---
