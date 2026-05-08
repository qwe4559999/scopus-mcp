import json
import hashlib
import time
import os
from pathlib import Path
from typing import Optional, Dict, Any, Union


def _default_cache_dir() -> Path:
    if os.name == 'nt':
        base = Path(os.environ.get('LOCALAPPDATA', Path.home() / 'AppData' / 'Local'))
    else:
        base = Path(os.environ.get('XDG_CACHE_HOME', Path.home() / '.cache'))
    return base / 'scopus-mcp'


class CacheManager:
    def __init__(self, cache_dir: Union[str, Path] = None, expiration_seconds: int = 86400):
        if cache_dir is None:
            cache_dir = _default_cache_dir()
        self.cache_dir = Path(cache_dir).resolve()
        self.expiration_seconds = expiration_seconds
        self.cache_dir.mkdir(parents=True, exist_ok=True)

    def _get_cache_key(self, url: str, params: Optional[Dict[str, Any]] = None) -> str:
        """Generates a SHA256 hash key based on URL and sorted parameters."""
        key_str = url
        if params:
            # Sort keys to ensure deterministic hashing
            key_str += json.dumps(params, sort_keys=True, default=str)
        return hashlib.sha256(key_str.encode('utf-8')).hexdigest()

    def get(self, url: str, params: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
        """Retrieves data from cache if it exists and is not expired."""
        key = self._get_cache_key(url, params)
        file_path = self.cache_dir / f"{key}.json"

        if not file_path.exists():
            return None

        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                cached_entry = json.load(f)
            
            timestamp = cached_entry.get('timestamp', 0)
            ttl = cached_entry.get('ttl', self.expiration_seconds) # Use stored TTL or default
            
            if time.time() - timestamp > ttl:
                return None # Expired
            
            return cached_entry.get('data')
        except (json.JSONDecodeError, IOError):
            return None

    def set(self, url: str, data: Any, params: Optional[Dict[str, Any]] = None, ttl: Optional[int] = None) -> None:
        """Saves data to the cache with a timestamp and optional TTL."""
        key = self._get_cache_key(url, params)
        file_path = self.cache_dir / f"{key}.json"
        
        cache_entry = {
            'timestamp': time.time(),
            'ttl': ttl if ttl is not None else self.expiration_seconds,
            'data': data
        }
        
        try:
            with open(file_path, 'w', encoding='utf-8') as f:
                json.dump(cache_entry, f, ensure_ascii=False, indent=2)
        except IOError:
            # Silently fail on cache write errors to avoid crashing the app
            pass
