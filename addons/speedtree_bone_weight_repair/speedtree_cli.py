"""Safe, cached SpeedTree command-line exports.

SpeedTree is a GUI executable even when it is used with ``-export``.  In
particular, descendants can keep inherited stdout/stderr handles alive after
the Modeler process exits.  Waiting on PIPE EOF therefore is not safe for a
headless Blender process.  This module redirects output to regular files and
waits only for the process handle.
"""

import ctypes
import hashlib
import importlib.util
import json
import os
import shutil
import signal
import subprocess
import tempfile
import uuid
import xml.etree.ElementTree as ET
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path


try:
    from .export_options_contract import require_texture_skip_writing
except ImportError:
    # Keep direct-file unit tests and maintenance scripts independent of bpy
    # package initialization.
    _EXPORT_OPTIONS_CONTRACT_PATH = (
        Path(__file__).with_name("export_options_contract.py")
    )
    _EXPORT_OPTIONS_SPEC = importlib.util.spec_from_file_location(
        "bwr_export_options_contract", _EXPORT_OPTIONS_CONTRACT_PATH
    )
    if _EXPORT_OPTIONS_SPEC is None or _EXPORT_OPTIONS_SPEC.loader is None:
        raise RuntimeError(
            "Could not load SpeedTree export-options contract: "
            + str(_EXPORT_OPTIONS_CONTRACT_PATH)
        )
    _EXPORT_OPTIONS_MODULE = importlib.util.module_from_spec(
        _EXPORT_OPTIONS_SPEC
    )
    _EXPORT_OPTIONS_SPEC.loader.exec_module(_EXPORT_OPTIONS_MODULE)
    require_texture_skip_writing = (
        _EXPORT_OPTIONS_MODULE.require_texture_skip_writing
    )


EXPORT_CACHE_VERSION = 1
_HASH_CHUNK_SIZE = 1024 * 1024
_WINDOWS_ACCESS_VIOLATION = 0xC0000005
_CRASH_RETRY_ATTEMPTS = 3
SPEEDTREE_EXPORT_MUTEX_ENV = "SPEEDTREE_EXPORT_MUTEX_NAME"
SPEEDTREE_EXPORT_MUTEX_DEFAULT = (
    r"Local\PARK.SpeedTree.Modeler.Export.v1.slot0"
)


@contextmanager
def speedtree_export_gate():
    """Share one machine-wide Modeler export slot with SK Batch."""
    name = os.environ.get(
        SPEEDTREE_EXPORT_MUTEX_ENV, SPEEDTREE_EXPORT_MUTEX_DEFAULT
    )
    if os.name == "nt":
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateMutexW.argtypes = (
            ctypes.c_void_p,
            ctypes.c_bool,
            ctypes.c_wchar_p,
        )
        kernel32.CreateMutexW.restype = ctypes.c_void_p
        kernel32.WaitForSingleObject.argtypes = (
            ctypes.c_void_p,
            ctypes.c_uint32,
        )
        kernel32.WaitForSingleObject.restype = ctypes.c_uint32
        kernel32.ReleaseMutex.argtypes = (ctypes.c_void_p,)
        kernel32.ReleaseMutex.restype = ctypes.c_bool
        kernel32.CloseHandle.argtypes = (ctypes.c_void_p,)
        kernel32.CloseHandle.restype = ctypes.c_bool
        handle = kernel32.CreateMutexW(None, False, name)
        if not handle:
            raise ctypes.WinError(ctypes.get_last_error())
        acquired = False
        try:
            result = kernel32.WaitForSingleObject(handle, 0xFFFFFFFF)
            if result not in {0x00000000, 0x00000080}:
                if result == 0xFFFFFFFF:
                    raise ctypes.WinError(ctypes.get_last_error())
                raise RuntimeError(
                    f"SpeedTree export mutex wait returned {result:#x}"
                )
            acquired = True
            yield
        finally:
            if acquired:
                kernel32.ReleaseMutex(handle)
            kernel32.CloseHandle(handle)
        return

    import fcntl

    safe_name = "".join(
        character if character.isalnum() or character in "_.-" else "_"
        for character in name
    )
    lock_path = Path(tempfile.gettempdir()) / f"{safe_name}.lock"
    with lock_path.open("a+b") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _utc_timestamp():
    return datetime.now(timezone.utc).isoformat()


def _windows_exit_code(returncode):
    """Return the unsigned Windows process status for signed/unsigned APIs."""
    return int(returncode) & 0xFFFFFFFF


def _is_retryable_exporter_crash(returncode):
    return _windows_exit_code(returncode) == _WINDOWS_ACCESS_VIOLATION


def _sha256_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        while True:
            chunk = handle.read(_HASH_CHUNK_SIZE)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _file_identity(path, include_hash=True):
    path = Path(path)
    stat = path.stat()
    identity = {
        "path": str(path.resolve()),
        "size": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
    }
    if include_hash:
        identity["sha256"] = _sha256_file(path)
    return identity


def _input_fingerprint(exe, spm, options, kind, target):
    # Hash authored inputs so a timestamp-preserving copy cannot accidentally
    # reuse stale geometry.  The executable is large; its path/stat identity is
    # sufficient to invalidate the cache on a SpeedTree install/update.
    payload = {
        "version": EXPORT_CACHE_VERSION,
        "kind": str(kind).lower(),
        "target": str(Path(target).resolve()),
        "spm": _file_identity(spm),
        "options": _file_identity(options),
        "speedtree_exe": _file_identity(exe, include_hash=False),
    }
    encoded = json.dumps(
        payload, sort_keys=True, ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest(), payload


def _cache_path(target):
    target = Path(target)
    return target.parent / ".speedtree_export_cache" / f"{target.name}.json"


def _load_cache(path):
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return None
    return data if isinstance(data, dict) else None


def _write_cache(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp-{uuid.uuid4().hex}")
    try:
        temporary.write_text(
            json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _basic_output_is_valid(kind, target, parse_xml=False):
    target = Path(target)
    try:
        if not target.is_file() or target.stat().st_size <= 0:
            return False
    except OSError:
        return False

    kind = str(kind).lower()
    xml_paths = []
    if kind == "xml":
        xml_paths.append(target)
    elif kind == "fbx":
        # The STMAT is required by the Blender material normalization pipeline,
        # so a lone FBX is not a reusable SpeedTree export.
        stmat = target.with_suffix(".stmat")
        try:
            if not stmat.is_file() or stmat.stat().st_size <= 0:
                return False
        except OSError:
            return False
        xml_paths.append(stmat)

    if parse_xml:
        for xml_path in xml_paths:
            try:
                ET.parse(xml_path)
            except (OSError, ET.ParseError):
                return False
    return True


def _artifact_record(path, root):
    path = Path(path)
    stat = path.stat()
    return {
        "relative_path": path.relative_to(root).as_posix(),
        "size": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
        "sha256": _sha256_file(path),
    }


def _artifact_matches(record, root):
    try:
        relative = Path(str(record["relative_path"]))
        if relative.is_absolute() or ".." in relative.parts:
            return False
        path = Path(root) / relative
        stat = path.stat()
        if not path.is_file() or stat.st_size != int(record["size"]):
            return False
        # The normal hit path is stat-only.  If OneDrive or a manual copy
        # touched the timestamp, verify content before declaring a miss.
        if stat.st_mtime_ns == int(record.get("mtime_ns", -1)):
            return True
        return _sha256_file(path) == str(record.get("sha256") or "")
    except (KeyError, OSError, TypeError, ValueError):
        return False


def _cache_hit(cache, fingerprint, kind, target):
    if not cache or cache.get("version") != EXPORT_CACHE_VERSION:
        return False
    if cache.get("input_fingerprint") != fingerprint:
        return False
    artifacts = cache.get("artifacts")
    if not isinstance(artifacts, list) or not artifacts:
        return False
    root = Path(target).parent
    if not all(_artifact_matches(record, root) for record in artifacts):
        return False
    return _basic_output_is_valid(kind, target, parse_xml=False)


def synchronize_result_mtime(result, minimum_mtime_ns):
    """Advance a verified cached output and its receipt as one export bundle.

    ``export_target`` has already proven the artifact hash and current input
    fingerprint. This is used when a sibling output (for example FBX) was
    regenerated later than a still-valid XML cache hit. Downstream consumers
    can then keep a strict XML-not-older-than-FBX contract without forcing an
    otherwise redundant SpeedTree export.
    """
    target = Path(str((result or {}).get("path") or ""))
    if not target.is_file():
        raise RuntimeError(f"Cannot synchronize a missing export artifact: {target}")
    stat = target.stat()
    minimum_mtime_ns = int(minimum_mtime_ns)
    if stat.st_mtime_ns >= minimum_mtime_ns:
        return {
            "changed": False,
            "path": str(target),
            "mtime_ns": int(stat.st_mtime_ns),
        }

    os.utime(target, ns=(stat.st_atime_ns, minimum_mtime_ns))
    cache_path = Path(
        str((result or {}).get("cache_path") or _cache_path(target))
    )
    cache = _load_cache(cache_path)
    if cache:
        records = cache.get("artifacts") or []
        for record in records:
            if record.get("relative_path") == target.name:
                record["mtime_ns"] = minimum_mtime_ns
        _write_cache(cache_path, cache)
        result["artifacts"] = records
    result["bundle_mtime_synchronized"] = True
    result["bundle_mtime_ns"] = minimum_mtime_ns
    return {
        "changed": True,
        "path": str(target),
        "mtime_ns": minimum_mtime_ns,
        "cache_path": str(cache_path),
    }


def _fresh_existing_artifacts(kind, target, inputs):
    """Return seed records for a trustworthy pre-cache export.

    Older add-on versions wrote deterministic FBX/XML paths without a cache
    receipt.  On the first run after this upgrade, adopt those outputs only if
    the required files are structurally valid and newer than the SPM, INI, and
    installed SpeedTree executable.  Once a receipt exists, a fingerprint
    mismatch always causes a real export instead of using this migration path.
    """
    target = Path(target)
    if not _basic_output_is_valid(kind, target, parse_xml=True):
        return None
    paths = [target]
    if str(kind).lower() == "fbx":
        paths.append(target.with_suffix(".stmat"))
    try:
        input_mtime = max(
            int(inputs[key]["mtime_ns"])
            for key in ("spm", "options", "speedtree_exe")
        )
        if any(path.stat().st_mtime_ns < input_mtime for path in paths):
            return None
        return [_artifact_record(path, target.parent) for path in paths]
    except (KeyError, OSError, TypeError, ValueError):
        return None


def _read_process_log(handle):
    handle.flush()
    handle.seek(0)
    return handle.read().decode("utf-8", errors="replace")


def _terminate_process_tree(process):
    """Terminate the launched process and all descendants after a timeout."""
    if os.name == "nt":
        try:
            subprocess.run(
                ["taskkill.exe", "/PID", str(process.pid), "/T", "/F"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=30,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            pass
    else:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except (OSError, ProcessLookupError):
            pass
        try:
            process.wait(timeout=5)
        except (OSError, subprocess.TimeoutExpired):
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except (OSError, ProcessLookupError):
                pass

    try:
        process.wait(timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        try:
            process.kill()
        except OSError:
            pass
        try:
            process.wait(timeout=5)
        except (OSError, subprocess.TimeoutExpired):
            pass


def _run_process(command, cwd, timeout_seconds):
    popen_kwargs = {
        "cwd": str(cwd),
        "stdin": subprocess.DEVNULL,
    }
    if os.name == "nt":
        popen_kwargs["creationflags"] = getattr(
            subprocess, "CREATE_NEW_PROCESS_GROUP", 0
        )
    else:
        popen_kwargs["start_new_session"] = True

    # Queue for the single Modeler slot before Popen/wait. Gate wait time is
    # intentionally outside the export timeout.
    with speedtree_export_gate():
        # Regular files are deliberate: Popen.wait() observes the process
        # handle, not EOF on a pipe inherited by a SpeedTree descendant.
        with tempfile.TemporaryFile(
            mode="w+b"
        ) as stdout_file, tempfile.TemporaryFile(mode="w+b") as stderr_file:
            process = subprocess.Popen(
                command,
                stdout=stdout_file,
                stderr=stderr_file,
                **popen_kwargs,
            )
            try:
                returncode = process.wait(timeout=timeout_seconds)
            except subprocess.TimeoutExpired:
                _terminate_process_tree(process)
                stdout = _read_process_log(stdout_file)
                stderr = _read_process_log(stderr_file)
                error = subprocess.TimeoutExpired(command, timeout_seconds)
                error.stdout = stdout
                error.stderr = stderr
                raise error
            stdout = _read_process_log(stdout_file)
            stderr = _read_process_log(stderr_file)
    return returncode, stdout, stderr


def _transactional_promote(staging_root, destination_root):
    """Promote staged files and restore every prior file if promotion fails."""
    staging_root = Path(staging_root)
    destination_root = Path(destination_root)
    sources = sorted(path for path in staging_root.rglob("*") if path.is_file())
    if not sources:
        raise RuntimeError("SpeedTree export staging directory contained no files.")

    destination_root.mkdir(parents=True, exist_ok=True)
    backup_root = Path(
        tempfile.mkdtemp(prefix=".bwr_speedtree_restore_", dir=str(destination_root))
    )
    promoted = []
    backed_up = []
    pending_copies = []
    try:
        for source in sources:
            relative = source.relative_to(staging_root)
            destination = destination_root / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            copy_path = destination.with_name(
                f".{destination.name}.bwr-new-{uuid.uuid4().hex}"
            )
            pending_copies.append(copy_path)
            shutil.copy2(source, copy_path)
            if destination.exists():
                backup = backup_root / relative
                backup.parent.mkdir(parents=True, exist_ok=True)
                os.replace(destination, backup)
                backed_up.append((destination, backup))
            os.replace(copy_path, destination)
            pending_copies.remove(copy_path)
            promoted.append(destination)
        return [path.relative_to(destination_root) for path in promoted]
    except Exception as exc:
        restore_errors = []
        for copy_path in pending_copies:
            try:
                copy_path.unlink()
            except FileNotFoundError:
                pass
            except OSError as restore_exc:
                restore_errors.append(str(restore_exc))
        for destination in reversed(promoted):
            try:
                destination.unlink()
            except FileNotFoundError:
                pass
            except OSError as restore_exc:
                restore_errors.append(str(restore_exc))
        for destination, backup in reversed(backed_up):
            try:
                destination.parent.mkdir(parents=True, exist_ok=True)
                os.replace(backup, destination)
            except OSError as restore_exc:
                restore_errors.append(str(restore_exc))
        suffix = ""
        if restore_errors:
            suffix = " Restore errors: " + "; ".join(restore_errors)
        raise RuntimeError(f"SpeedTree export promotion failed: {exc}.{suffix}") from exc
    finally:
        shutil.rmtree(backup_root, ignore_errors=True)


def export_target(exe, spm, options, kind, target, timeout_seconds=900):
    """Export one FBX/XML target with cache, staging, and timeout cleanup."""
    exe = Path(exe)
    spm = Path(spm)
    options = Path(options)
    target = Path(target)
    kind = str(kind).lower()
    require_texture_skip_writing(
        options, purpose=f"SpeedTree {kind.upper()} export"
    )
    target.parent.mkdir(parents=True, exist_ok=True)

    fingerprint, inputs = _input_fingerprint(exe, spm, options, kind, target)
    cache_path = _cache_path(target)
    started = _utc_timestamp()
    cache = _load_cache(cache_path)
    if _cache_hit(cache, fingerprint, kind, target):
        finished = _utc_timestamp()
        return {
            "path": str(target),
            "export_options": str(options),
            "exists": True,
            "size": target.stat().st_size,
            "returncode": 0,
            "started": started,
            "finished": finished,
            "stdout": "",
            "stderr": "",
            "cache_hit": True,
            "cache_seeded": False,
            "cache_path": str(cache_path),
            "input_fingerprint": fingerprint,
            "artifacts": cache.get("artifacts", []),
        }

    # Migration for valid outputs written by the former no-cache exporter.
    # Do not use it for a corrupt/stale/mismatched existing receipt: those
    # cases need a real export so the known provenance is restored.
    if not cache_path.exists():
        seed_artifacts = _fresh_existing_artifacts(kind, target, inputs)
        if seed_artifacts:
            finished = _utc_timestamp()
            seed_data = {
                "version": EXPORT_CACHE_VERSION,
                "kind": kind,
                "target": str(target.resolve()),
                "input_fingerprint": fingerprint,
                "inputs": inputs,
                "artifacts": seed_artifacts,
                "completed_at": finished,
                "seeded_from_fresh_existing_output": True,
            }
            _write_cache(cache_path, seed_data)
            return {
                "path": str(target),
                "export_options": str(options),
                "exists": True,
                "size": target.stat().st_size,
                "returncode": 0,
                "started": started,
                "finished": finished,
                "stdout": "Reused fresh valid output and seeded export cache.",
                "stderr": "",
                "cache_hit": True,
                "cache_seeded": True,
                "cache_path": str(cache_path),
                "input_fingerprint": fingerprint,
                "artifacts": seed_artifacts,
            }

    stdout = ""
    stderr = ""
    export_attempts = []
    promoted = []
    for attempt in range(1, _CRASH_RETRY_ATTEMPTS + 1):
        with tempfile.TemporaryDirectory(
            prefix=f"bwr_speedtree_{kind}_"
        ) as temp_dir:
            staging_root = Path(temp_dir)
            staged_target = staging_root / target.name
            command = [
                str(exe),
                str(spm),
                "-export_options",
                str(options),
                "-export",
                str(staged_target),
            ]
            try:
                returncode, stdout, stderr = _run_process(
                    command,
                    cwd=spm.parent,
                    timeout_seconds=timeout_seconds,
                )
            except subprocess.TimeoutExpired as exc:
                detail = (
                    str(exc.stderr or "") or str(exc.stdout or "")
                )[-1000:]
                suffix = f" Last output: {detail}" if detail else ""
                raise RuntimeError(
                    f"SpeedTree {kind.upper()} export timed out after "
                    f"{timeout_seconds} seconds; its process tree was "
                    f"terminated.{suffix}"
                ) from exc

            attempt_record = {
                "attempt": attempt,
                "returncode": returncode,
                "windows_exit_code": (
                    f"0x{_windows_exit_code(returncode):08X}"
                ),
            }
            export_attempts.append(attempt_record)
            if returncode != 0:
                retryable_crash = _is_retryable_exporter_crash(returncode)
                attempt_record["failure_kind"] = (
                    "process_exporter_crash"
                    if retryable_crash
                    else "process_export_failed"
                )
                if (
                    retryable_crash
                    and attempt < _CRASH_RETRY_ATTEMPTS
                ):
                    continue
                detail = (stderr or stdout)[-1000:]
                if retryable_crash:
                    raise RuntimeError(
                        f"SpeedTree {kind.upper()} export failed; "
                        "failure_kind=process_exporter_crash; "
                        f"attempts={attempt}; "
                        "windows_exit_code="
                        f"0x{_windows_exit_code(returncode):08X}: {detail}"
                    )
                raise RuntimeError(
                    f"SpeedTree {kind.upper()} export failed with code "
                    f"{returncode}: {detail}"
                )
            if not _basic_output_is_valid(
                kind, staged_target, parse_xml=True
            ):
                raise RuntimeError(
                    f"SpeedTree {kind.upper()} export finished but did not "
                    f"create a valid staged file: {staged_target}"
                )
            promoted = _transactional_promote(
                staging_root, target.parent
            )
            break

    if not _basic_output_is_valid(kind, target, parse_xml=True):
        raise RuntimeError(
            f"SpeedTree {kind.upper()} export promotion did not create a valid "
            f"file: {target}"
        )

    artifacts = [
        _artifact_record(target.parent / relative, target.parent)
        for relative in promoted
    ]
    finished = _utc_timestamp()
    cache_data = {
        "version": EXPORT_CACHE_VERSION,
        "kind": kind,
        "target": str(target.resolve()),
        "input_fingerprint": fingerprint,
        "inputs": inputs,
        "artifacts": artifacts,
        "completed_at": finished,
    }
    _write_cache(cache_path, cache_data)
    return {
        "path": str(target),
        "export_options": str(options),
        "exists": target.exists(),
        "size": target.stat().st_size if target.exists() else 0,
        "returncode": 0,
        "started": started,
        "finished": finished,
        "stdout": stdout[-4000:],
        "stderr": stderr[-4000:],
        "cache_hit": False,
        "cache_seeded": False,
        "cache_path": str(cache_path),
        "input_fingerprint": fingerprint,
        "artifacts": artifacts,
        "export_attempts": export_attempts,
    }
