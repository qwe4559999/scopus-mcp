"""Tests for the identifier helpers and new response cleaners.

These cover pure parsing/normalization logic and need no network or API key.
"""
from scopus_mcp.utils import (
    to_scopus_id,
    to_eid,
    detect_id_type,
    clean_identifiers,
    clean_references,
    clean_search_results,
)


def test_to_scopus_id_strips_prefixes():
    assert to_scopus_id("0031512927") == "0031512927"
    assert to_scopus_id("SCOPUS_ID:0031512927") == "0031512927"
    assert to_scopus_id("2-s2.0-0031512927") == "0031512927"


def test_to_eid_builds_and_preserves():
    assert to_eid("0031512927") == "2-s2.0-0031512927"
    assert to_eid("SCOPUS_ID:0031512927") == "2-s2.0-0031512927"
    assert to_eid("2-s2.0-0031512927") == "2-s2.0-0031512927"


def test_detect_id_type():
    assert detect_id_type("2-s2.0-0031512927") == "eid"
    assert detect_id_type("10.1287/isre.8.4.458") == "doi"
    assert detect_id_type("0031512927") == "scopus_id"
    assert detect_id_type("SCOPUS_ID:0031512927") == "scopus_id"


def test_clean_identifiers_extracts_crossrefs():
    raw = {
        "abstracts-retrieval-response": {
            "coredata": {
                "dc:identifier": "SCOPUS_ID:0031512927",
                "eid": "2-s2.0-0031512927",
                "prism:doi": "10.1287/isre.8.4.458",
                "dc:title": "The organizing vision in information systems innovation",
                "prism:publicationName": "Information Systems Research",
                "citedby-count": "1234",
            }
        }
    }
    out = clean_identifiers(raw)
    assert out["scopus_id"] == "0031512927"
    assert out["eid"] == "2-s2.0-0031512927"
    assert out["doi"] == "10.1287/isre.8.4.458"
    assert out["cited_by_count"] == "1234"


def test_clean_identifiers_synthesizes_eid_when_absent():
    raw = {"abstracts-retrieval-response": {"coredata": {"dc:identifier": "SCOPUS_ID:55555"}}}
    assert clean_identifiers(raw)["eid"] == "2-s2.0-55555"


def test_clean_identifiers_empty_on_garbage():
    assert clean_identifiers({}) == {}
    assert clean_identifiers({"nope": 1}) == {}


def test_clean_references_parses_ref_view():
    # Fixture uses the real flat Scopus REF view structure (no ref-info nesting)
    raw = {
        "abstracts-retrieval-response": {
            "references": {
                "reference": [
                    {
                        "@id": "1",
                        "title": "Toward a model of organizations",
                        "sourcetitle": "Academy of Management Review",
                        "prism:coverDate": "1984-01-01",
                        "scopus-id": "0021488089",
                        "ce:doi": "10.2307/258441",
                        "author-list": {
                            "author": [{"ce:indexed-name": "Daft R.L.", "@auid": "111"}]
                        },
                    }
                ]
            }
        }
    }
    refs = clean_references(raw)
    assert len(refs) == 1
    r = refs[0]
    assert r["position"] == "1"
    assert r["title"] == "Toward a model of organizations"
    assert r["year"] == "1984"
    assert r["scopus_id"] == "0021488089"
    assert r["doi"] == "10.2307/258441"
    assert r["authors"] == ["Daft R.L."]
    # At least one usable identifier must be present for network-analysis tools
    assert r["scopus_id"] is not None or r["doi"] is not None


def test_clean_references_handles_single_dict_and_limit():
    # reference returned as a dict (not list) must still be parsed
    raw = {
        "abstracts-retrieval-response": {
            "references": {
                "reference": {
                    "@id": "1",
                    "title": "Solo ref",
                    "scopus-id": "9999999",
                }
            }
        }
    }
    refs = clean_references(raw)
    assert len(refs) == 1 and refs[0]["title"] == "Solo ref"
    assert refs[0]["scopus_id"] == "9999999"


def test_clean_references_empty_on_garbage():
    assert clean_references({}) == []
    assert clean_references({"abstracts-retrieval-response": {}}) == []


# ---------------------------------------------------------------------------
# Bug 1: clean_search_results empty-set / error-sentinel handling
# ---------------------------------------------------------------------------

def test_clean_search_results_empty_on_total_zero():
    """totalResults '0' must return [] even when entry contains an error object."""
    data = {
        'search-results': {
            'opensearch:totalResults': '0',
            'entry': [{'error': 'Result set was empty'}],
        }
    }
    assert clean_search_results(data) == []


def test_clean_search_results_empty_on_error_entry_without_identifier():
    """An entry with an 'error' key and no dc:identifier is silently dropped."""
    data = {
        'search-results': {
            'opensearch:totalResults': '1',   # API may still say 1
            'entry': [{'error': 'Result set was empty'}],
        }
    }
    assert clean_search_results(data) == []


def test_clean_search_results_keeps_real_entry_alongside_error_entry():
    """A real entry that co-exists with an error entry is preserved."""
    data = {
        'search-results': {
            'opensearch:totalResults': '2',
            'entry': [
                {'error': 'some sentinel'},
                {'dc:identifier': 'SCOPUS_ID:99', 'dc:title': 'Real paper'},
            ],
        }
    }
    result = clean_search_results(data)
    assert len(result) == 1
    assert result[0]['scopus_id'] == '99'
