import json
import os
from pathlib import Path
from typing import Dict, Any, Optional
from dotenv import load_dotenv

# Load environment variables from .env file if present
load_dotenv()

# Define base paths
BASE_DIR = Path(__file__).resolve().parent.parent.parent
CONFIG_FILE = BASE_DIR / 'config.json'

def load_config_file() -> Dict[str, Any]:
    """Loads the configuration from config.json if it exists."""
    if CONFIG_FILE.exists():
        try:
            with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
                return json.load(f)
        except json.JSONDecodeError:
            return {}
    return {}

def get_api_key() -> str:
    """
    Retrieves the API key with the following precedence:
    1. Environment variable 'SCOPUS_API_KEY'
    2. config.json 'api_key' field
    """
    # 1. Check Environment Variable
    api_key = os.getenv('SCOPUS_API_KEY')
    if api_key:
        return api_key

    # 2. Check config.json
    config = load_config_file()
    api_key = config.get('api_key')
    
    if not api_key:
        raise ValueError(
            "Scopus API Key not found. Please set the 'SCOPUS_API_KEY' environment variable "
            "or add 'api_key' to config.json."
        )
        
    return api_key

def get_insttoken() -> Optional[str]:
    """
    Retrieves the optional institutional token with the following precedence:
    1. Environment variable 'SCOPUS_INSTTOKEN'
    2. config.json 'insttoken' field
    Returns None when not configured.
    """
    token = os.getenv('SCOPUS_INSTTOKEN')
    if token:
        return token
    config = load_config_file()
    return config.get('insttoken') or None


DEFAULT_PAGE_SIZE = 25
MAX_PAGE_SIZE = 200


def get_page_size() -> int:
    """
    Retrieves the per-request page size used by ScopusClient.search_all:
    1. Environment variable 'SCOPUS_PAGE_SIZE'
    2. config.json 'page_size' field
    3. Default 25

    25 is the 'count' ceiling for non-institutional Scopus keys — asking for
    more returns 400 INVALID_INPUT.  Institutional (subscriber) keys accept up
    to 200, which cuts request count, and therefore quota burn, by 8x.  That
    entitlement cannot be detected from the key or from the presence of an
    insttoken, so raising the page size is an explicit opt-in.

    Non-numeric values fall back to the default; numeric values are clamped to
    1..200.
    """
    config = load_config_file()
    raw = os.getenv('SCOPUS_PAGE_SIZE', config.get('page_size', DEFAULT_PAGE_SIZE))
    try:
        size = int(raw)
    except (TypeError, ValueError):
        return DEFAULT_PAGE_SIZE
    return max(1, min(size, MAX_PAGE_SIZE))


def get_cache_config() -> Dict[str, int]:
    """
    Retrieves cache expiration settings from env vars or config.json.
    Defaults:
    - Search: 1 hour (3600s)
    - Abstract: 30 days (2592000s)
    - Author: 7 days (604800s)
    - Default: 24 hours (86400s)
    """
    config = load_config_file()
    
    return {
        'search': int(os.getenv('CACHE_TTL_SEARCH', config.get('cache_ttl_search', 3600))),
        'abstract': int(os.getenv('CACHE_TTL_ABSTRACT', config.get('cache_ttl_abstract', 2592000))),
        'author': int(os.getenv('CACHE_TTL_AUTHOR', config.get('cache_ttl_author', 604800))),
        'default': int(os.getenv('CACHE_TTL_DEFAULT', config.get('cache_ttl_default', 86400)))
    }
