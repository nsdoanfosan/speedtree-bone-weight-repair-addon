"""Safe, cached SpeedTree command-line exports.

SpeedTree is a GUI executable even when it is used with ``-export``.  In
particular, descendants can keep inherited stdout/stderr handles alive after
the Modeler process exits.  Waiting on PIPE EOF therefore is not safe for a
headless Blender process.  This module redirects output to regular files and
waits only for the process handle.
"""

import ctypes
import codecs
import gzip
import hashlib
import importlib.util
import json
import math
import os
import re
import shutil
import signal
import subprocess
import tempfile
import threading
import time
import uuid
import xml.etree.ElementTree as ET
import xml.parsers.expat as expat
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


EXPORT_CACHE_VERSION = 3
_HASH_CHUNK_SIZE = 1024 * 1024
_WINDOWS_ACCESS_VIOLATION = 0xC0000005
_WINDOWS_STACK_BUFFER_OVERRUN = 0xC0000409
_WINDOWS_HEAP_CORRUPTION = 0xC0000374
_PERSISTENT_SESSION_UNAVAILABLE = 12
_PROCESS_INITIALIZATION_FAILED = 13
_PRIVATE_DESKTOP_CREATION_FAILED = 14
_PROCESS_EXPORT_STALLED = 24
_EXPORT_RETRY_ATTEMPTS = 3
_EXPORT_RETRY_BACKOFF_SECONDS = (0.25, 0.75)
SPEEDTREE_EXPORT_MUTEX_ENV = "SPEEDTREE_EXPORT_MUTEX_NAME"
SPEEDTREE_EXPORT_MUTEX_DEFAULT = (
    r"Local\PARK.SpeedTree.Modeler.Export.v1.slot0"
)
_NATIVE_SYNTHETIC_BONE_ID_START = 10000
_NATIVE_TO_XML_COORDINATE_SCALE = 30.48
_NATIVE_TO_XML_COORDINATE_SCALE_TOLERANCE = 1.0e-9
_XML_SYNTHETIC_COORDINATE_TOLERANCE = 0.1
_SPM_MINIMUM_BONE_GENERATOR_TYPES = {
    "branch",
    "spline branch",
    "splinebranch",
}


def _root_direct_element_byte_spans(xml_bytes, element_name):
    """Return exact UTF-8 byte spans for one root-direct XML element type."""
    parser = expat.ParserCreate()
    depth = 0
    open_starts = []
    spans = []

    def opening_tag_end(start_index):
        quote = None
        for index in range(start_index, len(xml_bytes)):
            value = xml_bytes[index]
            if quote is not None:
                if value == quote:
                    quote = None
                continue
            if value in (ord('"'), ord("'")):
                quote = value
            elif value == ord(">"):
                return index + 1
        raise RuntimeError(
            f"SpeedTree XML has an incomplete {element_name} opening tag"
        )

    def start(name, _attributes):
        nonlocal depth
        if depth == 1 and name == element_name:
            section_start = parser.CurrentByteIndex
            section_end = opening_tag_end(section_start)
            if xml_bytes[section_start:section_end].rstrip().endswith(b"/>"):
                spans.append((section_start, section_end))
                open_starts.append(None)
            else:
                open_starts.append(section_start)
        depth += 1

    def end(name):
        nonlocal depth
        depth -= 1
        if depth == 1 and name == element_name:
            if not open_starts:
                raise RuntimeError(
                    f"SpeedTree XML closed {element_name} without a root-direct start"
                )
            section_start = open_starts.pop()
            if section_start is None:
                return
            closing_start = parser.CurrentByteIndex
            closing_end = xml_bytes.find(b">", closing_start)
            if closing_end < 0:
                raise RuntimeError(
                    f"SpeedTree XML has an incomplete {element_name} closing tag"
                )
            spans.append((section_start, closing_end + 1))

    parser.StartElementHandler = start
    parser.EndElementHandler = end
    parser.Parse(xml_bytes, True)
    if open_starts:
        raise RuntimeError(
            f"SpeedTree XML has an unclosed root-direct {element_name} section"
        )
    return spans


def _spm_property_value_element(generator, property_name):
    for element in generator.iter():
        name = element.find("Name")
        if name is None or str(name.text or "") != property_name:
            continue
        value = element.find("Value")
        if value is None:
            raise RuntimeError(
                f"SpeedTree property has no Value element: {property_name}"
            )
        return value
    return None


def _is_cluster_source_spm(spm):
    spm = Path(spm)
    return (
        spm.parent.name.casefold() == "cluster"
        or spm.stem.casefold().startswith("sk_cluster_")
    )


def ensure_minimum_absolute_branch_bones(spm):
    """Persist the non-Cluster Branch/Spline-Branch minimum-bone policy.

    Every parsed zero-bone Branch generator is authored as Absolute/1 before
    Modeler sees it.  This is an SPM data repair, not a post-export Blender
    approximation.  Cluster providers deliberately retain their historical
    single reference-axis policy and are never modified here.
    """
    spm = Path(spm).resolve()
    original = spm.read_bytes()
    original_sha256 = hashlib.sha256(original).hexdigest()
    if _is_cluster_source_spm(spm):
        return {
            "status": "excluded_cluster_source",
            "spm": str(spm),
            "changed": False,
            "changed_generator_count": 0,
            "changed_generators": [],
            "backup": "",
            "source_sha256": original_sha256,
            "policy": "cluster_reference_axis_policy_unchanged_v1",
        }

    compressed = original.startswith(b"\x1f\x8b")
    try:
        xml_bytes = gzip.decompress(original) if compressed else original
        text = xml_bytes.decode("utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise RuntimeError(f"SpeedTree SPM is not readable UTF-8 XML: {spm}") from exc

    try:
        section_spans = _root_direct_element_byte_spans(
            xml_bytes, "Generators"
        )
    except (expat.ExpatError, RuntimeError) as exc:
        raise RuntimeError(
            "SpeedTree SPM document is not valid writable XML: " + str(spm)
        ) from exc
    if len(section_spans) != 1:
        raise RuntimeError(
            "SpeedTree SPM requires exactly one root Generators section: "
            + str(spm)
        )
    section_start, section_end = section_spans[0]
    try:
        generators = ET.fromstring(xml_bytes[section_start:section_end])
    except ET.ParseError as exc:
        raise RuntimeError(
            "SpeedTree SPM root Generators section is not valid XML: "
            + str(spm)
        ) from exc

    changed_generators = []
    for generator in generators.findall("Generator"):
        generator_type = str(generator.attrib.get("Type") or "").strip()
        if generator_type.casefold() not in _SPM_MINIMUM_BONE_GENERATOR_TYPES:
            continue
        bones_value = _spm_property_value_element(
            generator, "Physics:Bones"
        )
        if bones_value is None:
            continue
        try:
            bones_before = float(str(bones_value.text or "").strip())
        except ValueError as exc:
            raise RuntimeError(
                "SpeedTree Branch generator has an invalid Physics:Bones value: "
                + str(generator.findtext("Name") or "?")
            ) from exc
        if not math.isfinite(bones_before):
            raise RuntimeError(
                "SpeedTree Branch generator has a non-finite Physics:Bones value: "
                + str(generator.findtext("Name") or "?")
            )
        if not (bones_before <= 0.0):
            continue
        style_value = _spm_property_value_element(
            generator, "Physics:Bone style"
        )
        if style_value is None:
            raise RuntimeError(
                "Zero-bone SpeedTree Branch generator has no Physics:Bone style: "
                + str(generator.findtext("Name") or "?")
            )
        style_before = str(style_value.text or "").strip()
        style_value.text = "0"
        bones_value.text = "1"
        changed_generators.append({
            "guid": str(generator.findtext("GUID") or ""),
            "name": str(generator.findtext("Name") or "?"),
            "type": generator_type,
            "hidden": str(generator.findtext("Hidden") or "").casefold()
            == "true",
            "before": {
                "bone_style": style_before,
                "bones": bones_before,
            },
            "after": {
                "bone_style": 0,
                "bones": 1,
            },
        })

    if not changed_generators:
        return {
            "status": "already_compliant",
            "spm": str(spm),
            "changed": False,
            "changed_generator_count": 0,
            "changed_generators": [],
            "backup": "",
            "source_sha256": original_sha256,
        }

    rendered_generators = ET.tostring(
        generators, encoding="utf-8", short_empty_elements=True
    )
    updated_xml = (
        xml_bytes[:section_start]
        + rendered_generators
        + xml_bytes[section_end:]
    )
    try:
        ET.fromstring(updated_xml)
    except ET.ParseError as exc:
        raise RuntimeError(
            "Minimum-bone SPM candidate failed full XML validation: " + str(spm)
        ) from exc
    updated = (
        gzip.compress(updated_xml, compresslevel=9, mtime=0)
        if compressed
        else updated_xml
    )

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    backup_root = (
        spm.parent
        / "_spm_backups"
        / f"minimum_absolute_branch_bones_{timestamp}_{original_sha256[:8]}"
    )
    backup_root.mkdir(parents=True, exist_ok=False)
    backup = backup_root / spm.name
    backup.write_bytes(original)
    if hashlib.sha256(backup.read_bytes()).hexdigest() != original_sha256:
        raise RuntimeError("Minimum-bone SPM backup hash verification failed")

    if spm.read_bytes() != original:
        raise RuntimeError(
            "SpeedTree SPM changed while minimum-bone repair was computed: "
            + str(spm)
        )
    temporary = spm.with_name(
        f".{spm.name}.minimum-bones-{uuid.uuid4().hex}"
    )
    try:
        temporary.write_bytes(updated)
        check_raw = temporary.read_bytes()
        check_xml = (
            gzip.decompress(check_raw) if compressed else check_raw
        ).decode("utf-8")
        ET.fromstring(check_xml)
        os.replace(temporary, spm)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass

    persisted = spm.read_bytes()
    if persisted != updated:
        raise RuntimeError("Minimum-bone SPM persisted bytes did not verify")
    return {
        "status": "updated",
        "spm": str(spm),
        "changed": True,
        "changed_generator_count": len(changed_generators),
        "changed_generators": changed_generators,
        "backup": str(backup),
        "source_sha256": original_sha256,
        "updated_sha256": hashlib.sha256(persisted).hexdigest(),
        "compressed": compressed,
        "policy": "non_cluster_zero_bone_branch_to_absolute_one_v1",
    }


@contextmanager
def _system_speedtree_export_gate():
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


_SPEEDTREE_EXPORT_GATE_LOCAL = threading.local()


@contextmanager
def speedtree_export_gate():
    """Acquire the machine gate once while allowing nested helper exports."""
    depth = int(getattr(_SPEEDTREE_EXPORT_GATE_LOCAL, "depth", 0) or 0)
    if depth > 0:
        _SPEEDTREE_EXPORT_GATE_LOCAL.depth = depth + 1
        try:
            yield
        finally:
            _SPEEDTREE_EXPORT_GATE_LOCAL.depth = depth
        return

    with _system_speedtree_export_gate():
        _SPEEDTREE_EXPORT_GATE_LOCAL.depth = 1
        try:
            yield
        finally:
            _SPEEDTREE_EXPORT_GATE_LOCAL.depth = 0


def _utc_timestamp():
    return datetime.now(timezone.utc).isoformat()


def _windows_exit_code(returncode):
    """Return the unsigned Windows process status for signed/unsigned APIs."""
    return int(returncode) & 0xFFFFFFFF


def _retryable_export_failure_kind(returncode):
    code = _windows_exit_code(returncode)
    if code in {
        _WINDOWS_ACCESS_VIOLATION,
        _WINDOWS_STACK_BUFFER_OVERRUN,
        _WINDOWS_HEAP_CORRUPTION,
    } or code >= 0xC0000000:
        return "process_exporter_crash"
    if code == _PERSISTENT_SESSION_UNAVAILABLE:
        return "persistent_session_unavailable"
    if code in {
        _PROCESS_INITIALIZATION_FAILED,
        _PRIVATE_DESKTOP_CREATION_FAILED,
    }:
        return "process_startup_failed"
    if code == _PROCESS_EXPORT_STALLED:
        return "process_export_stalled"
    return ""


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
    # sufficient to invalidate the cache on a SpeedTree install/update.  The
    # adjacent hook is smaller and owns the injected serializer behavior, so
    # include its content hash as part of the producer identity.
    hook = Path(exe).with_name("speedtree_collision_hook.dll")
    if not hook.is_file():
        raise RuntimeError(
            "SpeedTree collision hook is missing beside the launcher: "
            + str(hook)
        )
    payload = {
        "version": EXPORT_CACHE_VERSION,
        "kind": str(kind).lower(),
        "target": str(Path(target).resolve()),
        "spm": _file_identity(spm),
        "options": _file_identity(options),
        "speedtree_exe": _file_identity(exe, include_hash=False),
        "speedtree_hook": _file_identity(hook),
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


def _normalized_path(value):
    try:
        return os.path.normcase(str(Path(value).resolve())).casefold()
    except (OSError, ValueError, TypeError):
        return ""


def _same_content_identity(recorded, current, *, require_path):
    if not isinstance(recorded, dict) or not isinstance(current, dict):
        return False
    if require_path and _normalized_path(recorded.get("path")) != _normalized_path(
        current.get("path")
    ):
        return False
    try:
        return bool(
            int(recorded.get("size")) == int(current.get("size"))
            and str(recorded.get("sha256") or "").casefold()
            == str(current.get("sha256") or "").casefold()
            and current.get("sha256")
        )
    except (TypeError, ValueError):
        return False


def _same_executable_identity(recorded, current):
    if not isinstance(recorded, dict) or not isinstance(current, dict):
        return False
    try:
        same_hash = (
            str(recorded.get("sha256") or "").casefold()
            == str(current.get("sha256") or "").casefold()
            if current.get("sha256")
            else True
        )
        return bool(
            _normalized_path(recorded.get("path"))
            == _normalized_path(current.get("path"))
            and int(recorded.get("size")) == int(current.get("size"))
            and int(recorded.get("mtime_ns")) == int(current.get("mtime_ns"))
            and same_hash
        )
    except (TypeError, ValueError):
        return False


def _semantically_equivalent_inputs(cache, inputs, kind, target):
    """Accept identical presets copied into another checkout.

    The output target and authored SPM remain path-bound.  An FBX/XML option
    preset is behaviorally identified by its bytes, so checkout path and mtime
    differences must not make two tools repeatedly overwrite the same export
    cache.  This fallback also upgrades existing version-1 receipts without a
    fleet-wide cache purge.
    """
    recorded = cache.get("inputs") if isinstance(cache, dict) else None
    if not isinstance(recorded, dict) or not isinstance(inputs, dict):
        return False
    if str(cache.get("kind") or "").casefold() != str(kind).casefold():
        return False
    if _normalized_path(cache.get("target")) != _normalized_path(target):
        return False
    return bool(
        _same_content_identity(
            recorded.get("spm"), inputs.get("spm"), require_path=True
        )
        and _same_content_identity(
            recorded.get("options"), inputs.get("options"), require_path=False
        )
        and _same_executable_identity(
            recorded.get("speedtree_exe"), inputs.get("speedtree_exe")
        )
        and _same_executable_identity(
            recorded.get("speedtree_hook"), inputs.get("speedtree_hook")
        )
    )


def _cache_hit(cache, fingerprint, kind, target, inputs=None):
    if not cache or cache.get("version") != EXPORT_CACHE_VERSION:
        return False
    if (
        cache.get("input_fingerprint") != fingerprint
        and not _semantically_equivalent_inputs(cache, inputs, kind, target)
    ):
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
            for key in ("spm", "options", "speedtree_exe", "speedtree_hook")
        )
        if any(path.stat().st_mtime_ns < input_mtime for path in paths):
            return None
        return [_artifact_record(path, target.parent) for path in paths]
    except (KeyError, OSError, TypeError, ValueError):
        return None


def _preserve_existing_output(
    kind,
    target,
    cache_path,
    fingerprint,
    inputs,
    started,
    options,
    verification_only=False,
):
    target = Path(target)
    if not _basic_output_is_valid(kind, target, parse_xml=True):
        return None
    paths = [target]
    if str(kind).lower() == "fbx":
        paths.append(target.with_suffix(".stmat"))
    artifacts = [_artifact_record(path, target.parent) for path in paths]
    finished = _utc_timestamp()
    _write_cache(cache_path, {
        "version": EXPORT_CACHE_VERSION,
        "kind": str(kind).lower(),
        "target": str(target.resolve()),
        "input_fingerprint": fingerprint,
        "inputs": inputs,
        "artifacts": artifacts,
        "completed_at": finished,
        "preserved_existing_output": True,
    })
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
        "cache_hit": False,
        "cache_seeded": False,
        "cache_path": str(cache_path),
        "input_fingerprint": fingerprint,
        "artifacts": artifacts,
        "preserved_existing_output": True,
        "verification_only": bool(verification_only),
    }


def _native_receipt_is_valid(path, spm):
    """Validate exact Modeler-runtime evidence for the current source SPM."""
    path = Path(path)
    spm = Path(spm).resolve()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        source = payload.get("source") or {}
        source_path = Path(str(source.get("path") or "")).resolve()
        source_stat = spm.stat()
        windows_write_time = (
            source_stat.st_mtime_ns // 100 + 116444736000000000
        )
        geometries = list(payload.get("geometries") or [])
        return bool(
            payload.get("kind") == "speedtree_native_export_receipt"
            and payload.get("status") == "ready"
            and int(payload.get("schema_version") or 0) >= 2
            and str(source_path).casefold() == str(spm).casefold()
            and int(source.get("size") or -1) == source_stat.st_size
            and int(source.get("last_write_time_100ns") or -1)
            == windows_write_time
            and int(
                payload.get("geometry_count")
                if payload.get("geometry_count") is not None
                else -1
            ) == len(geometries)
            and all(int(row.get("vertex_count") or 0) >= 0 for row in geometries)
        )
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return False


def load_native_receipt(path, spm):
    """Return current Modeler-runtime evidence or reject the stale sidecar."""
    path = Path(path)
    if not _native_receipt_is_valid(path, spm):
        raise RuntimeError(
            "Native SpeedTree export receipt is missing, stale, or invalid: "
            + str(path)
        )
    return json.loads(path.read_text(encoding="utf-8"))


def _verified_xml_backup(xml_path):
    xml_path = Path(xml_path)
    source_hash = _sha256_file(xml_path)
    backup_root = xml_path.parent / ".speedtree_xml_reconcile_backups"
    backup_root.mkdir(parents=True, exist_ok=True)
    backup = backup_root / (
        f"{xml_path.stem}.before-native-reconcile-{source_hash[:16]}"
        f"{xml_path.suffix}"
    )
    if not backup.exists():
        temporary = backup.with_name(f".{backup.name}.tmp-{uuid.uuid4().hex}")
        try:
            shutil.copy2(xml_path, temporary)
            if _sha256_file(temporary) != source_hash:
                raise RuntimeError(
                    "SpeedTree XML reconciliation backup hash verification failed: "
                    + str(temporary)
                )
            os.replace(temporary, backup)
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass
    if _sha256_file(backup) != source_hash:
        raise RuntimeError(
            "Existing SpeedTree XML reconciliation backup has unexpected bytes: "
            + str(backup)
        )
    return backup


def _synthetic_generator(receipt, bone, xml_root, xml_bones):
    """Resolve one receipt-backed generator; never invent a group name."""
    bone_id = int(bone["id"])
    source_rtti = str(bone.get("source_rtti") or "")
    instances = list(receipt.get("generated_instances") or [])
    matching = []
    for row in instances:
        try:
            if (
                int(row.get("source_bone_id")) == bone_id
                and str(row.get("source_rtti") or "") == source_rtti
            ):
                matching.append(row)
        except (TypeError, ValueError):
            continue
    guids = {
        str(row.get("generator_guid") or "").strip()
        for row in matching
        if str(row.get("generator_guid") or "").strip()
    }
    if len(guids) != 1:
        raise RuntimeError(
            f"Synthetic native bone {bone_id} does not have one exact "
            "receipt generator GUID"
        )
    guid = next(iter(guids))

    names = set()
    for row in instances:
        if str(row.get("generator_guid") or "").strip() != guid:
            continue
        try:
            xml_bone = xml_bones.get(int(row.get("source_bone_id")) - 1)
        except (TypeError, ValueError):
            xml_bone = None
        if xml_bone is not None and xml_bone.attrib.get("Generator"):
            names.add(xml_bone.attrib["Generator"])
    proof = "receipt_generator_guid_to_existing_xml_bone"

    if not names:
        token = next(
            (
                value
                for marker, value in (
                    ("LeafMesh", "leaf"),
                    ("Grass", "grass"),
                    ("Spline", "branch"),
                    ("Branch", "branch"),
                    ("Frond", "frond"),
                )
                if marker in source_rtti
            ),
            "",
        )
        for material in xml_root.iter("Material"):
            try:
                data = json.loads(material.attrib.get("UserData") or "")
                name = str(data.get("generator") or "").strip()
            except (AttributeError, TypeError, ValueError):
                continue
            if token and token in name.casefold():
                names.add(name)
        proof = "receipt_generator_guid_and_unique_xml_material_intent"
    if len(names) != 1:
        raise RuntimeError(
            f"Synthetic native bone {bone_id} has no unique evidenced source "
            "generator; refusing to invent a simulation group"
        )
    return next(iter(names)), {"proof": proof, "generator_guid": guid}


def _parse_speedtree_xml_float(value):
    """Parse Modeler XML numbers independently of the Windows UI locale."""
    text = str(value).strip()
    if "," in text:
        if "." in text or text.count(",") != 1:
            raise ValueError(f"ambiguous SpeedTree XML number: {text}")
        text = text.replace(",", ".")
    number = float(text)
    if not math.isfinite(number):
        raise ValueError(f"non-finite SpeedTree XML number: {text}")
    return number


def _validate_synthetic_xml_bone(element, bone, scale):
    bone_id = int(bone["id"])
    try:
        if int(element.attrib.get("ParentID", "-1")) != int(bone["parent_id"]) - 1:
            raise ValueError("parent mismatch")
        for prefix, key in (("Start", "start_native"), ("End", "end_native")):
            actual = [
                _parse_speedtree_xml_float(
                    element.attrib[f"{prefix}{axis}"]
                )
                for axis in "XYZ"
            ]
            expected = [float(value) * scale for value in bone[key]]
            if max(abs(a - b) for a, b in zip(actual, expected)) > (
                _XML_SYNTHETIC_COORDINATE_TOLERANCE
            ):
                raise ValueError(f"{prefix} coordinate mismatch")
    except (KeyError, TypeError, ValueError) as exc:
        raise RuntimeError(
            f"Existing synthetic XML Bone {bone_id - 1} conflicts with its "
            "native receipt"
        ) from exc


def reconcile_xml_with_native_receipt(
    xml_path, native_receipt, spm, *, create_backup=True
):
    """Supplement only receipt-proven reserved bones in a SpeedTree XML.

    Existing Bone rows are byte-preserved. Ordinary missing IDs, conflicting
    reserved IDs, an unproven generator, or a coordinate-contract mismatch all
    fail closed before the XML is written.
    """
    xml_path = Path(xml_path)
    native_receipt = Path(native_receipt)
    if not _native_receipt_is_valid(native_receipt, spm):
        raise RuntimeError(
            "Cannot reconcile SpeedTree XML from a stale native receipt: "
            + str(native_receipt)
        )
    receipt = json.loads(native_receipt.read_text(encoding="utf-8"))
    receipt_rows = list(receipt.get("bones") or [])
    if not receipt_rows:
        return {
            "status": "not_required",
            "changed": False,
            "added_xml_ids": [],
            "backup": "",
        }

    original = xml_path.read_bytes()
    has_utf8_bom = original.startswith(codecs.BOM_UTF8)
    try:
        text = original.decode("utf-8-sig")
        xml_root = ET.fromstring(text)
    except (UnicodeDecodeError, ET.ParseError) as exc:
        raise RuntimeError(
            "SpeedTree XML is not valid UTF-8 XML: " + str(xml_path)
        ) from exc
    sections = list(xml_root.iter("Bones"))
    if len(sections) != 1:
        raise RuntimeError("SpeedTree XML requires exactly one Bones section")
    section = sections[0]
    xml_by_id = {}
    try:
        for element in section.findall("Bone"):
            bone_id = int(element.attrib["ID"])
            if bone_id in xml_by_id:
                raise RuntimeError(f"Duplicate SpeedTree XML Bone ID: {bone_id}")
            xml_by_id[bone_id] = element
        if int(section.attrib["Count"]) != len(xml_by_id):
            raise RuntimeError("SpeedTree XML Bones Count does not match its rows")
    except (KeyError, TypeError, ValueError) as exc:
        raise RuntimeError("SpeedTree XML contains invalid Bone metadata") from exc

    receipt_by_id = {}
    try:
        for source in receipt_rows:
            bone_id = int(source["id"])
            bone = dict(source)
            bone["id"] = bone_id
            bone["parent_id"] = int(source["parent_id"])
            bone["start_native"] = tuple(float(v) for v in source["start_native"])
            bone["end_native"] = tuple(float(v) for v in source["end_native"])
            if (
                bone_id <= 0
                or bone_id in receipt_by_id
                or bone["parent_id"] < 0
                or len(bone["start_native"]) != 3
                or len(bone["end_native"]) != 3
            ):
                raise ValueError("invalid receipt bone")
            receipt_by_id[bone_id] = bone
        scale = float(receipt["coordinate_contract"]["native_unit_to_solver"])
    except (KeyError, TypeError, ValueError) as exc:
        raise RuntimeError("Native receipt contains invalid Bone metadata") from exc
    if abs(scale - _NATIVE_TO_XML_COORDINATE_SCALE) > (
        _NATIVE_TO_XML_COORDINATE_SCALE_TOLERANCE
    ):
        raise RuntimeError(
            f"Native receipt/XML coordinate scale is not exactly 30.48: {scale}"
        )

    missing_native_ids = [
        bone_id
        for bone_id in sorted(receipt_by_id)
        if bone_id - 1 not in xml_by_id
    ]
    ordinary_missing = [
        bone_id
        for bone_id in missing_native_ids
        if bone_id < _NATIVE_SYNTHETIC_BONE_ID_START
    ]
    if ordinary_missing:
        raise RuntimeError(
            "SpeedTree XML is missing ordinary native receipt bones; only "
            "reserved synthetic IDs may be reconciled: "
            + ", ".join(str(value) for value in ordinary_missing)
        )

    for bone_id, receipt_bone in receipt_by_id.items():
        if bone_id < _NATIVE_SYNTHETIC_BONE_ID_START:
            continue
        existing = xml_by_id.get(bone_id - 1)
        if existing is not None:
            _validate_synthetic_xml_bone(existing, receipt_bone, scale)

    if not missing_native_ids:
        return {
            "status": "already_reconciled",
            "changed": False,
            "added_xml_ids": [],
            "backup": "",
        }

    missing_set = set(missing_native_ids)
    additions = []
    provenance = []
    for bone_id in missing_native_ids:
        receipt_bone = receipt_by_id[bone_id]
        source_rtti = str(receipt_bone.get("source_rtti") or "").strip()
        if not source_rtti or not source_rtti.endswith("Node@@"):
            raise RuntimeError(
                f"Reserved native bone {bone_id} lacks a proven source node RTTI"
            )
        parent_id = int(receipt_bone["parent_id"])
        if parent_id > 0 and (
            parent_id - 1 not in xml_by_id and parent_id not in missing_set
        ):
            raise RuntimeError(
                f"Reserved native bone {bone_id} references missing parent "
                f"bone {parent_id}"
            )
        generator, proof = _synthetic_generator(
            receipt, receipt_bone, xml_root, xml_by_id
        )
        number = lambda value: format(
            0.0 if abs(float(value)) < 0.5e-9 else float(value), ".10g"
        )
        start = [value * scale for value in receipt_bone["start_native"]]
        end = [value * scale for value in receipt_bone["end_native"]]
        element = ET.Element("Bone", {
            "ID": str(bone_id - 1),
            "ParentID": str(parent_id - 1),
            "Radius": "0",
            **{f"Start{axis}": number(value) for axis, value in zip("XYZ", start)},
            **{f"End{axis}": number(value) for axis, value in zip("XYZ", end)},
            "Mass": "0",
            "Generator": generator,
        })
        additions.append(ET.tostring(element, encoding="unicode"))
        provenance.append({
            "native_bone_id": bone_id,
            "xml_bone_id": bone_id - 1,
            "source_rtti": source_rtti,
            "generator": generator,
            **proof,
        })

    opening_matches = list(re.finditer(
        r'<Bones\b(?P<before>[^>]*\bCount=")(?P<count>\d+)(?P<after>"[^>]*)>',
        text,
    ))
    closing_matches = list(re.finditer(r'(?m)^(?P<indent>[ \t]*)</Bones>', text))
    if len(opening_matches) != 1 or len(closing_matches) != 1:
        raise RuntimeError(
            "SpeedTree XML Bones text layout is not uniquely writable"
        )
    opening = opening_matches[0]
    closing = closing_matches[0]
    if closing.start() <= opening.end():
        raise RuntimeError("SpeedTree XML Bones section has invalid ordering")
    declared_count = int(opening.group("count"))
    updated_opening = (
        opening.group(0)[: opening.start("count") - opening.start()]
        + str(declared_count + len(additions))
        + opening.group(0)[opening.end("count") - opening.start():]
    )
    text = text[:opening.start()] + updated_opening + text[opening.end():]

    count_delta = len(updated_opening) - len(opening.group(0))
    closing_start = closing.start() + count_delta
    newline = "\r\n" if "\r\n" in text else "\n"
    child_indent = closing.group("indent") + "\t"
    insertion = "".join(
        child_indent + line + newline for line in additions
    )
    text = text[:closing_start] + insertion + text[closing_start:]
    payload = (codecs.BOM_UTF8 if has_utf8_bom else b"") + text.encode("utf-8")
    backup = _verified_xml_backup(xml_path) if create_backup else None
    temporary = xml_path.with_name(
        f".{xml_path.name}.bwr-reconcile-{uuid.uuid4().hex}"
    )
    try:
        temporary.write_bytes(payload)
        ET.parse(temporary)
        os.replace(temporary, xml_path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass

    check_root = ET.parse(xml_path).getroot()
    check_section = next(check_root.iter("Bones"))
    check_by_id = {
        int(element.attrib["ID"]): element
        for element in check_section.findall("Bone")
    }
    if int(check_section.attrib["Count"]) != len(check_by_id):
        raise RuntimeError("Reconciled SpeedTree XML Bones Count is invalid")
    for bone_id in missing_native_ids:
        added = check_by_id.get(bone_id - 1)
        if added is None:
            raise RuntimeError(
                f"Reconciled SpeedTree XML is missing Bone ID {bone_id - 1}"
            )
        _validate_synthetic_xml_bone(added, receipt_by_id[bone_id], scale)
    return {
        "status": "reconciled_reserved_synthetic_bones",
        "changed": True,
        "added_xml_ids": [value - 1 for value in missing_native_ids],
        "backup": str(backup) if backup is not None else "",
        "coordinate_scale": scale,
        "provenance": provenance,
    }


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


def export_target(
    exe,
    spm,
    options,
    kind,
    target,
    timeout_seconds=900,
    verification_only=False,
    native_receipt=None,
    force_reexport=False,
):
    """Export one FBX/XML target with cache, staging, and timeout cleanup."""
    exe = Path(exe)
    spm = Path(spm)
    options = Path(options)
    target = Path(target)
    native_receipt = Path(native_receipt) if native_receipt else None
    if native_receipt is not None:
        native_receipt.parent.mkdir(parents=True, exist_ok=True)
        if native_receipt.parent.resolve() != target.parent.resolve():
            raise ValueError(
                "Native receipt and FBX target must share one transaction directory."
            )
    kind = str(kind).lower()
    require_texture_skip_writing(
        options, purpose=f"SpeedTree {kind.upper()} export"
    )
    target.parent.mkdir(parents=True, exist_ok=True)

    fingerprint, inputs = _input_fingerprint(exe, spm, options, kind, target)
    cache_path = _cache_path(target)
    started = _utc_timestamp()
    cache = _load_cache(cache_path)
    receipt_ready = bool(
        native_receipt is None
        or (
            kind == "fbx"
            and _native_receipt_is_valid(native_receipt, spm)
        )
    )
    if (
        not force_reexport
        and _cache_hit(cache, fingerprint, kind, target, inputs)
        and receipt_ready
    ):
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
            "force_reexport_requested": False,
            "cache_path": str(cache_path),
            "input_fingerprint": fingerprint,
            "artifacts": cache.get("artifacts", []),
            "native_receipt": str(native_receipt) if native_receipt else "",
        }

    # Migration for valid outputs written by the former no-cache exporter.
    # Do not use it for a corrupt/stale/mismatched existing receipt: those
    # cases need a real export so the known provenance is restored.
    if (
        not force_reexport
        and not cache_path.exists()
        and native_receipt is None
    ):
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
                "force_reexport_requested": False,
                "cache_path": str(cache_path),
                "input_fingerprint": fingerprint,
                "artifacts": seed_artifacts,
            }

    stdout = ""
    stderr = ""
    export_attempts = []
    promoted = []
    for attempt in range(1, _EXPORT_RETRY_ATTEMPTS + 1):
        with tempfile.TemporaryDirectory(
            prefix=f"bwr_speedtree_{kind}_"
        ) as temp_dir:
            staging_root = Path(temp_dir)
            staged_target = staging_root / target.name
            command = [str(exe)]
            if verification_only:
                command.append("--verification-only")
            staged_receipt = None
            if native_receipt is not None:
                staged_receipt = staging_root / native_receipt.name
                command.extend(["--native-receipt", str(staged_receipt)])
            command.extend([
                str(spm),
                "-export_options",
                str(options),
                "-export",
                str(staged_target),
            ])
            try:
                returncode, stdout, stderr = _run_process(
                    command,
                    cwd=spm.parent,
                    timeout_seconds=timeout_seconds,
                )
            except subprocess.TimeoutExpired as exc:
                if not verification_only:
                    result = export_target(
                        exe=exe,
                        spm=spm,
                        options=options,
                        kind=kind,
                        target=target,
                        timeout_seconds=timeout_seconds,
                        verification_only=True,
                        native_receipt=native_receipt,
                        force_reexport=force_reexport,
                    )
                    result["collision_fallback"] = True
                    return result
                preserved = None
                if not force_reexport:
                    preserved = _preserve_existing_output(
                        kind,
                        target,
                        cache_path,
                        fingerprint,
                        inputs,
                        started,
                        options,
                        verification_only=True,
                    )
                if preserved is not None and native_receipt is None:
                    preserved["collision_fallback"] = True
                    return preserved
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
                failure_kind = _retryable_export_failure_kind(returncode)
                attempt_record["failure_kind"] = (
                    failure_kind or "process_export_failed"
                )
                if failure_kind and attempt < _EXPORT_RETRY_ATTEMPTS:
                    backoff = _EXPORT_RETRY_BACKOFF_SECONDS[attempt - 1]
                    attempt_record["retry_backoff_seconds"] = backoff
                    time.sleep(backoff)
                    continue
                if not verification_only:
                    result = export_target(
                        exe=exe,
                        spm=spm,
                        options=options,
                        kind=kind,
                        target=target,
                        timeout_seconds=timeout_seconds,
                        verification_only=True,
                        native_receipt=native_receipt,
                        force_reexport=force_reexport,
                    )
                    result["collision_fallback"] = True
                    return result
                preserved = None
                if not force_reexport:
                    preserved = _preserve_existing_output(
                        kind,
                        target,
                        cache_path,
                        fingerprint,
                        inputs,
                        started,
                        options,
                        verification_only=True,
                    )
                if preserved is not None and native_receipt is None:
                    preserved["collision_fallback"] = True
                    return preserved
                detail = (stderr or stdout)[-1000:]
                if failure_kind:
                    raise RuntimeError(
                        f"SpeedTree {kind.upper()} export failed; "
                        f"failure_kind={failure_kind}; "
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
                if not verification_only:
                    result = export_target(
                        exe=exe,
                        spm=spm,
                        options=options,
                        kind=kind,
                        target=target,
                        timeout_seconds=timeout_seconds,
                        verification_only=True,
                        native_receipt=native_receipt,
                        force_reexport=force_reexport,
                    )
                    result["collision_fallback"] = True
                    return result
                preserved = None
                if not force_reexport:
                    preserved = _preserve_existing_output(
                        kind,
                        target,
                        cache_path,
                        fingerprint,
                        inputs,
                        started,
                        options,
                        verification_only=True,
                    )
                if preserved is not None and native_receipt is None:
                    preserved["collision_fallback"] = True
                    return preserved
                raise RuntimeError(
                    f"SpeedTree {kind.upper()} export finished but did not "
                    f"create a valid staged file: {staged_target}"
                )
            if (
                staged_receipt is not None
                and not _native_receipt_is_valid(staged_receipt, spm)
            ):
                raise RuntimeError(
                    "SpeedTree FBX export finished without a valid native receipt: "
                    + str(staged_receipt)
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
        "verification_only": bool(verification_only),
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
        "verification_only": bool(verification_only),
        "force_reexport_requested": bool(force_reexport),
        "native_receipt": str(native_receipt) if native_receipt else "",
    }


def _export_bundle_fallback(
    exe,
    spm,
    prepared,
    results,
    timeout_seconds,
    native_receipt=None,
    force_reexport=False,
):
    for item in prepared:
        row = None
        if (
            not force_reexport
            and not (native_receipt is not None and item["kind"] == "fbx")
        ):
            row = _preserve_existing_output(
                item["kind"],
                item["target"],
                item["cache_path"],
                item["fingerprint"],
                item["inputs"],
                item["started"],
                item["options"],
            )
        if row is None:
            row = export_target(
                exe=exe,
                spm=spm,
                options=item["options"],
                kind=item["kind"],
                target=item["target"],
                timeout_seconds=timeout_seconds,
                verification_only=True,
                native_receipt=(
                    native_receipt if item["kind"] == "fbx" else None
                ),
                force_reexport=force_reexport,
            )
        row["bundled_process"] = False
        row["bundle_fallback"] = True
        results[item["kind"]] = row
    _reconcile_completed_bundle_results(
        results, native_receipt, spm, create_backup=True
    )
    return results


def _refresh_xml_result_cache(result, reconciliation):
    target = Path(str((result or {}).get("path") or ""))
    if not target.is_file():
        raise RuntimeError(
            "Cannot refresh reconciliation cache for missing XML: "
            + str(target)
        )
    artifacts = [_artifact_record(target, target.parent)]
    cache_path = Path(
        str((result or {}).get("cache_path") or _cache_path(target))
    )
    cache = _load_cache(cache_path)
    if not cache:
        raise RuntimeError(
            "Cannot refresh missing SpeedTree XML export cache: "
            + str(cache_path)
        )
    cache["artifacts"] = artifacts
    cache["native_receipt_xml_reconciliation"] = reconciliation
    _write_cache(cache_path, cache)
    result["artifacts"] = artifacts
    result["native_receipt_xml_reconciliation"] = reconciliation


def _reconcile_completed_bundle_results(
    results, native_receipt, spm, *, create_backup
):
    if native_receipt is None or "xml" not in results:
        return None
    reconciliation = reconcile_xml_with_native_receipt(
        results["xml"]["path"],
        native_receipt,
        spm,
        create_backup=create_backup,
    )
    _refresh_xml_result_cache(results["xml"], reconciliation)
    if "fbx" in results:
        results["fbx"]["native_receipt_xml_reconciliation"] = reconciliation
    return reconciliation


def _validated_minimum_bone_policy_receipt(spm, receipt):
    """Validate the persistent policy result against the bytes on disk."""
    spm = Path(spm).resolve()
    if not isinstance(receipt, dict):
        raise RuntimeError("Minimum branch-bone policy returned no receipt")
    status = str(receipt.get("status") or "")
    allowed = {
        "updated",
        "already_compliant",
        "excluded_cluster_source",
    }
    if status not in allowed:
        raise RuntimeError(
            "Minimum branch-bone policy returned an unsupported status: "
            + repr(status)
        )
    try:
        receipt_spm = Path(str(receipt.get("spm") or "")).resolve()
    except (OSError, RuntimeError, ValueError) as exc:
        raise RuntimeError(
            "Minimum branch-bone policy receipt has no valid SPM path"
        ) from exc
    if receipt_spm != spm:
        raise RuntimeError(
            "Minimum branch-bone policy receipt belongs to another SPM"
        )

    changed = receipt.get("changed")
    changed_count = receipt.get("changed_generator_count")
    if not isinstance(changed, bool) or not isinstance(changed_count, int):
        raise RuntimeError(
            "Minimum branch-bone policy receipt has malformed change fields"
        )
    if status == "updated":
        if not changed or changed_count <= 0:
            raise RuntimeError(
                "Updated minimum branch-bone policy receipt is inconsistent"
            )
    elif changed or changed_count != 0:
        raise RuntimeError(
            "Non-updated minimum branch-bone policy receipt is inconsistent"
        )

    current = _file_identity(spm)
    expected_digest_field = (
        "updated_sha256" if status == "updated" else "source_sha256"
    )
    expected_digest = str(receipt.get(expected_digest_field) or "").lower()
    if not re.fullmatch(r"[0-9a-f]{64}", expected_digest):
        raise RuntimeError(
            "Minimum branch-bone policy receipt has no valid "
            + expected_digest_field
        )
    if expected_digest != str(current.get("sha256") or "").lower():
        raise RuntimeError(
            "Minimum branch-bone policy receipt does not match persisted SPM"
        )

    if status == "updated":
        backup = Path(str(receipt.get("backup") or "")).resolve()
        source_digest = str(receipt.get("source_sha256") or "").lower()
        if not backup.is_file() or not re.fullmatch(
            r"[0-9a-f]{64}", source_digest
        ):
            raise RuntimeError(
                "Updated minimum branch-bone policy receipt has no verified backup"
            )
        if _sha256_file(backup).lower() != source_digest:
            raise RuntimeError(
                "Minimum branch-bone policy backup digest does not match receipt"
            )

    validated = dict(receipt)
    validated["sealed_source_identity"] = current
    return validated


def apply_minimum_absolute_branch_bone_policy(spm):
    """Apply and validate the SPM policy under the shared Modeler mutex."""
    with speedtree_export_gate():
        return _validated_minimum_bone_policy_receipt(
            spm,
            ensure_minimum_absolute_branch_bones(spm),
        )


def _validated_policy_export_request(exe, spm, targets, native_receipt):
    """Validate every immutable export input before persistent SPM repair."""
    exe = Path(exe).resolve()
    spm = Path(spm).resolve()
    rows = tuple(
        (str(kind).lower(), Path(target).resolve(), Path(options).resolve())
        for kind, target, options in targets
    )
    if not exe.is_file():
        raise RuntimeError("SpeedTree executable does not exist: " + str(exe))
    if not spm.is_file() or spm.suffix.casefold() != ".spm":
        raise RuntimeError("SpeedTree SPM does not exist: " + str(spm))
    hook = exe.with_name("speedtree_collision_hook.dll")
    if not hook.is_file():
        raise RuntimeError(
            "SpeedTree collision hook is missing beside the launcher: "
            + str(hook)
        )
    if not rows or len(rows) > 2:
        raise RuntimeError(
            "Minimum-bone export transaction requires one or two targets"
        )
    kinds = [kind for kind, _target, _options in rows]
    if len(set(kinds)) != len(kinds) or any(
        kind not in {"fbx", "xml"} for kind in kinds
    ):
        raise RuntimeError(
            "Minimum-bone export transaction target kinds are invalid"
        )
    for kind, target, options in rows:
        require_texture_skip_writing(
            options,
            purpose=f"SpeedTree {kind.upper()} export",
        )
        target.parent.mkdir(parents=True, exist_ok=True)

    receipt = Path(native_receipt).resolve() if native_receipt else None
    if receipt is not None:
        fbx_rows = [row for row in rows if row[0] == "fbx"]
        if len(fbx_rows) != 1:
            raise RuntimeError(
                "Native receipt export requires exactly one FBX target."
            )
        if receipt.parent != fbx_rows[0][1].parent:
            raise ValueError(
                "Native receipt and FBX target must share one transaction directory."
            )
    return exe, spm, rows, receipt


def export_bundle_with_minimum_bone_policy(
    exe,
    spm,
    targets,
    timeout_seconds=900,
    native_receipt=None,
    force_reexport=False,
    policy_report=None,
):
    """Persist Absolute/1 and export one sealed SPM under one mutex."""
    exe, spm, targets, native_receipt = _validated_policy_export_request(
        exe,
        spm,
        targets,
        native_receipt,
    )
    with speedtree_export_gate():
        policy = _validated_minimum_bone_policy_receipt(
            spm,
            ensure_minimum_absolute_branch_bones(spm),
        )
        sealed = dict(policy["sealed_source_identity"])
        try:
            results = export_bundle(
                exe=exe,
                spm=spm,
                targets=targets,
                timeout_seconds=timeout_seconds,
                native_receipt=native_receipt,
                force_reexport=force_reexport,
            )
        finally:
            current = _file_identity(spm)
            if current != sealed:
                raise RuntimeError(
                    "SpeedTree SPM changed during the sealed export transaction"
                )
    if policy_report is not None:
        policy_report["spm_bone_policy"] = policy
    return results


def export_bundle(
    exe,
    spm,
    targets,
    timeout_seconds=900,
    native_receipt=None,
    force_reexport=False,
):
    """Export FBX and XML through one collision-CLI/Modeler process.

    Each target keeps its independent content cache. A one-target miss still
    uses ``export_target``; only two simultaneous misses pay for the bundled
    native-CLI invocation.
    """
    exe = Path(exe)
    spm = Path(spm)
    native_receipt = Path(native_receipt) if native_receipt else None
    prepared = []
    all_items = []
    results = {}
    for kind, target, options in targets:
        kind = str(kind).lower()
        target = Path(target)
        options = Path(options)
        require_texture_skip_writing(
            options, purpose=f"SpeedTree {kind.upper()} export"
        )
        target.parent.mkdir(parents=True, exist_ok=True)
        fingerprint, inputs = _input_fingerprint(
            exe, spm, options, kind, target
        )
        cache_path = _cache_path(target)
        started = _utc_timestamp()
        cache = _load_cache(cache_path)
        item = {
            "kind": kind,
            "target": target,
            "options": options,
            "fingerprint": fingerprint,
            "inputs": inputs,
            "cache_path": cache_path,
            "started": started,
        }
        all_items.append(item)
        if (
            not force_reexport
            and _cache_hit(cache, fingerprint, kind, target, inputs)
        ):
            results[kind] = {
                "path": str(target),
                "export_options": str(options),
                "exists": True,
                "size": target.stat().st_size,
                "returncode": 0,
                "started": started,
                "finished": _utc_timestamp(),
                "stdout": "",
                "stderr": "",
                "cache_hit": True,
                "cache_seeded": False,
                "force_reexport_requested": False,
                "cache_path": str(cache_path),
                "input_fingerprint": fingerprint,
                "artifacts": cache.get("artifacts", []),
                "bundled_process": False,
                "native_receipt": (
                    str(native_receipt)
                    if native_receipt is not None and kind == "fbx"
                    else ""
                ),
            }
            continue
        prepared.append(item)

    if native_receipt is not None:
        fbx_items = [item for item in all_items if item["kind"] == "fbx"]
        if len(fbx_items) != 1:
            raise RuntimeError(
                "Native receipt export requires exactly one FBX target."
            )
        if native_receipt.parent.resolve() != fbx_items[0]["target"].parent.resolve():
            raise ValueError(
                "Native receipt and FBX target must share one transaction directory."
            )
        receipt_ready = _native_receipt_is_valid(native_receipt, spm)
        fbx_will_export = any(item["kind"] == "fbx" for item in prepared)
        if not receipt_ready or fbx_will_export:
            prepared = list(all_items)
            results.clear()

    if not prepared:
        return results
    if len(prepared) == 1:
        item = prepared[0]
        results[item["kind"]] = export_target(
            exe=exe,
            spm=spm,
            options=item["options"],
            kind=item["kind"],
            target=item["target"],
            timeout_seconds=timeout_seconds,
            native_receipt=(
                native_receipt if item["kind"] == "fbx" else None
            ),
            force_reexport=force_reexport,
        )
        results[item["kind"]]["bundled_process"] = False
        _reconcile_completed_bundle_results(
            results, native_receipt, spm, create_backup=True
        )
        return results
    if len(prepared) != 2:
        raise RuntimeError("Bundled SpeedTree export requires exactly FBX and XML.")

    prepared.sort(key=lambda item: item["kind"] != "fbx")
    primary, secondary = prepared
    common_root = Path(
        os.path.commonpath(
            [str(item["target"].parent.resolve()) for item in prepared]
        )
    )
    export_attempts = []
    stdout = ""
    stderr = ""
    bundle_reconciliation = None
    for attempt in range(1, _EXPORT_RETRY_ATTEMPTS + 1):
        with tempfile.TemporaryDirectory(
            prefix="bwr_speedtree_bundle_"
        ) as temp_dir:
            staging_root = Path(temp_dir)
            staged = {}
            for item in prepared:
                relative = item["target"].resolve().relative_to(common_root)
                staged[item["kind"]] = staging_root / relative
                staged[item["kind"]].parent.mkdir(parents=True, exist_ok=True)
            staged_receipt = None
            command = [str(exe)]
            if native_receipt is not None:
                relative_receipt = native_receipt.resolve().relative_to(common_root)
                staged_receipt = staging_root / relative_receipt
                staged_receipt.parent.mkdir(parents=True, exist_ok=True)
                command.extend(["--native-receipt", str(staged_receipt)])
            command.extend([
                "--secondary-export-options",
                str(secondary["options"]),
                "--secondary-export",
                str(staged[secondary["kind"]]),
                str(spm),
                "-export_options",
                str(primary["options"]),
                "-export",
                str(staged[primary["kind"]]),
            ])
            try:
                returncode, stdout, stderr = _run_process(
                    command,
                    cwd=spm.parent,
                    timeout_seconds=timeout_seconds,
                )
            except subprocess.TimeoutExpired:
                return _export_bundle_fallback(
                    exe,
                    spm,
                    prepared,
                    results,
                    timeout_seconds,
                    native_receipt,
                    force_reexport,
                )

            attempt_record = {
                "attempt": attempt,
                "returncode": returncode,
                "windows_exit_code": f"0x{_windows_exit_code(returncode):08X}",
            }
            export_attempts.append(attempt_record)
            if returncode != 0:
                failure_kind = _retryable_export_failure_kind(returncode)
                attempt_record["failure_kind"] = (
                    failure_kind or "process_export_failed"
                )
                if failure_kind and attempt < _EXPORT_RETRY_ATTEMPTS:
                    backoff = _EXPORT_RETRY_BACKOFF_SECONDS[attempt - 1]
                    attempt_record["retry_backoff_seconds"] = backoff
                    time.sleep(backoff)
                    continue
                return _export_bundle_fallback(
                    exe,
                    spm,
                    prepared,
                    results,
                    timeout_seconds,
                    native_receipt,
                    force_reexport,
                )
            invalid = [
                item["kind"]
                for item in prepared
                if not _basic_output_is_valid(
                    item["kind"], staged[item["kind"]], parse_xml=True
                )
            ]
            if invalid:
                return _export_bundle_fallback(
                    exe,
                    spm,
                    prepared,
                    results,
                    timeout_seconds,
                    native_receipt,
                    force_reexport,
                )
            if (
                staged_receipt is not None
                and not _native_receipt_is_valid(staged_receipt, spm)
            ):
                return _export_bundle_fallback(
                    exe,
                    spm,
                    prepared,
                    results,
                    timeout_seconds,
                    native_receipt,
                    force_reexport,
                )
            if staged_receipt is not None and "xml" in staged:
                bundle_reconciliation = reconcile_xml_with_native_receipt(
                    staged["xml"],
                    staged_receipt,
                    spm,
                    create_backup=False,
                )
                if bundle_reconciliation.get("changed"):
                    xml_item = next(
                        item for item in prepared if item["kind"] == "xml"
                    )
                    if xml_item["target"].is_file():
                        backup = _verified_xml_backup(xml_item["target"])
                        bundle_reconciliation["backup"] = str(backup)
            _transactional_promote(staging_root, common_root)
            break

    finished = _utc_timestamp()
    for item in prepared:
        artifact_paths = [item["target"]]
        if item["kind"] == "fbx":
            artifact_paths.append(item["target"].with_suffix(".stmat"))
            if native_receipt is not None:
                artifact_paths.append(native_receipt)
        artifacts = [
            _artifact_record(path, item["target"].parent)
            for path in artifact_paths
        ]
        cache_data = {
            "version": EXPORT_CACHE_VERSION,
            "kind": item["kind"],
            "target": str(item["target"].resolve()),
            "input_fingerprint": item["fingerprint"],
            "inputs": item["inputs"],
            "artifacts": artifacts,
            "completed_at": finished,
            "bundled_process": True,
            "native_receipt": (
                str(native_receipt)
                if native_receipt is not None and item["kind"] == "fbx"
                else ""
            ),
        }
        if bundle_reconciliation is not None:
            cache_data["native_receipt_xml_reconciliation"] = (
                bundle_reconciliation
            )
        _write_cache(item["cache_path"], cache_data)
        results[item["kind"]] = {
            "path": str(item["target"]),
            "export_options": str(item["options"]),
            "exists": item["target"].exists(),
            "size": item["target"].stat().st_size,
            "returncode": 0,
            "started": item["started"],
            "finished": finished,
            "stdout": stdout[-4000:],
            "stderr": stderr[-4000:],
            "cache_hit": False,
            "cache_seeded": False,
            "cache_path": str(item["cache_path"]),
            "input_fingerprint": item["fingerprint"],
            "artifacts": artifacts,
            "export_attempts": export_attempts,
            "bundled_process": True,
            "force_reexport_requested": bool(force_reexport),
            "native_receipt": (
                str(native_receipt)
                if native_receipt is not None and item["kind"] == "fbx"
                else ""
            ),
        }
        if bundle_reconciliation is not None:
            results[item["kind"]]["native_receipt_xml_reconciliation"] = (
                bundle_reconciliation
            )
    return results
