"""Verify existing server recovery bytes before an opted-in SPM mutation.

This helper does not check out, shelve, submit, sync, or restore any file.
SPEEDTREE_PERFORCE_CLIENT selects the workspace (default: ArtSources).
SPEEDTREE_PERFORCE_SHELF may name an already uploaded numbered shelf.
"""
from pathlib import Path
import hashlib
import io
import marshal
import os
import re
import subprocess


def _p4(client, *args):
    result = subprocess.run(
        [os.environ.get('P4_EXECUTABLE', 'p4'), '-c', client, *args],
        capture_output=True, timeout=60,
        creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    if result.returncode:
        raise RuntimeError('Perforce recovery check failed: ' +
                           result.stderr.decode('utf-8', errors='replace').strip())
    return result.stdout


def _records(data):
    def decode(value):
        if not isinstance(value, bytes):
            return value
        try:
            return value.decode('utf-8')
        except UnicodeDecodeError:
            return value.decode('cp949')

    stream, rows = io.BytesIO(data), []
    while stream.tell() < len(data):
        raw = marshal.load(stream)
        row = {decode(k): decode(v) for k, v in raw.items()}
        if row.get('code') == 'error':
            raise RuntimeError('Perforce recovery check failed: ' + row.get('data', ''))
        if row.get('code') == 'stat':
            rows.append(row)
    return rows


def _mapping(path, client):
    rows = _records(_p4(client, '-G', 'where', str(path)))
    mapped = [r for r in rows if 'unmap' not in r and r.get('path') and r.get('depotFile')]
    if len(mapped) != 1 or Path(mapped[0]['path']).resolve() != path.resolve():
        raise RuntimeError('SPM does not have one exact Perforce workspace mapping')
    return mapped[0]['depotFile']


def verify_reference(path, reference, expected_sha256):
    """Verify the referenced server object, even after the working SPM changed."""
    match = re.fullmatch(r'p4://([^/]+)(//.+(?:#\d+|@=\d+))', reference)
    if not match or not re.fullmatch(r'[0-9a-f]{64}', expected_sha256):
        raise RuntimeError('Invalid Perforce recovery reference or SHA256')
    client, spec = match.groups()
    depot = _mapping(Path(path), client)
    if not re.fullmatch(re.escape(depot) + r'(?:#\d+|@=\d+)', spec):
        raise RuntimeError('Perforce recovery belongs to a different source')
    payload = _p4(client, 'print', '-q', spec)
    if hashlib.sha256(payload).hexdigest() != expected_sha256:
        raise RuntimeError('Perforce recovery bytes do not match the original SPM')
    return reference


def verified_recovery(path):
    path = Path(path).resolve()
    original = path.read_bytes()
    client = os.environ.get('SPEEDTREE_PERFORCE_CLIENT', 'ArtSources')
    depot = _mapping(path, client)
    shelf = os.environ.get('SPEEDTREE_PERFORCE_SHELF', '')
    if shelf:
        if not shelf.isdecimal() or int(shelf) <= 0:
            raise RuntimeError('SPEEDTREE_PERFORCE_SHELF must be a positive changelist number')
        spec = depot + '@=' + shelf
    else:
        rows = _records(_p4(client, '-G', 'fstat', '-T', 'depotFile,haveRev,headType', str(path)))
        if (len(rows) != 1 or not str(rows[0].get('haveRev', '')).isdecimal()
                or int(rows[0]['haveRev']) <= 0):
            raise RuntimeError('No submitted have revision; provide a verified uploaded shelf')
        spec = depot + '#' + str(rows[0]['haveRev'])
    reference = 'p4://' + client + spec
    verify_reference(path, reference, hashlib.sha256(original).hexdigest())
    if path.read_bytes() != original:
        raise RuntimeError('SPM changed during Perforce recovery verification')
    return reference
