"""Strict adapter for the shared SpeedTree handoff contract.

The shared rules live beside ``substance-tools/pipeline_contract.json``.  This
module stays stdlib-only so report/provenance failures can be rejected before
Blender data is changed.  Legacy reports never enter this adapter and retain
the add-on's existing fallback behavior.
"""
from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import os
from functools import lru_cache
from pathlib import Path


CENTRAL_MODULE_NAME = "speedtree_handoff_contract.py"
CENTRAL_MODULE_ENV = "SPEEDTREE_HANDOFF_CONTRACT_PATH"
PIPELINE_ENVELOPE_FIELD = "speedtree_pipeline_contract"
CONTENT_FINGERPRINT_POLICY = "canonical_path_sha256_size_v1"
SOURCE_MODES = {
    "managed_texture_set",
    "preserve_declared_sources",
    "unresolved",
}
TEXTURE_CONTRACT_MODE_FIELD = "texture_contract_mode"
RUNTIME_TOLERANT_TEXTURE_MODE = "runtime_tolerant"
STRICT_PUBLICATION_TEXTURE_MODE = "strict_publication"
TEXTURE_CONTRACT_MODES = {
    RUNTIME_TOLERANT_TEXTURE_MODE,
    STRICT_PUBLICATION_TEXTURE_MODE,
}


@lru_cache(maxsize=1)
def _preview_contract_api():
    """Load the sibling pure module with or without package context."""
    try:
        from . import preview_texture_contract

        return preview_texture_contract
    except ImportError:
        candidate = Path(__file__).with_name(
            "preview_texture_contract.py"
        )
        spec = importlib.util.spec_from_file_location(
            "speedtree_bwr_preview_texture_contract",
            candidate,
        )
        if spec is None or spec.loader is None:
            raise RuntimeError(
                "SpeedTree preview texture contract is unavailable"
            )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module


def _central_candidates():
    explicit = str(os.environ.get(CENTRAL_MODULE_ENV) or "").strip()
    if explicit:
        yield Path(explicit).expanduser()

    source = Path(__file__).resolve()
    seen = set()
    for parent in source.parents:
        candidate = parent / "substance-tools" / CENTRAL_MODULE_NAME
        key = os.path.normcase(str(candidate))
        if key not in seen:
            seen.add(key)
            yield candidate

    candidate = (
        Path.home()
        / "Documents"
        / "GitHub"
        / "substance-tools"
        / CENTRAL_MODULE_NAME
    )
    key = os.path.normcase(str(candidate))
    if key not in seen:
        yield candidate


@lru_cache(maxsize=1)
def central_contract_api():
    checked = []
    for candidate in _central_candidates():
        checked.append(str(candidate))
        if not candidate.is_file():
            continue
        spec = importlib.util.spec_from_file_location(
            "speedtree_bwr_shared_handoff_contract", candidate
        )
        if spec is None or spec.loader is None:
            continue
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    raise RuntimeError(
        "Shared SpeedTree handoff contract is unavailable. Checked: "
        + ", ".join(checked)
    )


def envelope_field():
    api = central_contract_api()
    configured = str(api.preflight_report_rules().get("envelope_field") or "")
    return configured or PIPELINE_ENVELOPE_FIELD


def _content_identity(value):
    if isinstance(value, dict):
        return {
            key: _content_identity(item)
            for key, item in sorted(value.items())
            if key != "mtime_ns"
        }
    if isinstance(value, list):
        return [_content_identity(item) for item in value]
    return value


def source_fingerprint(source, policy=""):
    if policy not in ("", CONTENT_FINGERPRINT_POLICY):
        raise ValueError(
            "unsupported SpeedTree preflight source fingerprint policy: "
            + str(policy)
        )
    payload = (
        _content_identity(source)
        if policy == CONTENT_FINGERPRINT_POLICY
        else source
    )
    encoded = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def sha256_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _resolved_path(path):
    return Path(path).expanduser().resolve(strict=True)


def _path_key(path):
    return os.path.normcase(str(_resolved_path(path))).casefold()


def _validate_live_identity(identity, label, expected_path=None):
    recorded_path = str(identity.get("canonical_path") or "").strip()
    try:
        live_path = _resolved_path(recorded_path)
    except (OSError, RuntimeError) as exc:
        raise RuntimeError(f"{label} source is missing: {recorded_path}") from exc

    if expected_path:
        try:
            expected_key = _path_key(expected_path)
        except (OSError, RuntimeError) as exc:
            raise RuntimeError(f"Current {label} source is missing: {expected_path}") from exc
        if _path_key(live_path) != expected_key:
            raise RuntimeError(
                f"{label} canonical path mismatch: {live_path} != "
                f"{_resolved_path(expected_path)}"
            )

    stable = None
    for _attempt in range(2):
        try:
            before = live_path.stat()
            live_sha256 = sha256_file(live_path)
            after = live_path.stat()
        except OSError as exc:
            raise RuntimeError(f"{label} source could not be read: {live_path}") from exc
        if (before.st_size, before.st_mtime_ns) == (
            after.st_size,
            after.st_mtime_ns,
        ):
            stable = (after.st_size, live_sha256)
            break
    if stable is None:
        raise RuntimeError(f"{label} source changed while validating: {live_path}")
    live_size, live_sha256 = stable
    recorded_sha256 = str(identity.get("sha256") or "").strip().casefold()
    if live_sha256 != recorded_sha256:
        raise RuntimeError(
            f"{label} source hash mismatch; preflight is stale: {live_path}"
        )
    if "size" in identity and int(identity["size"]) != live_size:
        raise RuntimeError(
            f"{label} source size mismatch; preflight is stale: {live_path}"
        )
    return {
        "canonical_path": str(live_path),
        "sha256": live_sha256,
        "size": live_size,
    }


def _validate_managed_binding(intent, required_roles):
    mode = str(intent.get("texture_source_mode") or "").strip()
    if mode not in SOURCE_MODES:
        raise ValueError(
            f"Invalid texture_source_mode for {intent.get('material_name')!r}: {mode!r}"
        )
    if mode == "unresolved":
        raise ValueError(
            f"Unresolved texture source in successful preflight: "
            f"{intent.get('material_name')!r}"
        )
    binding = intent.get("texture_binding")
    if not isinstance(binding, dict):
        raise ValueError(
            f"Material intent has no texture_binding: {intent.get('material_name')!r}"
        )
    origin_receipt = binding.get("origin_receipt")
    preview_api = _preview_contract_api()
    declares_preview = preview_api.receipt_declares_preview_fallback(
        origin_receipt
    )
    if (
        isinstance(origin_receipt, dict)
        and origin_receipt.get(
            preview_api.RECEIPT_CAPABILITIES_FIELD
        )
        and not declares_preview
    ):
        raise ValueError("unsupported texture receipt capability")
    if declares_preview:
        preview_api.validate_preview_receipt(
            origin_receipt,
            requested_usage=(
                preview_api.PREVIEW_ONLY_USAGE
                if mode == "preserve_declared_sources"
                else "production_canonical"
            ),
        )
    if mode == "preserve_declared_sources":
        return
    if str(binding.get("status") or "") != "ok":
        raise ValueError(
            f"Managed texture binding is not ready for "
            f"{intent.get('material_name')!r}: {binding.get('status')!r}"
        )
    files = binding.get("files")
    if not isinstance(files, dict):
        raise ValueError(
            f"Managed texture binding has no files for {intent.get('material_name')!r}"
        )
    for role in required_roles:
        value = str(files.get(role) or "").strip()
        path = Path(value)
        try:
            ready = path.is_file() and path.stat().st_size > 0
        except OSError:
            ready = False
        if not ready:
            raise RuntimeError(
                f"Managed texture binding is stale for "
                f"{intent.get('material_name')!r}: {role} -> {value or '<missing>'}"
            )


def _texture_issue_code(exc):
    message = str(exc).casefold()
    if "preview" in message:
        return "preview_receipt_not_production_capable"
    if "capability" in message:
        return "unsupported_texture_receipt_capability"
    if "stale" in message:
        return "stale_texture_binding"
    if "unresolved" in message:
        return "unresolved_texture_binding"
    if "no texture_binding" in message:
        return "missing_texture_binding"
    if "not ready" in message:
        return "incomplete_texture_binding"
    return "texture_binding_rejected"


def _texture_issue_severity(code):
    # Runtime texture authority and availability are telemetry, not admission
    # or operator-action events. Actual image decode/I/O warnings are emitted
    # later by Blender when a chosen file cannot be consumed.
    del code
    return "info"


def _live_runtime_files(binding, required_roles):
    """Return only currently readable files from an otherwise untrusted row."""
    files = binding.get("files") if isinstance(binding, dict) else None
    if not isinstance(files, dict):
        return {}, list(required_roles)
    available = {}
    missing = []
    for role in required_roles:
        value = str(files.get(role) or "").strip()
        path = Path(value)
        try:
            ready = path.is_file() and path.stat().st_size > 0
        except OSError:
            ready = False
        if ready:
            available[role] = str(path)
        else:
            missing.append(role)
    return available, missing


def _runtime_tolerant_binding(intent, required_roles):
    """Quarantine one invalid texture binding without rejecting the handoff.

    This is deliberately downstream of the strict validator.  Receipt and
    publication audits remain fail-closed; the BAT runtime converts their
    rejection into an empty parameter assignment plus a structured diagnostic.
    """
    normalized = copy.deepcopy(intent)
    binding = normalized.get("texture_binding")
    binding = copy.deepcopy(binding) if isinstance(binding, dict) else {}
    mode = str(normalized.get("texture_source_mode") or "").strip()
    diagnostic = None
    try:
        _validate_managed_binding(normalized, required_roles)
    except (RuntimeError, TypeError, ValueError) as exc:
        code = _texture_issue_code(exc)
        diagnostic = {
            "code": code,
            "severity": _texture_issue_severity(code),
            "material": str(normalized.get("material_name") or ""),
            "message": str(exc),
        }

    if diagnostic is None:
        if mode == "preserve_declared_sources":
            binding["binding_disposition"] = "preserve_declared_sources"
            binding["available_roles"] = sorted(
                str(role) for role in (binding.get("source_roles") or [])
            )
        else:
            binding["binding_disposition"] = "bind_available"
            binding["available_roles"] = sorted(
                str(role) for role in (binding.get("files") or {})
            )
            binding["missing_roles"] = []
        normalized["texture_binding"] = binding
        return normalized, None

    # Only a receipt/capability/usage rejection invalidates the whole row.
    # Ordinary incomplete or stale availability keeps the currently live roles
    # even when the otherwise valid production row carries a receipt.
    receipt_rejected = diagnostic["code"] in {
        "preview_receipt_not_production_capable",
        "unsupported_texture_receipt_capability",
        "texture_binding_rejected",
    }
    if receipt_rejected:
        available, missing = {}, list(required_roles)
    elif mode == "managed_texture_set":
        available, missing = _live_runtime_files(binding, required_roles)
    else:
        available, missing = {}, list(required_roles)

    # Strip every field that could make a downstream consumer treat this row
    # as an authoritative manifest/receipt binding.  The rejected provenance
    # remains represented only by the diagnostic code below.
    for field in (
        "origin_receipt",
        "slot_files",
        "source_paths",
        "source_roles",
        "source_maps",
        "preserved_files",
        "declared_source_receipt",
        "expected_t_paths",
        "expected_texture_base",
        "manifest_path",
        "texture_contract_status",
        "source_origin",
        "source_evidence",
        "origin_state",
    ):
        binding.pop(field, None)
    binding["files"] = available
    binding["available_roles"] = sorted(available)
    binding["missing_roles"] = sorted(set(missing))
    binding["binding_disposition"] = (
        "bind_available" if available else "leave_unassigned"
    )
    binding["status"] = "partial" if available else "unassigned"
    binding["warning_codes"] = (
        [diagnostic["code"]]
        if diagnostic.get("severity") == "warning"
        else []
    )
    binding["diagnostic_codes"] = [diagnostic["code"]]
    normalized["texture_binding"] = binding
    normalized["texture_source_mode"] = (
        "managed_texture_set" if available else "unresolved"
    )
    return normalized, diagnostic


def _is_texture_only_issue(issue):
    if not isinstance(issue, dict):
        return False
    code = str(issue.get("code") or "").strip().upper()
    scope = str(issue.get("scope") or "").strip().casefold()
    return (
        code.startswith("TEXTURE_")
        or code.startswith("CANONICAL_TEXTURE_")
        or code.startswith("ATLAS_TEXTURE_")
        or scope in {"texture", "texture_binding", "texture_set"}
    )


def validate_live_preflight_envelope(
    envelope,
    *,
    spm_path,
    stmat_paths=(),
    expected_mesh_name="",
    texture_contract_mode=STRICT_PUBLICATION_TEXTURE_MODE,
):
    """Validate schema plus live SPM/STMAT identity before Blender mutation."""
    api = central_contract_api()
    expected_mesh_name = expected_mesh_name or Path(spm_path).stem
    validated = api.validate_preflight_envelope(
        envelope, expected_mesh_name=expected_mesh_name
    )
    if texture_contract_mode not in TEXTURE_CONTRACT_MODES:
        raise ValueError(
            "unsupported SpeedTree texture contract mode: "
            + str(texture_contract_mode)
        )
    outcome = str(validated.get("outcome") or "")
    issues = list(validated.get("issues") or [])
    texture_only_block = bool(
        texture_contract_mode == RUNTIME_TOLERANT_TEXTURE_MODE
        and outcome != "ok"
        and issues
        and all(_is_texture_only_issue(issue) for issue in issues)
    )
    if outcome != "ok" and not texture_only_block:
        raise RuntimeError(
            "SpeedTree material preflight is not ready: "
            + str(outcome or "unknown")
        )

    source = validated["source"]
    recorded_fingerprint = str(validated.get("source_fingerprint") or "").casefold()
    fingerprint_policy = str(
        validated.get("source_fingerprint_policy") or ""
    )
    if recorded_fingerprint != source_fingerprint(source, fingerprint_policy):
        raise RuntimeError("SpeedTree preflight source fingerprint mismatch")

    live = {
        "spm": _validate_live_identity(source["spm"], "SPM", spm_path),
        "stmat": [],
    }
    recorded_stmat = source.get("stmat") or []
    if not recorded_stmat:
        raise RuntimeError("Successful SpeedTree preflight has no STMAT provenance")
    by_path = {}
    for index, identity in enumerate(recorded_stmat):
        row = _validate_live_identity(identity, f"STMAT[{index}]")
        live["stmat"].append(row)
        by_path[_path_key(row["canonical_path"])] = row

    for expected_path in stmat_paths or ():
        if not expected_path:
            continue
        try:
            expected_key = _path_key(expected_path)
        except (OSError, RuntimeError) as exc:
            raise RuntimeError(f"Current STMAT source is missing: {expected_path}") from exc
        if expected_key not in by_path:
            raise RuntimeError(
                f"Current STMAT is not owned by this preflight: "
                f"{_resolved_path(expected_path)}"
            )

    required_roles = tuple(api.required_texture_roles())
    texture_diagnostics = []
    runtime_intents = []
    for intent in validated.get("material_intents") or []:
        if texture_contract_mode == RUNTIME_TOLERANT_TEXTURE_MODE:
            runtime_intent, diagnostic = _runtime_tolerant_binding(
                intent, required_roles
            )
            runtime_intents.append(runtime_intent)
            if diagnostic is not None:
                texture_diagnostics.append(diagnostic)
        else:
            _validate_managed_binding(intent, required_roles)
            runtime_intents.append(copy.deepcopy(intent))
    validated["material_intents"] = runtime_intents
    validated[TEXTURE_CONTRACT_MODE_FIELD] = texture_contract_mode
    validated["texture_only_outcome_override"] = texture_only_block
    validated["texture_diagnostics"] = texture_diagnostics
    validated["texture_warnings"] = [
        row for row in texture_diagnostics
        if row.get("severity") == "warning"
    ]
    available_counts = [
        len((intent.get("texture_binding") or {}).get("files") or {})
        for intent in runtime_intents
    ]
    if texture_only_block:
        validated["texture_outcome"] = (
            "partial" if any(available_counts) else "unassigned"
        )
    elif texture_diagnostics:
        validated["texture_outcome"] = (
            "partial" if any(available_counts) else "unassigned"
        )
    else:
        validated["texture_outcome"] = "complete"
    return copy.deepcopy(validated), live


def texture_bindings_from_envelope(envelope):
    """Project authoritative intent bindings into the legacy BWR wiring shape."""
    rows = []
    for intent in envelope.get("material_intents") or []:
        binding = dict(intent.get("texture_binding") or {})
        binding.update(
            {
                "material": intent.get("material_name", ""),
                "material_key": intent.get("material_key", ""),
                "production_group_base": intent.get(
                    "production_group_base", ""
                ),
                "material_index": intent.get("stmat_material_index"),
                "texture_source_mode": intent.get("texture_source_mode", ""),
            }
        )
        rows.append(binding)
    return rows


def _intent_semantics(intent):
    return (
        intent.get("tree_part"),
        intent.get("tree_shading"),
        intent.get("instance_profile") or "",
    )


def resolve_material_intent(material_name, envelope):
    """Resolve one final Blender material by exact key or production-group base.

    PCG Atlas Auto Split tokens are deliberately absent from this resolver.
    """
    api = central_contract_api()
    intents = list(envelope.get("material_intents") or [])
    material_key = api.normalize_material_key(material_name)
    exact = [row for row in intents if row.get("material_key") == material_key]
    match_mode = "exact_material_key"
    candidates = exact
    if not candidates:
        base_name = api.production_group_base_name(material_name)
        base_key = api.normalize_material_key(base_name)
        if base_key:
            candidates = [
                row
                for row in intents
                if api.normalize_material_key(row.get("production_group_base"))
                == base_key
            ]
        match_mode = "production_group_base"
    if not candidates:
        return None

    semantics = {_intent_semantics(row) for row in candidates}
    if len(semantics) != 1:
        sources = ", ".join(
            str(row.get("material_name") or "?") for row in candidates
        )
        raise RuntimeError(
            f"Conflicting SpeedTree material intents for {material_name!r}: {sources}"
        )
    tree_part, tree_shading, profile = next(iter(semantics))
    return {
        "match_mode": match_mode,
        "material_name": material_name,
        "material_key": material_key,
        "material_instance_base": api.material_instance_base_name(material_name),
        "tree_part": tree_part,
        "tree_shading": tree_shading,
        "instance_profile": profile,
        "source_materials": [
            str(row.get("material_name") or "") for row in candidates
        ],
    }


def normalize_instance_profile(value):
    return central_contract_api().normalize_instance_profile(value)


def production_group_tokens(value):
    return central_contract_api().production_group_tokens(value)


def production_group_base_name(value):
    return central_contract_api().production_group_base_name(value)


__all__ = [
    "PIPELINE_ENVELOPE_FIELD",
    "RUNTIME_TOLERANT_TEXTURE_MODE",
    "STRICT_PUBLICATION_TEXTURE_MODE",
    "TEXTURE_CONTRACT_MODE_FIELD",
    "central_contract_api",
    "envelope_field",
    "normalize_instance_profile",
    "production_group_base_name",
    "production_group_tokens",
    "resolve_material_intent",
    "sha256_file",
    "source_fingerprint",
    "texture_bindings_from_envelope",
    "validate_live_preflight_envelope",
]
