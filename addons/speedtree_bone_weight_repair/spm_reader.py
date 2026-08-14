"""Process-local, change-aware access to SpeedTree SPM XML data.

Blender repair consumes the same SPM in several independent stages.  SpeedTree
10 SPM files are gzip-compressed XML, so repeatedly opening, decompressing, and
parsing one unchanged file adds avoidable work.  This module keeps a small LRU
snapshot per process and invalidates it whenever the file stat identity changes.

Cached XML roots and derived values are read-only contracts.  Callers must not
mutate them.
"""

import gzip
import os
import xml.etree.ElementTree as ET
from collections import OrderedDict
from pathlib import Path
from threading import RLock


MAX_CACHE_ENTRIES = 4

_lock = RLock()
_xml_bytes_cache = OrderedDict()
_xml_root_cache = OrderedDict()
_derived_cache = OrderedDict()
_stats = {
    "file_reads": 0,
    "gzip_decompressions": 0,
    "xml_parses": 0,
    "derived_builds": 0,
    "cache_hits": 0,
}


def source_key(path):
    """Return a process-local invalidation key for one SPM file."""
    source = Path(path).resolve()
    stat = source.stat()
    return (
        os.path.normcase(str(source)),
        int(stat.st_size),
        int(stat.st_mtime_ns),
        int(getattr(stat, "st_ctime_ns", 0)),
    )


def _remember(cache, key, value):
    cache[key] = value
    cache.move_to_end(key)
    while len(cache) > MAX_CACHE_ENTRIES:
        cache.popitem(last=False)
    return value


def _cached(cache, key):
    try:
        value = cache[key]
    except KeyError:
        return None
    cache.move_to_end(key)
    _stats["cache_hits"] += 1
    return value


def read_spm_xml(path):
    """Return decoded SPM XML bytes, reusing an unchanged process snapshot."""
    key = source_key(path)
    with _lock:
        cached = _cached(_xml_bytes_cache, key)
        if cached is not None:
            return cached

        raw = Path(path).read_bytes()
        _stats["file_reads"] += 1
        if raw[:2] == b"\x1f\x8b":
            raw = gzip.decompress(raw)
            _stats["gzip_decompressions"] += 1
        return _remember(_xml_bytes_cache, key, raw)


def read_spm_root(path):
    """Return one shared, read-only ElementTree root for an unchanged SPM."""
    key = source_key(path)
    with _lock:
        cached = _cached(_xml_root_cache, key)
        if cached is not None:
            return cached
        root = ET.fromstring(read_spm_xml(path))
        _stats["xml_parses"] += 1
        return _remember(_xml_root_cache, key, root)


def get_derived(path, namespace, builder):
    """Build one read-only semantic view per unchanged SPM and namespace."""
    source = source_key(path)
    key = (str(namespace), source)
    with _lock:
        cached = _cached(_derived_cache, key)
        if cached is not None:
            return cached
        value = builder(read_spm_root(path))
        _stats["derived_builds"] += 1
        return _remember(_derived_cache, key, value)


def cache_info():
    """Return serializable diagnostics for repair reports and tests."""
    with _lock:
        return {
            **_stats,
            "xml_byte_snapshots": len(_xml_bytes_cache),
            "xml_root_snapshots": len(_xml_root_cache),
            "derived_snapshots": len(_derived_cache),
            "max_entries": MAX_CACHE_ENTRIES,
        }


def clear_cache():
    """Clear process snapshots; intended for deterministic tests."""
    with _lock:
        _xml_bytes_cache.clear()
        _xml_root_cache.clear()
        _derived_cache.clear()
        for name in _stats:
            _stats[name] = 0
