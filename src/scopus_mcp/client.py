import logging
import asyncio
import math
import httpx
from typing import Optional, Dict, Any
from urllib.parse import urljoin

from .config import get_api_key, get_cache_config, get_insttoken, get_page_size
from .cache import CacheManager
from .utils import to_scopus_id, to_eid

# Setup basic logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

BASE_URL = "https://api.elsevier.com/"

class ScopusClient:
    """
    Async client for interacting with the Elsevier Scopus API.
    Handles authentication, caching, rate limiting, and retries.
    """
    def __init__(self):
        self.api_key = get_api_key()
        self.cache_config = get_cache_config()
        # Per-request page size for search_all; 25 by default (see get_page_size).
        self.page_size = get_page_size()
        insttoken = get_insttoken()
        self.headers = {
            'X-ELS-APIKey': self.api_key,
            'Accept': 'application/json',
            'User-Agent': 'ScopusMCP/0.7.0',
        }
        if insttoken:
            self.headers['X-ELS-Insttoken'] = insttoken
        # Initialize CacheManager with default expiration
        self.cache = CacheManager(expiration_seconds=self.cache_config['default'])
        self.client = httpx.AsyncClient(
            headers=self.headers,
            timeout=30.0,
            follow_redirects=True
        )
        self.quota_info = {} # Store latest quota headers

    async def close(self):
        """Closes the underlying HTTP client."""
        await self.client.aclose()

    async def get_quota_status(self) -> Dict[str, Any]:
        """Returns the latest known quota status."""
        return self.quota_info

    async def _request(self, method: str, endpoint: str, params: Optional[Dict[str, Any]] = None, use_cache: bool = True, ttl: Optional[int] = None) -> Dict[str, Any]:
        """
        Internal method to handle API requests with caching, rate limiting, and retries.
        """
        url = urljoin(BASE_URL, endpoint)
        
        # Check cache (Synchronous cache access is fast enough)
        if use_cache and method.upper() == 'GET':
            cached = self.cache.get(url, params)
            if cached:
                logger.debug(f"Cache hit for {url}")
                return cached

        retries = 3
        backoff = 1

        while retries > 0:
            try:
                response = await self.client.request(method, url, params=params)
                
                # Update Quota Info from Headers
                self._update_quota_info(response.headers)
                
                # Handle Rate Limiting
                if response.status_code == 429:
                    if retries <= 1:
                        quota_snap = {k: response.headers.get(k, '') for k in (
                            'X-RateLimit-Remaining', 'X-RateLimit-Reset', 'X-ELS-Status')}
                        raise Exception(
                            f"Rate limit exceeded (429) after retries. "
                            f"Quota headers: {quota_snap}"
                        )
                    import time
                    reset_time = int(response.headers.get('X-RateLimit-Reset', 0))
                    sleep_time = max(reset_time - time.time(), backoff)
                    logger.warning(f"Rate limit exceeded. Retrying in {sleep_time:.1f}s...")
                    await asyncio.sleep(sleep_time)
                    retries -= 1
                    backoff *= 2
                    continue

                response.raise_for_status()
                data = response.json()

                # Save to cache if GET
                if use_cache and method.upper() == 'GET':
                    self.cache.set(url, data, params, ttl=ttl)

                return data

            except httpx.HTTPStatusError as e:
                # Update quota info even on error if headers exist
                if e.response:
                    self._update_quota_info(e.response.headers)
                    
                status = e.response.status_code
                if status in [500, 502, 503, 504]:
                    logger.warning(f"Server error {status}. Retrying in {backoff}s...")
                    await asyncio.sleep(backoff)
                    retries -= 1
                    backoff *= 2
                elif status == 401:
                    logger.error("Authentication failed. Check your API key.")
                    is_ref = params is not None and params.get('view') == 'REF'
                    quota_snap = {k: e.response.headers.get(k, '') for k in (
                        'X-ELS-Status', 'X-RateLimit-Remaining',
                        'X-RateLimit-Reset', 'X-ELS-Quota-Remaining-Weekly')}
                    quota_str = ', '.join(f'{k}={v}' for k, v in quota_snap.items() if v)
                    if is_ref:
                        msg = (
                            "REF-view fetch failed: Invalid API Key — likely a "
                            "REF-view entitlement or quota limit, not a bad key "
                            "(key works for other endpoints)."
                        )
                    else:
                        msg = "Authentication failed: Invalid API Key"
                    if quota_str:
                        msg += f" Quota/rate headers: [{quota_str}]"
                    raise Exception(msg) from e
                elif status == 404:
                    logger.info(f"Resource not found: {url}")
                    return {} 
                else:
                    # Surface the response body and the query so malformed-syntax
                    # 400s (bad REF()/field syntax) are distinguishable from
                    # entitlement 403s without guesswork.
                    body = ''
                    try:
                        body = e.response.text[:500]
                    except Exception:
                        pass
                    q = params.get('query') if params else None
                    raise Exception(
                        f"Scopus API error {status} for {url} "
                        f"(query={q!r}): {body}"
                    ) from e
            except httpx.RequestError as e:
                logger.warning(f"Request failed: {e}. Retrying...")
                await asyncio.sleep(backoff)
                retries -= 1
                backoff *= 2
            except ValueError:
                logger.error("Failed to parse JSON response")
                raise Exception("Invalid JSON response from Scopus API")

        raise Exception(f"Max retries exceeded for {url}")

    def _update_quota_info(self, headers: httpx.Headers):
        """Updates internal quota state from response headers."""
        self.quota_info = {
            'limit': headers.get('X-RateLimit-Limit', 'unknown'),
            'remaining': headers.get('X-RateLimit-Remaining', 'unknown'),
            'reset': headers.get('X-RateLimit-Reset', 'unknown'),
            'status': 'OK'
        }

    async def search_scopus(self, query: str, count: int = 25, start: int = 0, sort: str = 'coverDate') -> Dict[str, Any]:
        """
        Searches Scopus API.
        Endpoint: content/search/scopus
        """
        params = {
            'query': query,
            'count': count,
            'start': start,
            'sort': sort,
            'view': 'STANDARD'
        }
        return await self._request('GET', 'content/search/scopus', params, ttl=self.cache_config['search'])

    async def get_abstract(self, scopus_id: str) -> Dict[str, Any]:
        """
        Retrieves abstract details.
        Endpoint: content/abstract/scopus_id/{id}
        """
        clean_id = scopus_id.replace('SCOPUS_ID:', '')
        return await self._request('GET', f'content/abstract/scopus_id/{clean_id}', ttl=self.cache_config['abstract'])

    async def get_author(self, author_id: str) -> Dict[str, Any]:
        """
        Retrieves author profile.
        Endpoint: content/author/author_id/{id}
        """
        clean_id = author_id.replace('AUTHOR_ID:', '')
        return await self._request('GET', f'content/author/author_id/{clean_id}', ttl=self.cache_config['author'])

    async def get_abstract_by(self, value: str, id_type: str = 'scopus_id') -> Dict[str, Any]:
        """
        Retrieves an abstract record by any supported identifier type.
        Endpoint: content/abstract/{id_type}/{value}

        id_type is one of: 'scopus_id', 'eid', 'doi', 'pii'.
        Used by resolve_identifier to cross-reference IDs (Scopus ID / EID / DOI).
        """
        id_type = (id_type or 'scopus_id').lower()
        if id_type == 'scopus_id':
            endpoint = f"content/abstract/scopus_id/{to_scopus_id(value)}"
        elif id_type == 'eid':
            endpoint = f"content/abstract/eid/{str(value).strip()}"
        elif id_type == 'doi':
            endpoint = f"content/abstract/doi/{str(value).strip()}"
        elif id_type == 'pii':
            endpoint = f"content/abstract/pii/{str(value).strip()}"
        else:
            raise ValueError(f"Unsupported id_type: {id_type}")
        return await self._request('GET', endpoint, ttl=self.cache_config['abstract'])

    async def get_references(self, scopus_id: str) -> Dict[str, Any]:
        """
        Retrieves the cited-reference list (backward citations) for a document
        via the Abstract Retrieval REF view.
        Endpoint: content/abstract/scopus_id/{id}?view=REF

        Note: the REF view requires an entitled (subscriber) key; an
        unentitled key returns 403, surfaced as an error by _request.
        Deeper paging of long reference lists uses the 'startref' parameter.
        """
        params = {'view': 'REF'}
        return await self._request(
            'GET',
            f"content/abstract/scopus_id/{to_scopus_id(scopus_id)}",
            params,
            ttl=self.cache_config['abstract'],
        )

    async def search_all(self, query: str, max_results: int = 200, sort: str = 'coverDate') -> Dict[str, Any]:
        """
        Pages through a Scopus search and returns aggregated results up to max_results.

        Uses start-based paging (ceiling 5,000) when max_results <= 5,000.  Switches to
        cursor=* deep paging when max_results > 5,000 — the two modes are mutually
        exclusive per Scopus API rules.  Deduplicates across pages by dc:identifier.

        Page size is self.page_size — 25 by default, the per-request 'count' ceiling for
        non-institutional keys; see config.get_page_size for raising it.

        Paging is guaranteed to terminate: each loop stops on an empty page, a short
        page, a page that adds no new identifiers, a cursor that fails to advance, or
        a hard page cap.  Without those guards a misbehaving API — one returning a full
        batch alongside an unchanging @next cursor — would spin forever.
        """
        page_size = max(1, int(self.page_size))
        CURSOR_CEILING = 5000

        all_entries: list = []
        seen_ids: set = set()
        total_available: int = 0
        note: Optional[str] = None

        # Backstop for pathological paging.  Honest paging needs at most
        # ceil(max_results / page_size) requests; double that plus slack so
        # duplicate-heavy result sets still page through normally, and treat
        # anything beyond it as the API failing to make progress.
        max_pages = math.ceil(max_results / page_size) * 2 + 10
        pages = 0
        hit_page_cap = False

        use_cursor = max_results > CURSOR_CEILING

        if use_cursor:
            cursor: str = '*'
            while len(all_entries) < max_results:
                if pages >= max_pages:
                    hit_page_cap = True
                    break
                pages += 1
                batch = min(page_size, max_results - len(all_entries))
                params: Dict[str, Any] = {
                    'query': query,
                    'count': batch,
                    'cursor': cursor,
                    'sort': sort,
                    'view': 'STANDARD',
                }
                data = await self._request(
                    'GET', 'content/search/scopus', params,
                    use_cache=True, ttl=self.cache_config['search'],
                )
                sr = data.get('search-results', {})
                if not total_available:
                    try:
                        total_available = int(sr.get('opensearch:totalResults', 0))
                    except (ValueError, TypeError):
                        pass
                entries = sr.get('entry', [])
                if not entries:
                    break
                added = 0
                for e in entries:
                    uid = e.get('dc:identifier') or e.get('eid') or ''
                    if uid not in seen_ids:
                        seen_ids.add(uid)
                        all_entries.append(e)
                        added += 1
                next_cursor = (sr.get('cursor') or {}).get('@next')
                if not next_cursor:
                    break
                if len(entries) < batch:
                    break  # API returned fewer than requested — results exhausted
                if next_cursor == cursor:
                    # The cursor has stopped advancing: following it again would
                    # re-fetch this exact page forever.
                    logger.warning(
                        f"Deep paging stopped: @next cursor did not advance past {cursor!r}."
                    )
                    break
                if added == 0:
                    break  # Page contained only records already seen
                cursor = next_cursor
        else:
            start = 0
            while len(all_entries) < max_results and start < CURSOR_CEILING:
                if pages >= max_pages:
                    hit_page_cap = True
                    break
                pages += 1
                batch = min(page_size, max_results - len(all_entries), CURSOR_CEILING - start)
                data = await self.search_scopus(query, count=batch, start=start, sort=sort)
                sr = data.get('search-results', {})
                if not total_available:
                    try:
                        total_available = int(sr.get('opensearch:totalResults', 0))
                    except (ValueError, TypeError):
                        pass
                entries = sr.get('entry', [])
                if not entries:
                    break
                added = 0
                for e in entries:
                    uid = e.get('dc:identifier') or e.get('eid') or ''
                    if uid not in seen_ids:
                        seen_ids.add(uid)
                        all_entries.append(e)
                        added += 1
                start += len(entries)
                if len(entries) < batch or added == 0:
                    break  # Exhausted or only duplicates returned

        fetched = len(all_entries)
        truncated = bool(total_available and fetched < total_available)
        if truncated:
            note = (
                f"Result set capped: fetched {fetched} of {total_available} total "
                f"(max_results={max_results})."
            )
        if hit_page_cap:
            cap_note = (
                f"Paging stopped at the {max_pages}-page safety cap without reaching "
                f"max_results={max_results}: the API kept returning pages that added "
                f"few or no new records."
            )
            logger.warning(cap_note)
            note = f"{note} {cap_note}" if note else cap_note

        return {
            'search-results': {'entry': all_entries},
            '_meta': {
                'total_fetched': fetched,
                'total_available': total_available,
                'truncated': truncated,
                'pages_fetched': pages,
                'hit_page_cap': hit_page_cap,
                'note': note,
            },
        }

    async def get_sciencedirect_fulltext(self, doi: str) -> Optional[Dict[str, Any]]:
        """
        Retrieve the ScienceDirect full-text-retrieval-response for a DOI.
        Returns None when the caller lacks entitlement (401/403) or the article
        is not on ScienceDirect (404).  Requires SCOPUS_INSTTOKEN to be set for
        most full-text content; without it the response is typically abstract-only.
        Endpoint: content/article/doi/{doi}
        """
        try:
            return await self._request(
                'GET',
                f'content/article/doi/{doi.strip()}',
                use_cache=False,
            )
        except Exception as exc:
            msg = str(exc).lower()
            if any(code in msg for code in ('401', '403', 'authentication', 'entitlement')):
                logger.info(f"ScienceDirect fulltext not entitled for doi={doi}: {exc}")
                return None
            raise

    async def get_citing_papers(self, scopus_id: str, count: int = 25, start: int = 0, sort: str = 'coverDate') -> Dict[str, Any]:
        """
        Retrieves forward citations via a Search API REF() query.
        Builds REF(2-s2.0-<id>) using the centralized EID normalization,
        which the Search API accepts (the bare-id REFEID(<id>) form 400s).
        """
        query = f"REF({to_eid(scopus_id)})"
        return await self.search_scopus(query, count=count, start=start, sort=sort)
