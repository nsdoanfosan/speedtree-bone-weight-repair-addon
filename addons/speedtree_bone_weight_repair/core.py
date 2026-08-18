import colorsys
import hashlib
import json
import math
import os
import re
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from time import perf_counter

import bpy
import bmesh
import numpy as np
from mathutils import Vector, kdtree

from . import handoff_contract, speedtree_cli, spm_reader
from .preview_texture_contract import (
    PREVIEW_ONLY_USAGE,
    PREVIEW_RECEIPT_VERSION,
    PREVIEW_ROLE_FALLBACKS_FIELD,
    RECEIPT_CAPABILITIES_FIELD,
    build_preview_role_fallback,
    finalize_preview_receipt,
    preview_role_fallbacks_signature,
    receipt_declares_preview_fallback,
    validate_preview_receipt,
)

# Parked 3D Branch Cluster prototype.
# The active add-on no longer imports/registers this path. Keep the separate
# branch_clusters.py module as reference only unless the workflow returns to it.
# from . import branch_clusters


EPSILON = 1e-6
COORD_RE = re.compile(r"X:([-+0-9.eE]+),\s*Y:([-+0-9.eE]+),\s*Z:([-+0-9.eE]+)")
JSON_PREVIEW_ATTR_NAME = "Codex_JSON_Group_Preview"
JSON_PREVIEW_SCENE_KEY = "codex_json_preview_active"
JSON_PREVIEW_COLLECTION_HIDE_KEY = "codex_json_preview_original_hide_viewport"
JSON_PREVIEW_ARMATURE_HIDE_KEY = "codex_json_preview_armature_prev_hide"
CLUSTER_NORMALIZER_GENERATED_KEYS = (
    "speedtree_cluster_generated",
    "atlas_leaf_cluster_generated",
)
SPEEDTREE_MODEL_USER_DATA_PROPERTY = "SpeedTree SDK:User data"
UNREAL_INSTANCE_PROFILE_PROPERTY = "unreal_instance_profile"
UNREAL_INSTANCE_PROFILE_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}\Z")
UNREAL_TREE_PART_PROPERTY = "unreal_tree_part"
UNREAL_TREE_SHADING_PROPERTY = "unreal_tree_shading"
JSON_PREVIEW_OBJECT_KEYS = (
    "codex_json_group",
    "codex_json_group_matched_by",
    "codex_json_preview_source",
    # Legacy key from the old material-swap preview; still cleared off old scenes.
    "codex_json_preview_original_materials",
)


def _live_file_identity(path):
    """Hash one live source file without trusting a historical receipt."""
    source = Path(path).resolve()
    if not source.is_file():
        raise RuntimeError(f"SpeedTree live source does not exist: {source}")
    digest = hashlib.sha256()
    with source.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return {
        "canonical_path": str(source),
        "sha256": digest.hexdigest(),
        "size": source.stat().st_size,
    }


def _live_speedtree_source_identity(spm_path, stmat_paths):
    """Describe the current SPM/STMAT files used by this exact import."""
    return {
        "spm": _live_file_identity(spm_path),
        "stmat": sorted(
            (_live_file_identity(path) for path in stmat_paths),
            key=lambda row: (
                row["canonical_path"].casefold(),
                row["sha256"],
                row["size"],
            ),
        ),
    }


def write_report(path, data):
    if not path:
        return
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def is_cluster_normalizer_generated(data_block):
    return bool(
        data_block
        and any(data_block.get(key) for key in CLUSTER_NORMALIZER_GENERATED_KEYS)
    )


def load_speedtree_texture_readiness_contract(
    path, *, spm_path="", source_fbx_path=""
):
    if not path:
        return None
    contract_path = Path(path)
    if not contract_path.is_file():
        raise RuntimeError(f"SpeedTree texture contract does not exist: {path}")
    try:
        payload = json.loads(contract_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(
            f"SpeedTree texture contract could not be read: {path} ({exc})"
        ) from exc
    if not isinstance(payload, dict):
        raise RuntimeError(
            "SpeedTree texture contract root must be a JSON object"
        )
    if handoff_contract.PIPELINE_ENVELOPE_FIELD in payload:
        envelope = payload.get(handoff_contract.PIPELINE_ENVELOPE_FIELD)
        if not isinstance(envelope, dict):
            raise RuntimeError(
                "SpeedTree pipeline contract envelope must be a JSON object"
            )
        if not spm_path:
            raise RuntimeError(
                "New SpeedTree preflight contract requires the current SPM path"
            )
        stmat_paths = (
            [Path(source_fbx_path).with_suffix(".stmat")]
            if source_fbx_path
            else []
        )
        try:
            validated = handoff_contract.central_contract_api().validate_preflight_envelope(
                envelope, expected_mesh_name=Path(spm_path).stem
            )
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            raise RuntimeError(
                f"SpeedTree material contract could not be read: {exc}"
            ) from exc
        live_source = _live_speedtree_source_identity(
            spm_path, stmat_paths
        )
        live_source["historical_identity_fields_are_diagnostic"] = True
        tree_user_data = validated.get("tree_user_data") or {}
        profile_inspection = inspect_spm_unreal_instance_profile(spm_path)
        reported_profile = handoff_contract.normalize_instance_profile(
            validated.get("instance_profile")
        )

        contract = {
            "status": "ok",
            "bindings": handoff_contract.texture_bindings_from_envelope(
                validated
            ),
            "strict_speedtree_pipeline_contract": True,
            "speedtree_pipeline_contract": validated,
            "live_source_identity": live_source,
            "instance_profile": reported_profile,
            "tree_user_data": dict(tree_user_data),
            "profile_inspection": profile_inspection,
            "historical_identity_fields_are_diagnostic": True,
            handoff_contract.TEXTURE_CONTRACT_MODE_FIELD: (
                handoff_contract.RUNTIME_TOLERANT_TEXTURE_MODE
            ),
            "texture_outcome": validated.get(
                "texture_outcome", "complete"
            ),
            "texture_diagnostics": list(
                validated.get("texture_diagnostics") or []
            ),
            "texture_warnings": list(
                validated.get("texture_warnings") or []
            ),
        }
    else:
        contract = payload.get("texture_readiness_contract", payload)
    if not isinstance(contract, dict) or not isinstance(
        contract.get("bindings"), list
    ):
        raise RuntimeError(
            f"SpeedTree texture contract has no bindings: {path}"
        )
    contract = dict(contract)
    contract.setdefault(
        handoff_contract.TEXTURE_CONTRACT_MODE_FIELD,
        handoff_contract.RUNTIME_TOLERANT_TEXTURE_MODE,
    )
    contract["contract_path"] = str(contract_path.resolve())
    return contract


def utc_timestamp():
    return datetime.now(timezone.utc).isoformat()


BUNDLED_PRESET_DIR = Path(__file__).parent / "presets" / "speedtree_10_1"
BUNDLED_FBX_EXPORT_OPTIONS = BUNDLED_PRESET_DIR / "Options_MA_Fbx.ini"
BUNDLED_FBX_NO_BONES_EXPORT_OPTIONS = BUNDLED_PRESET_DIR / "Options_MA_Fbx_NoBones.ini"
BUNDLED_XML_EXPORT_OPTIONS = BUNDLED_PRESET_DIR / "Options_HI_Xml.ini"
LEGACY_BUNDLED_EXPORT_OPTIONS = Path(__file__).parent / "presets" / "Options_Fbx.ini"


def inspect_spm_bone_generators(spm_path):
    try:
        return spm_reader.get_derived(
            spm_path,
            "bone_generators_v1",
            _inspect_spm_bone_generators_root,
        )
    except Exception as exc:
        return {
            "inspection_ok": False,
            "inspection_error": str(exc),
            "visible_branch_generators": 0,
            "enabled_branch_generators": 1,
            "disabled_generators": [],
        }


def _inspect_spm_bone_generators_root(root):
    visible = []
    enabled = []
    disabled = []
    for gen in root.findall(".//Generator"):
        if gen.attrib.get("Type") != "Branch" or child_bool(gen, "Hidden", False):
            continue
        style = element_property_value(gen, "Physics:Bone style")
        bones = element_property_value(gen, "Physics:Bones")
        if style is None or bones is None:
            continue
        item = {
            "generator": gen.findtext("Name") or "?",
            "style": float(style),
            "bones": float(bones),
        }
        visible.append(item)
        if item["style"] == 0.0 and item["bones"] == 0.0:
            disabled.append(item)
        else:
            enabled.append(item)
    return {
        "inspection_ok": True,
        "visible_branch_generators": len(visible),
        "enabled_branch_generators": len(enabled),
        "disabled_generators": disabled,
    }


def spm_has_enabled_bone_generators(spm_path):
    return inspect_spm_bone_generators(spm_path)["enabled_branch_generators"] > 0


def require_spm_sk_ready(spm_path):
    bone_status = inspect_spm_bone_generators(spm_path)
    has_enabled_bones = bone_status["enabled_branch_generators"] > 0
    if (
        bone_status.get("inspection_ok")
        and bone_status["visible_branch_generators"] > 0
        and not has_enabled_bones
    ):
        settings = ", ".join(
            f"{item['generator']}(style={item['style']:g}, bones={item['bones']:g})"
            for item in bone_status["disabled_generators"]
        )
        raise RuntimeError(
            "SPM is not SK-ready: every visible Branch bone generator is Absolute/0 "
            f"(bones disabled): {settings}. Configure bones on at least one visible "
            "Branch generator before running the SK batch."
        )
    return bone_status


def default_speedtree_export_options(spm_path, kind="fbx"):
    # The add-on ships the project defaults so a fresh checkout exports the same
    # way everywhere: FBX grouped by material, XML grouped by hierarchy.
    kind = (kind or "fbx").lower()
    bundled = BUNDLED_XML_EXPORT_OPTIONS if kind == "xml" else BUNDLED_FBX_EXPORT_OPTIONS
    if bundled.exists():
        return str(bundled)

    # Fallbacks only apply if the bundled preset is missing.
    spm_dir = Path(spm_path).parent if spm_path else Path.cwd()
    sibling_name = "Options_HI_Xml.ini" if kind == "xml" else "Options_MA_Fbx.ini"
    sibling = spm_dir / sibling_name
    if sibling.exists():
        return str(sibling)
    if kind == "fbx" and LEGACY_BUNDLED_EXPORT_OPTIONS.exists():
        return str(LEGACY_BUNDLED_EXPORT_OPTIONS)
    return r"C:\Program Files\SpeedTree\SpeedTree Modeler v10.1.0\export_presets\VFX\__FBX.ini"


def run_speedtree_cli_export(
    spm_path,
    speedtree_exe_path="",
    export_options_path="",
    fbx_export_options_path="",
    xml_export_options_path="",
    output_root="",
    name_stem="",
    export_fbx=True,
    export_xml=True,
    timeout_seconds=900,
    allow_boneless=False,
    allow_manual_bones=False,
):
    spm = Path(spm_path)
    if not spm.exists():
        raise RuntimeError(f"SPM does not exist: {spm_path}")

    exe = Path(speedtree_exe_path or r"C:\Program Files\SpeedTree\SpeedTree Modeler v10.1.0\win64\SpeedTree_Modeler.exe")
    if not exe.exists():
        raise RuntimeError(f"SpeedTree Modeler executable does not exist: {exe}")

    root = Path(output_root) if output_root else spm.parent
    stem = name_stem or spm.stem
    bone_status = (
        inspect_spm_bone_generators(spm)
        if allow_boneless or allow_manual_bones
        else require_spm_sk_ready(spm)
    )
    has_enabled_bones = bone_status["enabled_branch_generators"] > 0
    targets = []
    if export_fbx:
        options = Path(fbx_export_options_path or export_options_path or default_speedtree_export_options(spm_path, "fbx"))
        if (
            allow_boneless
            and not has_enabled_bones
            and BUNDLED_FBX_NO_BONES_EXPORT_OPTIONS.exists()
        ):
            options = BUNDLED_FBX_NO_BONES_EXPORT_OPTIONS
        targets.append(("fbx", root / "fbx" / f"{stem}.fbx", options))
    if export_xml:
        options = Path(xml_export_options_path or export_options_path or default_speedtree_export_options(spm_path, "xml"))
        targets.append(("xml", root / "xml" / f"{stem}.xml", options))
    if not targets:
        raise RuntimeError("Enable at least one SpeedTree export target.")

    results = {}
    export_options = {}
    for kind, target, options in targets:
        if not options.exists():
            raise RuntimeError(f"SpeedTree {kind.upper()} export options INI does not exist: {options}")
        export_options[kind] = str(options)
        target.parent.mkdir(parents=True, exist_ok=True)
    if (
        len(targets) == 2
        and exe.name.casefold() == "speedtree_collision_cli.exe"
    ):
        results = speedtree_cli.export_bundle(
            exe=exe,
            spm=spm,
            targets=targets,
            timeout_seconds=timeout_seconds,
        )
    else:
        for kind, target, options in targets:
            results[kind] = speedtree_cli.export_target(
                exe=exe,
                spm=spm,
                options=options,
                kind=kind,
                target=target,
                timeout_seconds=timeout_seconds,
            )

    bundle_mtime_sync = None
    if "fbx" in results and "xml" in results:
        bundle_minimum_mtime = max(
            spm.stat().st_mtime_ns,
            Path(results["fbx"]["path"]).stat().st_mtime_ns,
        )
        bundle_mtime_sync = speedtree_cli.synchronize_result_mtime(
            results["xml"],
            bundle_minimum_mtime,
        )

    return {
        "speedtree_exe": str(exe),
        "spm": str(spm),
        "export_options": export_options,
        "output_root": str(root),
        "name_stem": stem,
        "spm_has_enabled_bones": has_enabled_bones,
        "manual_bones_preserved": bool(allow_manual_bones),
        "spm_bone_generators": bone_status,
        "export_cache_version": speedtree_cli.EXPORT_CACHE_VERSION,
        "export_bundle_mtime_sync": bundle_mtime_sync,
        "exports": results,
    }


def remove_phantom_image_nodes(objects):
    # SpeedTree FBX exports declare texture slots (e.g. bark Opacity) whose
    # files were never written; the importer then creates image nodes with no
    # pixel data and no file on disk. Those phantoms break the Unreal Handoff
    # validation and Prepare External Textures on every import, so strip them.
    removed = []
    seen_materials = set()
    candidate_images = set()
    for obj in objects:
        if obj.type != "MESH" or not obj.data:
            continue
        for material in obj.data.materials:
            if not material or material.name in seen_materials or not material.node_tree:
                continue
            seen_materials.add(material.name)
            for node in list(material.node_tree.nodes):
                if node.type != "TEX_IMAGE" or not node.image:
                    continue
                image = node.image
                if image.has_data:
                    continue
                source_value = image.filepath_raw or image.filepath
                if source_value and Path(bpy.path.abspath(source_value)).is_file():
                    continue
                removed.append({"material": material.name, "node": node.name, "image": image.name})
                candidate_images.add(image.name)
                material.node_tree.nodes.remove(node)
    for image_name in candidate_images:
        image = bpy.data.images.get(image_name)
        if image and image.users == 0:
            bpy.data.images.remove(image)
    return removed


def collect_object_materials(objects):
    materials = []
    seen = set()
    for obj in objects:
        if obj.type != "MESH" or not obj.data:
            continue
        for material in obj.data.materials:
            if material and material.name not in seen:
                seen.add(material.name)
                materials.append(material)
    return materials


def tag_speedtree_import_materials(
    objects,
    source_fbx_path,
    source_identity_path="",
):
    for material in collect_object_materials(objects):
        material["codex_source_fbx"] = str(source_fbx_path)
        if source_identity_path:
            material["codex_source_identity"] = str(source_identity_path)


def normalized_source_fbx_path(value):
    if not value:
        return ""
    return os.path.normcase(
        os.path.normpath(os.path.abspath(os.path.expanduser(str(value))))
    )


def belongs_to_source_fbx(datablock, source_fbx_path):
    if datablock is None:
        return False
    tagged_path = datablock.get("codex_source_fbx", "")
    return bool(tagged_path) and (
        normalized_source_fbx_path(tagged_path)
        == normalized_source_fbx_path(source_fbx_path)
    )


def belongs_to_source_identity(datablock, source_identity_path):
    if datablock is None or not source_identity_path:
        return False
    tagged_path = datablock.get("codex_source_identity", "")
    return bool(tagged_path) and (
        normalized_source_fbx_path(tagged_path)
        == normalized_source_fbx_path(source_identity_path)
    )


def source_fbx_cleanup_lineage(source_fbx_path):
    """Return canonical FBX plus its retired unprefixed compatibility alias.

    Cluster output names are now canonicalized to ``SK_*``.  Older repair runs
    may still have Blender datablocks tagged with the sibling FBX name without
    that prefix.  The alias is used only by the idempotent cleanup pass and only
    for ``Cluster/fbx`` outputs; normal provenance checks remain exact-path
    checks.
    """
    if not source_fbx_path:
        return set()
    source_path = Path(source_fbx_path)
    paths = {normalized_source_fbx_path(source_path)}
    is_cluster_output = (
        source_path.parent.name.casefold() == "fbx"
        and source_path.parent.parent.name.casefold() == "cluster"
    )
    if (
        is_cluster_output
        and source_path.stem.lower().startswith("sk_")
        and len(source_path.stem) > 3
    ):
        legacy_path = source_path.with_name(
            source_path.stem[3:] + source_path.suffix
        )
        paths.add(normalized_source_fbx_path(legacy_path))
    return paths


def belongs_to_source_fbx_cleanup_lineage(datablock, source_fbx_path):
    if datablock is None:
        return False
    tagged_path = datablock.get("codex_source_fbx", "")
    return bool(tagged_path) and (
        normalized_source_fbx_path(tagged_path)
        in source_fbx_cleanup_lineage(source_fbx_path)
    )


def tag_existing_source_materials(objects):
    for obj in objects:
        source_fbx = obj.get("codex_source_fbx", "") if obj else ""
        if source_fbx:
            tag_speedtree_import_materials([obj], source_fbx)


def strip_speedtree_material_suffixes(objects, suffix="_Mat"):
    # Rename only materials used by this import/build batch. Do not look up and
    # reuse scene materials by target name; if a same-name material already
    # exists, Blender will keep the datablocks separate with a numeric suffix.
    renamed = []
    for material in collect_object_materials(objects):
        old_name = material.name
        duplicate_suffix = ""
        match = re.search(r"(\.\d{3})$", old_name)
        base_name = old_name
        if match:
            duplicate_suffix = match.group(1)
            base_name = old_name[: -len(duplicate_suffix)]
        if not base_name.endswith(suffix):
            continue
        target_name = base_name[: -len(suffix)] + duplicate_suffix
        if not target_name:
            continue
        material.name = target_name
        renamed.append({"old": old_name, "new": material.name})
    return renamed


SPEEDTREE_TEXTURE_ROLES = (
    "color",
    "normal",
    "extra",
    "height",
    "opacity",
    "subsurface",
)
SPEEDTREE_TEXTURE_EXTENSIONS = (".tga", ".png", ".tif", ".tiff", ".exr")
ATLAS_CANONICAL_TEXTURE_STATUS = "canonical_pcg_output"
ATLAS_SOURCE_FALLBACK_STATUS = (
    "source_fallback_needs_pcg_generation"
)
ATLAS_BLENDER_CLUSTER_BAKE_STATUS = "blender_cluster_bake"
ATLAS_PROVISIONAL_RECEIPT_KIND = "speedtree_texture_provisional_receipt"
ATLAS_CLUSTER_RECEIPT_KIND = "blender_cluster_bake_texture_origin_receipt"
STMAT_MAP_INDEX_SPACE = "stmat_xml_map_order_v1"
SOURCE_SPM_MAP_INDEX_SPACE = "source_spm_map_order_v1"
_LEGACY_STMAT_MAP_INDEX_SPACE = "stmat_xml_map_order"
_LEGACY_SOURCE_SPM_MAP_INDEX_SPACE = "source_spm_map_order"
_DEFAULT_ORIGINAL_TEXTURE_ROOT = (
    r"D:\OneDrive\Forestportfolio\Texture"
)


def _configured_original_texture_roots():
    configured = str(
        os.environ.get("SPEEDTREE_ORIGINAL_TEXTURE_ROOTS") or ""
    ).strip()
    values = (
        [value for value in configured.split(os.pathsep) if value.strip()]
        if configured
        else [_DEFAULT_ORIGINAL_TEXTURE_ROOT]
    )
    return tuple(Path(value).expanduser().resolve() for value in values)


SPEEDTREE_ORIGINAL_TEXTURE_ROOTS = _configured_original_texture_roots()
ATLAS_BLOCKED_TEXTURE_PATH_PARTS = {
    ".sk_batch_isolated_bark",
    "_sk_batch_isolated_bark",
    ".sk_batch_temp",
    "_sk_batch_temp",
    ".sk_batch_cache",
    "_sk_batch_cache",
    ".speedtree_export_cache",
    "_speedtree_export_cache",
    ".speedtree_export",
    "_speedtree_export",
    "_pcgtex_generated",
    "_pcgtex_backups",
}


def _runtime_tolerant_texture_contract(texture_contract):
    return bool(
        isinstance(texture_contract, dict)
        and texture_contract.get(
            handoff_contract.TEXTURE_CONTRACT_MODE_FIELD
        ) == handoff_contract.RUNTIME_TOLERANT_TEXTURE_MODE
    )


def _bat_runtime_texture_contract(texture_contract):
    """Make the operational BAT boundary tolerant without weakening audits."""
    result = dict(texture_contract or {})
    source_status = str(result.get("status") or "").strip()
    if source_status and source_status != "ok":
        result.setdefault("source_texture_status", source_status)
    result["status"] = "ok"
    result.setdefault("bindings", [])
    result.setdefault(
        "texture_outcome",
        "complete" if result["bindings"] else "unassigned",
    )
    result[handoff_contract.TEXTURE_CONTRACT_MODE_FIELD] = (
        handoff_contract.RUNTIME_TOLERANT_TEXTURE_MODE
    )
    return result


def load_speedtree_runtime_texture_contract(
    path, *, spm_path="", source_fbx_path=""
):
    """Load operational handoff metadata without admitting texture gates.

    A parsed new pipeline envelope still receives full structural/live source
    validation. Missing, unreadable, malformed, or incomplete legacy
    texture-only metadata becomes an empty runtime contract.
    """
    if not path:
        return _bat_runtime_texture_contract(None)
    contract_path = Path(path)
    diagnostic = None
    try:
        payload = json.loads(contract_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        payload = None
        diagnostic = _texture_diagnostic(
            "",
            "texture_contract_unavailable",
            f"Texture metadata unavailable; continuing without bindings: {exc}",
        )

    if (
        isinstance(payload, dict)
        and handoff_contract.PIPELINE_ENVELOPE_FIELD in payload
    ):
        return _bat_runtime_texture_contract(
            load_speedtree_texture_readiness_contract(
                path,
                spm_path=spm_path,
                source_fbx_path=source_fbx_path,
            )
        )

    if isinstance(payload, dict):
        legacy = payload.get("texture_readiness_contract", payload)
        if isinstance(legacy, dict) and isinstance(
            legacy.get("bindings"), list
        ):
            result = _bat_runtime_texture_contract(legacy)
            result["contract_path"] = str(contract_path.resolve())
            return result
        diagnostic = _texture_diagnostic(
            "",
            "texture_contract_unassigned",
            "Legacy texture metadata has no bindings; continuing unassigned",
        )

    result = _bat_runtime_texture_contract(None)
    result["contract_path"] = str(contract_path.resolve())
    result["texture_diagnostics"] = [diagnostic] if diagnostic else []
    result["texture_warnings"] = []
    result["texture_outcome"] = "unassigned"
    return result


def _texture_diagnostic(
    material,
    code,
    message,
    *,
    severity="info",
    **details,
):
    row = {
        "code": str(code),
        "severity": str(severity),
        "material": str(getattr(material, "name", material) or ""),
        "message": str(message),
    }
    row.update(details)
    return row


def _quarantine_texture_binding(
    binding,
    material,
    code,
    message,
    *,
    severity="info",
    missing_roles=None,
    allow_local_search=True,
):
    """Return a diagnostic-only binding that cannot publish any path."""
    result = dict(binding or {})
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
        result.pop(field, None)
    result.update(
        {
            "material": getattr(material, "name", str(material or "")),
            "status": "unassigned",
            "texture_source_mode": "unresolved",
            "binding_disposition": "leave_unassigned",
            "files": {},
            "available_roles": [],
            "missing_roles": sorted(
                set(missing_roles or SPEEDTREE_TEXTURE_ROLES)
            ),
            "warning_codes": [str(code)] if severity == "warning" else [],
            "diagnostic_codes": [str(code)],
            # Quarantine rejects only the unsafe authority row. It must not
            # suppress an independent exact STMAT/local lookup in runtime
            # mode; that lookup can still bind a safe subset.
            "allow_local_search": bool(allow_local_search),
        }
    )
    return result, _texture_diagnostic(
        material,
        code,
        message,
        severity=severity,
    )


def _has_authoritative_fallback_evidence(binding):
    if not isinstance(binding, dict):
        return False
    evidence = str(binding.get("source_evidence") or "").strip()
    if evidence == "authoritative_global_original_root":
        return True
    return bool(
        str(binding.get("manifest_path") or "").strip()
        and evidence
    )


def _speedtree_texture_set_key(value):
    """Canonical comparison key shared by M_ materials and T_ output sets."""
    value = re.sub(r"(\.\d{3})$", "", str(value or "").strip())
    if value[:2].lower() in {"m_", "t_"}:
        value = value[2:]
    return re.sub(r"[^a-z0-9]+", "", value.lower())


def _speedtree_texture_dir(source_fbx_path):
    source = Path(source_fbx_path).resolve()
    if source.parent.name.lower() == "fbx":
        return source.parent.parent / "texture"
    return source.parent / "texture"


def _speedtree_asset_root(source_fbx_path):
    source = Path(source_fbx_path).resolve()
    return source.parent.parent if source.parent.name.lower() == "fbx" else source.parent


def _atlas_manifest_asset_root(manifest_path, manifest=None):
    manifest_path = Path(manifest_path).resolve()
    explicit = str((manifest or {}).get("asset_root") or "").strip()
    if explicit:
        root = Path(explicit).expanduser()
        if not root.is_absolute():
            root = manifest_path.parent / root
        return root.resolve()
    if manifest_path.parent.name.casefold() in {
        ".atlas_leaf_speedtree_scopes",
        ".atlas_leaf_speedtree_targets",
    }:
        return manifest_path.parent.parent
    return manifest_path.parent


def _speedtree_material_name_key(value):
    value = re.sub(r"(\.\d{3})$", "", str(value or "").strip())
    if value.lower().endswith("_mat"):
        value = value[:-4]
    return re.sub(r"[^a-z0-9]+", "", value.lower())


def _fallback_production_group_parts(value):
    """Mirror the shared numeric-boundary rule for standalone installs."""
    name = re.sub(r"(\.\d{3})$", "", str(value or "").strip())
    if name.casefold().endswith("_mat"):
        name = name[:-4]
    numeric_segments = list(
        re.finditer(r"(?<![A-Za-z0-9])\d+(?![A-Za-z0-9])", name)
    )
    if not numeric_segments:
        return name, ""
    boundary = numeric_segments[-1]
    suffix_match = re.fullmatch(r"[_. -]+(.+)", name[boundary.end() :])
    if not suffix_match:
        return name, ""
    suffix = suffix_match.group(1).strip().casefold()
    return (name[: boundary.end()], suffix) if suffix else (name, "")


def _production_group_parts(value):
    try:
        tokens = handoff_contract.production_group_tokens(value)
        base = handoff_contract.production_group_base_name(value)
        return base, tokens[0] if tokens else ""
    except RuntimeError:
        return _fallback_production_group_parts(value)


def _speedtree_stmat_materials(source_fbx_path):
    """Read authoritative material provenance from SpeedTree's FBX sidecar."""
    stmat_path = Path(source_fbx_path).resolve().with_suffix(".stmat")
    result = {"path": str(stmat_path), "materials": {}, "error": ""}
    if not stmat_path.is_file():
        return result
    try:
        root = ET.parse(stmat_path).getroot()
    except (OSError, ET.ParseError) as exc:
        result["error"] = str(exc)
        return result

    for material_index, node in enumerate(
        root.findall(".//Material"),
        start=1,
    ):
        name = str(node.attrib.get("Name") or "").strip()
        if not name:
            continue
        source_paths = []
        source_maps = {}
        source_slots = []
        for map_index, map_node in enumerate(node.findall("./Map")):
            source = str(map_node.attrib.get("Source") or "").strip()
            if source:
                source_paths.append(source)
                map_name = str(
                    map_node.attrib.get("Name") or ""
                ).strip()
                source_slots.append({
                    "map_index": map_index,
                    "map": map_name,
                    "source": source,
                })
                if map_name:
                    source_maps[map_name.casefold()] = source
        user_data = {}
        raw_user_data = str(node.attrib.get("UserData") or "").strip()
        if raw_user_data:
            try:
                parsed = json.loads(raw_user_data)
                if isinstance(parsed, dict):
                    user_data = parsed
            except json.JSONDecodeError:
                pass
        result["materials"][_speedtree_material_name_key(name)] = {
            "name": name,
            "material_index": material_index,
            "material_id": str(
                user_data.get("stmat_material_id")
                or user_data.get("material_id")
                or node.attrib.get("ID")
                or material_index
            ),
            "source_paths": source_paths,
            "source_maps": source_maps,
            "source_slots": source_slots,
            "user_data": user_data,
        }
    return result


def _resolved_speedtree_source_path(source_fbx_path, value):
    path = Path(str(value or "")).expanduser()
    if not path.is_absolute():
        path = Path(source_fbx_path).resolve().parent / path
    return path.resolve()


def _path_is_under(path, root):
    try:
        Path(path).resolve().relative_to(Path(root).resolve())
        return True
    except (OSError, ValueError):
        return False


def _path_identity(value):
    return os.path.normcase(str(Path(value).expanduser().resolve())).casefold()


def _source_fbx_expected_spm(source_fbx_path):
    source = Path(source_fbx_path).expanduser().resolve()
    asset_root = _speedtree_asset_root(source)
    return asset_root / f"{source.stem}.spm"


def _texture_semantic_role(value):
    role = re.sub(r"[^a-z0-9]+", "", str(value or "").casefold())
    aliases = {
        "albedo": "color",
        "basecolor": "color",
        "diffuse": "color",
        "colour": "color",
        "color": "color",
        "alpha": "opacity",
        "opacity": "opacity",
        "transparency": "opacity",
        "mask": "opacity",
        "normal": "normal",
        "rough": "gloss",
        "roughness": "gloss",
        "gloss": "gloss",
        "ambientocclusion": "ao",
        "ao": "ao",
        "height": "height",
        "displacement": "height",
        "subsurface": "subsurfacecolor",
        "subsurfacecolor": "subsurfacecolor",
        "translucency": "subsurfacecolor",
        "transmission": "subsurfacecolor",
        "subsurfaceamount": "subsurfaceamount",
    }
    return aliases.get(role, role)


def _atlas_provisional_source_role(value):
    """Normalize Atlas inputs without collapsing distinct PBR source maps."""
    role = re.sub(r"[^a-z0-9]+", "", str(value or "").casefold())
    if role in {"rough", "roughness"}:
        return "roughness"
    return _texture_semantic_role(role)


def _file_sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _speedtree_preserved_cluster_sources(
    source_fbx_path,
    material,
    stmat_data=None,
    expected_binding=None,
):
    """Classify one exact STMAT material as a Blender Cluster bake.

    The classifier proves the current material name/id and every source-bearing
    STMAT map slot against one physical-capture manifest.  Directory and file
    naming never establish provenance by themselves.
    """
    stmat_data = stmat_data or _speedtree_stmat_materials(source_fbx_path)
    stmat_material = stmat_data.get("materials", {}).get(
        _speedtree_material_name_key(material.name), {}
    )
    if not stmat_material:
        return None
    raw_source_maps = stmat_material.get("source_maps") or {}
    if (
        not raw_source_maps
        or len(stmat_material.get("source_paths") or [])
        != len(raw_source_maps)
    ):
        return None
    source_maps = {}
    for raw_role, raw_path in raw_source_maps.items():
        role = _texture_semantic_role(raw_role)
        if role in source_maps and str(source_maps[role]) != str(raw_path):
            return None
        source_maps[role] = raw_path

    cluster_root = _speedtree_asset_root(source_fbx_path) / "cluster"
    resolved = {}
    for map_name, value in source_maps.items():
        try:
            path = _resolved_speedtree_source_path(source_fbx_path, value)
        except (OSError, ValueError):
            return None
        try:
            valid_source = (
                _path_is_under(path, cluster_root)
                and path.is_file()
                and path.stat().st_size > 0
            )
        except OSError:
            valid_source = False
        if not valid_source:
            return None
        resolved[map_name] = str(path)
    slot_identity = {}
    for row in stmat_material.get("source_slots") or []:
        role = _texture_semantic_role(row.get("map"))
        if not role or role in slot_identity:
            return None
        try:
            slot_path = _resolved_speedtree_source_path(
                source_fbx_path,
                row.get("source"),
            )
        except (OSError, ValueError):
            return None
        if (
            role not in resolved
            or _path_identity(slot_path)
            != _path_identity(resolved[role])
        ):
            return None
        slot_identity[role] = {
            "map_index": int(row.get("map_index") or 0),
            "map": str(row.get("map") or ""),
        }
    if set(slot_identity) != set(resolved):
        return None
    expected_receipt = (
        (expected_binding or {}).get("origin_receipt")
        if isinstance(expected_binding, dict)
        else None
    )
    if expected_receipt is not None:
        if (
            not isinstance(expected_receipt, dict)
            or expected_receipt.get("kind")
            != ATLAS_CLUSTER_RECEIPT_KIND
            or expected_receipt.get("source_origin")
            != ATLAS_BLENDER_CLUSTER_BAKE_STATUS
        ):
            return None
        if receipt_declares_preview_fallback(expected_receipt):
            try:
                validate_preview_receipt(
                    expected_receipt,
                    requested_usage=PREVIEW_ONLY_USAGE,
                )
            except (TypeError, ValueError):
                return None
        elif expected_receipt.get(RECEIPT_CAPABILITIES_FIELD):
            return None
        elif str(expected_receipt.get("version") or "") != "1":
            return None
    expected_index_space = str(
        (expected_receipt or {}).get("slot_index_space")
        or (expected_receipt or {}).get("map_index_space")
        or ""
    ).strip()
    if expected_index_space not in {
        "",
        STMAT_MAP_INDEX_SPACE,
        SOURCE_SPM_MAP_INDEX_SPACE,
        _LEGACY_STMAT_MAP_INDEX_SPACE,
        _LEGACY_SOURCE_SPM_MAP_INDEX_SPACE,
    }:
        return None
    enforce_stmat_map_index = (
        expected_index_space
        in {STMAT_MAP_INDEX_SPACE, _LEGACY_STMAT_MAP_INDEX_SPACE}
    )

    candidate_paths = set()
    expected_manifest = str(
        (expected_receipt or {}).get("physical_capture_manifest")
        or (expected_binding or {}).get("physical_capture_manifest")
        or ""
    ).strip()
    if expected_manifest:
        expected_manifest_path = Path(
            expected_manifest
        ).expanduser().resolve()
        if not _path_is_under(expected_manifest_path, cluster_root):
            return None
        candidate_paths.add(expected_manifest_path)
    else:
        try:
            candidate_paths.update(
                path.resolve()
                for path in cluster_root.glob(
                    "*_auto_capture_manifest.json"
                )
            )
        except OSError:
            return None

    stmat_material_name = str(
        stmat_material.get("name") or material.name
    ).strip()
    stmat_material_id = str(
        stmat_material.get("material_id") or ""
    ).strip()
    material_key = _speedtree_material_name_key(material.name)
    proofs = []
    for manifest_path in sorted(candidate_paths, key=lambda path: str(path).casefold()):
        if (
            not manifest_path.is_file()
            or not _path_is_under(manifest_path, cluster_root)
        ):
            continue
        try:
            payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            continue
        if (
            not isinstance(payload, dict)
            or payload.get("workflow_mode") != "PHYSICAL_DIRECT_CAPTURE"
            or payload.get("direct_uv_source")
            != "same_blender_physical_capture_projection"
        ):
            continue
        capture_hash = str(
            payload.get("physical_capture_contract_sha256")
            or (payload.get("physical_capture_contract") or {}).get(
                "contract_sha256"
            )
            or ""
        ).strip().casefold()
        nested_hash = str(
            (payload.get("physical_capture_contract") or {}).get(
                "contract_sha256"
            )
            or ""
        ).strip().casefold()
        if (
            not re.fullmatch(r"[0-9a-f]{64}", capture_hash)
            or (nested_hash and nested_hash != capture_hash)
        ):
            continue
        if expected_receipt is not None and str(
            expected_receipt.get(
                "physical_capture_contract_sha256"
            )
            or ""
        ).strip().casefold() != capture_hash:
            continue

        declared_material = str(
            (expected_receipt or {}).get("material_name")
            or (expected_receipt or {}).get("material")
            or payload.get("material_name")
            or payload.get("material")
            or ""
        ).strip()
        if (
            not declared_material
            or _speedtree_material_name_key(declared_material)
            != material_key
            or _speedtree_material_name_key(stmat_material_name)
            != material_key
        ):
            continue
        declared_material_ids = {
            str(value).strip()
            for value in (
                (expected_receipt or {}).get("material_id"),
                (expected_receipt or {}).get("source_material_id"),
                (expected_binding or {}).get("stmat_material_id"),
                (expected_binding or {}).get("material_id"),
                payload.get("material_id"),
                payload.get("source_material_id"),
            )
            if str(value or "").strip()
        }
        if declared_material_ids and (
            not stmat_material_id
            or declared_material_ids != {stmat_material_id}
        ):
            continue

        declared = {}
        declared_rows = []
        valid_rows = True
        for row in payload.get("maps") or []:
            if (
                not isinstance(row, dict)
                or not row.get("path")
                or not row.get("role")
            ):
                continue
            raw_role = re.sub(
                r"[^a-z0-9]+",
                "",
                str(row.get("role") or "").casefold(),
            )
            role = _texture_semantic_role(row.get("role"))
            path = Path(str(row.get("path"))).expanduser()
            if not path.is_absolute():
                path = manifest_path.parent / path
            path = path.resolve()
            sha256 = str(row.get("sha256") or "").strip().casefold()
            try:
                exact_file = (
                    _path_is_under(path, cluster_root)
                    and path.is_file()
                    and path.stat().st_size > 0
                    and re.fullmatch(r"[0-9a-f]{64}", sha256)
                    and _file_sha256(path).casefold() == sha256
                )
            except OSError:
                exact_file = False
            if not exact_file or (
                role in declared
                and _path_identity(declared[role]["path"])
                != _path_identity(path)
            ):
                valid_rows = False
                break
            declared_row = {
                "role": role,
                "raw_role": raw_role,
                "path": str(path),
                "sha256": sha256,
            }
            declared[role] = declared_row
            declared_rows.append(declared_row)
        if not valid_rows:
            continue

        slot_files = []
        preview_role_fallbacks = []
        for role, path in sorted(resolved.items()):
            declared_row = declared.get(role)
            if (
                declared_row is None
                or _path_identity(declared_row["path"])
                != _path_identity(path)
            ):
                # Exact semantic-role equality remains the default.  This
                # helper recognizes only the receipt-recorded preview case.
                if (
                    expected_receipt is None
                    or not receipt_declares_preview_fallback(
                        expected_receipt
                    )
                ):
                    break
                selected_rows = [
                    row
                    for row in declared_rows
                    if _path_identity(row["path"])
                    == _path_identity(path)
                ]
                fallback = build_preview_role_fallback(
                    slot_role=role,
                    slot_path=path,
                    selected_rows=selected_rows,
                    material_id=stmat_material_id,
                    # SpeedTree SPM and STMAT names differ by the exported
                    # ``_Mat`` suffix.  The normalized key comparison above
                    # proves they are the same material; keep the producer's
                    # exact parent-receipt spelling for cross-reader identity.
                    material_name=declared_material,
                    contract_hash=capture_hash,
                    map_index=slot_identity[role]["map_index"],
                    map_name=slot_identity[role]["map"],
                    workflow_mode=payload.get("workflow_mode"),
                    direct_uv_source=payload.get("direct_uv_source"),
                )
                if fallback is None:
                    break
                declared_row = declared[fallback["manifest_role"]]
                preview_role_fallbacks.append(fallback)
            slot_files.append({
                "map_index": slot_identity[role]["map_index"],
                "stmat_map_index": slot_identity[role]["map_index"],
                "map": slot_identity[role]["map"],
                "capture_role": role,
                "path": str(Path(path).resolve()),
                "sha256": declared_row["sha256"],
            })
        else:
            expected_slots = (
                (expected_receipt or {}).get("slot_files")
                or (expected_receipt or {}).get("capture_maps")
                or []
            )
            if expected_slots:
                expected_by_role = {}
                for row in expected_slots:
                    if not isinstance(row, dict):
                        valid_rows = False
                        break
                    role = _texture_semantic_role(
                        row.get("capture_role")
                        or row.get("map")
                        or row.get("role")
                    )
                    path = str(row.get("path") or "").strip()
                    if not role or not path or role in expected_by_role:
                        valid_rows = False
                        break
                    expected_by_role[role] = row
                if not valid_rows:
                    continue
                for slot in slot_files:
                    row = expected_by_role.get(slot["capture_role"])
                    expected_stmat_index = (
                        row.get("stmat_map_index")
                        if isinstance(row, dict)
                        and row.get("stmat_map_index") is not None
                        else (
                            row.get("map_index")
                            if isinstance(row, dict)
                            else None
                        )
                    )
                    stmat_index_mismatch = False
                    if enforce_stmat_map_index:
                        try:
                            stmat_index_mismatch = (
                                expected_stmat_index is None
                                or int(expected_stmat_index)
                                != slot["stmat_map_index"]
                            )
                        except (TypeError, ValueError):
                            stmat_index_mismatch = True
                    if (
                        row is None
                        or stmat_index_mismatch
                        or (
                            str(row.get("map") or "").strip()
                            and str(row.get("map")).strip()
                            != slot["map"]
                        )
                        or _path_identity(row["path"])
                        != _path_identity(slot["path"])
                        or (
                            str(row.get("sha256") or "").strip()
                            and str(row.get("sha256")).strip().casefold()
                            != slot["sha256"]
                        )
                    ):
                        valid_rows = False
                        break
                if not valid_rows:
                    continue
            if (
                expected_receipt is not None
                and PREVIEW_ROLE_FALLBACKS_FIELD in expected_receipt
            ):
                expected_fallbacks = expected_receipt.get(
                    PREVIEW_ROLE_FALLBACKS_FIELD
                )
                if expected_index_space in {
                    SOURCE_SPM_MAP_INDEX_SPACE,
                    _LEGACY_SOURCE_SPM_MAP_INDEX_SPACE,
                }:
                    if not isinstance(expected_fallbacks, list):
                        continue
                    expected_fallbacks = [
                        dict(row)
                        for row in expected_fallbacks
                        if isinstance(row, dict)
                    ]
                    if len(expected_fallbacks) != len(
                        expected_receipt.get(
                            PREVIEW_ROLE_FALLBACKS_FIELD
                        )
                    ):
                        continue
                    valid_fallback_indexes = True
                    for row in expected_fallbacks:
                        matching_slots = []
                        for slot in expected_slots:
                            if not isinstance(slot, dict):
                                continue
                            slot_role = _texture_semantic_role(
                                slot.get("capture_role")
                                or slot.get("map")
                                or slot.get("role")
                            )
                            try:
                                source_index_match = int(
                                    slot.get("map_index")
                                ) == int(row.get("map_index"))
                            except (TypeError, ValueError):
                                source_index_match = False
                            if (
                                slot_role == row.get("slot_role")
                                and source_index_match
                                and str(slot.get("map") or "").strip()
                                == str(row.get("map") or "").strip()
                                and _path_identity(slot.get("path"))
                                == _path_identity(row.get("path"))
                                and str(
                                    slot.get("sha256") or ""
                                ).strip().casefold()
                                == str(
                                    row.get("sha256") or ""
                                ).strip().casefold()
                            ):
                                matching_slots.append(slot)
                        if len(matching_slots) != 1:
                            valid_fallback_indexes = False
                            break
                        live_stmat_slot = slot_identity.get(
                            row.get("slot_role")
                        )
                        if live_stmat_slot is None:
                            valid_fallback_indexes = False
                            break
                        try:
                            live_stmat_index = int(
                                live_stmat_slot["map_index"]
                            )
                            declared_stmat_index = matching_slots[0].get(
                                "stmat_map_index"
                            )
                            if (
                                declared_stmat_index is not None
                                and int(declared_stmat_index)
                                != live_stmat_index
                            ):
                                raise ValueError(
                                    "stmat_map_index mismatch"
                                )
                            row["map_index"] = live_stmat_index
                        except (KeyError, TypeError, ValueError):
                            valid_fallback_indexes = False
                            break
                    if not valid_fallback_indexes:
                        continue
                expected_fallback_signature = (
                    preview_role_fallbacks_signature(
                        expected_fallbacks
                    )
                )
                current_fallback_signature = (
                    preview_role_fallbacks_signature(
                        preview_role_fallbacks
                    )
                )
                if (
                    expected_fallback_signature is None
                    or expected_fallback_signature
                    != current_fallback_signature
                ):
                    continue
            if expected_slots:
                for slot in slot_files:
                    expected_slot = expected_by_role.get(
                        slot["capture_role"]
                    )
                    declared_spm_index = (
                        expected_slot.get("spm_map_index")
                        if isinstance(expected_slot, dict)
                        else None
                    )
                    if declared_spm_index is None:
                        continue
                    try:
                        declared_spm_index = int(declared_spm_index)
                    except (TypeError, ValueError):
                        valid_rows = False
                        break
                    if declared_spm_index < 0:
                        valid_rows = False
                        break
                    slot["spm_map_index"] = declared_spm_index
                if not valid_rows:
                    continue
            proofs.append({
                "manifest_path": manifest_path,
                "capture_hash": capture_hash,
                "receipt_material_name": declared_material,
                "slot_files": slot_files,
                PREVIEW_ROLE_FALLBACKS_FIELD: preview_role_fallbacks,
            })

    signatures = {
        (
            proof["capture_hash"],
            tuple(
                (
                    row["map_index"],
                    row["map"],
                    row["capture_role"],
                    _path_identity(row["path"]),
                    row["sha256"],
                )
                for row in proof["slot_files"]
            ),
            preview_role_fallbacks_signature(
                proof[PREVIEW_ROLE_FALLBACKS_FIELD]
            ),
        )
        for proof in proofs
    }
    if len(signatures) != 1:
        return None
    proof = proofs[0]
    origin_receipt = {
        "kind": ATLAS_CLUSTER_RECEIPT_KIND,
        "version": 1,
        "source_origin": ATLAS_BLENDER_CLUSTER_BAKE_STATUS,
        "source_spm": str(_source_fbx_expected_spm(source_fbx_path)),
        "material_id": stmat_material_id,
        "material_name": (
            proof["receipt_material_name"]
            if proof[PREVIEW_ROLE_FALLBACKS_FIELD]
            else material.name
        ),
        "stmat_material_name": stmat_material_name,
        "slot_index_space": STMAT_MAP_INDEX_SPACE,
        "physical_capture_manifest": str(
            proof["manifest_path"].resolve()
        ),
        "physical_capture_contract_sha256": proof["capture_hash"],
        "source_refs": [
            row["path"] for row in proof["slot_files"]
        ],
        "slot_files": proof["slot_files"],
    }
    if proof[PREVIEW_ROLE_FALLBACKS_FIELD]:
        origin_receipt[PREVIEW_ROLE_FALLBACKS_FIELD] = list(
            proof[PREVIEW_ROLE_FALLBACKS_FIELD]
        )
        try:
            origin_receipt = finalize_preview_receipt(origin_receipt)
            validate_preview_receipt(
                origin_receipt,
                requested_usage=PREVIEW_ONLY_USAGE,
            )
        except (TypeError, ValueError):
            return None
        if int(origin_receipt.get("version") or 0) != (
            PREVIEW_RECEIPT_VERSION
        ):
            return None
        manifest_path = Path(
            origin_receipt["physical_capture_manifest"]
        )
        if (
            not manifest_path.is_file()
            or not _path_is_under(manifest_path, cluster_root)
        ):
            return None
        slots_by_role = {
            row["capture_role"]: row
            for row in origin_receipt["slot_files"]
        }
        for row in origin_receipt[PREVIEW_ROLE_FALLBACKS_FIELD]:
            selected_path = Path(row["path"]).expanduser().resolve()
            selected_slot = slots_by_role.get(row["slot_role"])
            try:
                live_match = (
                    _path_is_under(selected_path, cluster_root)
                    and selected_path.is_file()
                    and selected_path.stat().st_size > 0
                    and _file_sha256(selected_path).casefold()
                    == row["sha256"]
                )
            except OSError:
                live_match = False
            if (
                not live_match
                or selected_slot is None
                or _path_identity(selected_slot["path"])
                != _path_identity(selected_path)
                or selected_slot["sha256"] != row["sha256"]
                or row["material_id"] != stmat_material_id
                or row["material_name"]
                != origin_receipt["material_name"]
                or row["contract_hash"] != proof["capture_hash"]
            ):
                return None
    return {
        "cluster_root": str(cluster_root.resolve()),
        "source_maps": resolved,
        "preserved_files": resolved,
        "origin_kind": ATLAS_BLENDER_CLUSTER_BAKE_STATUS,
        "origin_receipt": origin_receipt,
        PREVIEW_ROLE_FALLBACKS_FIELD: list(
            proof[PREVIEW_ROLE_FALLBACKS_FIELD]
        ),
    }


def _speedtree_preserved_declared_sources(
    source_fbx_path, material, stmat_data=None
):
    """Verify non-managed STMAT sources without guessing a managed T_ set."""
    stmat_data = stmat_data or _speedtree_stmat_materials(source_fbx_path)
    materials = stmat_data.get("materials", {})
    material_key = _speedtree_material_name_key(material.name)
    if material_key not in materials:
        return None
    stmat_material = materials[material_key]
    source_paths = stmat_material.get("source_paths") or []
    if not source_paths:
        # A declared STMAT material can intentionally consist only of constant
        # map values (Color/Gloss/etc.).  Its empty Source set is a complete
        # declaration, not a stale file reference.  The exact STMAT name is
        # still required above, so an unmatched Blender placeholder does not
        # receive this exemption.
        return {
            "declared_sources": [],
            "source_free": True,
        }
    resolved = []
    for value in source_paths:
        try:
            path = _resolved_speedtree_source_path(source_fbx_path, value)
            ready = path.is_file() and path.stat().st_size > 0
        except (OSError, ValueError):
            ready = False
        if not ready:
            return None
        resolved.append(str(path))
    return {"declared_sources": sorted(set(resolved), key=str.casefold)}


def _speedtree_manifest_paths(source_fbx_path, stmat_material=None):
    root = _speedtree_asset_root(source_fbx_path)
    paths = []
    user_data = (stmat_material or {}).get("user_data") or {}
    scope = str(user_data.get("scope") or "").strip()
    if scope:
        safe_scope = re.sub(r"[^A-Za-z0-9_.-]+", "_", scope).strip("._") or "AtlasLeaf"
        paths.append(root / ".atlas_leaf_speedtree_scopes" / f"{safe_scope}.json")
    paths.append(root / "speedtree_import_manifest.json")
    unique = []
    seen = set()
    for path in paths:
        key = os.path.normcase(str(path))
        if key not in seen:
            seen.add(key)
            unique.append(path)
    return unique


def _speedtree_consumer_scoped_manifest_path(
    source_fbx_path, manifest
):
    scope = str((manifest or {}).get("export_scope_id") or "").strip()
    if not scope:
        return None
    safe_scope = (
        re.sub(r"[^A-Za-z0-9_.-]+", "_", scope).strip("._")
        or "AtlasLeaf"
    )
    consumer = Path(source_fbx_path).stem
    if not consumer:
        return None
    return (
        _speedtree_asset_root(source_fbx_path)
        / ".atlas_leaf_speedtree_scopes"
        / f"{safe_scope}__{consumer}.json"
    )


def _load_speedtree_import_manifest(path):
    path = Path(path)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(
            f"Authoritative SpeedTree import manifest is unreadable or "
            f"malformed: {path}: {exc}"
        ) from exc
    if not isinstance(data, dict):
        raise RuntimeError(
            "Authoritative SpeedTree import manifest must contain a JSON "
            f"object: {path}"
        )
    return data


def _manifest_texture_entries(manifest):
    """Return de-duplicated per-material Atlas texture contracts."""
    status = str(manifest.get("texture_contract_status") or "").strip()
    if not status:
        return []
    status_fields = {
        ATLAS_CANONICAL_TEXTURE_STATUS: (
            "canonical_texture_outputs",
            "canonical_texture_output",
            "files",
        ),
        ATLAS_SOURCE_FALLBACK_STATUS: (
            "source_texture_fallbacks",
            "source_texture_fallback",
            "source_paths",
        ),
        ATLAS_BLENDER_CLUSTER_BAKE_STATUS: (
            "blender_cluster_bake_textures",
            "blender_cluster_bake_texture",
            "files",
        ),
    }
    if status not in status_fields:
        raise RuntimeError(
            "Atlas import manifest has an unknown texture_contract_status: "
            + repr(status)
        )

    top_level_field, nested_field, payload_field = status_fields[status]
    rows = []
    rows.extend(manifest.get(top_level_field) or [])
    for group in manifest.get("material_groups") or []:
        if not isinstance(group, dict):
            continue
        group_status = str(
            group.get("texture_contract_status") or status
        ).strip()
        if group_status != status:
            raise RuntimeError(
                "Atlas import manifest mixes incompatible texture contract "
                "states"
            )
        nested = group.get(nested_field)
        if isinstance(nested, dict):
            row = dict(nested)
            row.setdefault("material_name", group.get("material"))
            row.setdefault("material", group.get("material"))
            rows.append(row)

    normalized = []
    seen = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        material_name = str(
            row.get("material_name") or row.get("material") or ""
        ).strip()
        payload = row.get(payload_field)
        signature = json.dumps(
            {
                "material": _speedtree_material_name_key(material_name),
                "payload": payload or {},
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        if signature in seen:
            continue
        seen.add(signature)
        normalized.append(
            {
                "status": status,
                "material_name": material_name,
                "contract": dict(row),
            }
        )
    if not normalized:
        raise RuntimeError(
            "Atlas import manifest declares a texture contract but contains "
            "no per-material mappings"
        )
    return normalized


def _blocked_atlas_texture_path(path):
    parts = [part.casefold() for part in Path(path).parts]
    blocked = {
        part for part in parts
        if (
            part in ATLAS_BLOCKED_TEXTURE_PATH_PARTS
            or part.startswith(".sk_batch_")
            or part.startswith("_sk_batch_")
        )
    }
    return sorted(blocked)


def _manifest_path_value(value, manifest_path):
    path = Path(str(value or "")).expanduser()
    if not path.is_absolute():
        path = Path(manifest_path).parent / path
    return path.resolve()


def _validate_atlas_target_identity(
    contract,
    manifest_path,
    source_fbx_path,
    diagnostics,
):
    asset_root = _atlas_manifest_asset_root(manifest_path, contract)
    target_value = str(
        contract.get("target_spm")
        or contract.get("spm")
        or ""
    ).strip()
    if not target_value:
        diagnostics.append("target_spm=missing")
        return
    target_spm = _manifest_path_value(target_value, manifest_path)
    if not _path_is_under(target_spm, asset_root):
        diagnostics.append(
            f"target_spm={target_spm}, reason=outside_manifest_asset"
        )
    if (
        source_fbx_path
        and target_spm.name.casefold()
        != _source_fbx_expected_spm(source_fbx_path).name.casefold()
    ):
        diagnostics.append(
            f"target_spm={target_spm}, "
            f"expected_name={_source_fbx_expected_spm(source_fbx_path).name}, "
            "reason=fbx_spm_identity_mismatch"
        )


def _validate_atlas_canonical_entry(
    entry,
    manifest_path,
    source_fbx_path=None,
):
    contract = entry["contract"]
    if any(
        receipt_declares_preview_fallback(candidate)
        for candidate in (
            contract,
            contract.get("origin_receipt"),
        )
    ):
        raise RuntimeError(
            "Atlas canonical texture mapping rejects preview-only receipt "
            "capabilities"
        )
    texture_base = str(contract.get("texture_base") or "").strip()
    if not texture_base.casefold().startswith("t_"):
        raise RuntimeError(
            "Atlas canonical texture_base must start with T_: "
            + repr(texture_base)
        )
    files = contract.get("files")
    if not isinstance(files, dict):
        files = {}
    texture_root_value = str(contract.get("texture_root") or "").strip()
    texture_root = (
        _manifest_path_value(texture_root_value, manifest_path)
        if texture_root_value
        else _atlas_manifest_asset_root(manifest_path, contract) / "texture"
    )
    normalized = {}
    diagnostics = []
    _validate_atlas_target_identity(
        contract,
        manifest_path,
        source_fbx_path,
        diagnostics,
    )
    for role in SPEEDTREE_TEXTURE_ROLES:
        value = str(files.get(role) or "").strip()
        expected = texture_root / f"{texture_base}_{role}.tga"
        if not value:
            diagnostics.append(
                f"role={role}, expected={expected}, reason=missing_mapping"
            )
            continue
        path = _manifest_path_value(value, manifest_path)
        blocked = _blocked_atlas_texture_path(path)
        try:
            under_root = _path_is_under(path, texture_root)
            ready = path.is_file() and path.stat().st_size > 0
        except OSError:
            under_root = False
            ready = False
        if blocked:
            diagnostics.append(
                f"role={role}, path={path}, blocked={','.join(blocked)}"
            )
        elif not under_root:
            diagnostics.append(
                f"role={role}, path={path}, reason=outside_texture_root"
            )
        elif not path.name.casefold().startswith("t_"):
            diagnostics.append(
                f"role={role}, path={path}, reason=not_T_output"
            )
        elif path.stem.casefold() != (
            f"{texture_base}_{role}".casefold()
        ):
            diagnostics.append(
                f"role={role}, path={path}, expected={expected}, "
                "reason=role_filename_mismatch"
            )
        elif not ready:
            diagnostics.append(
                f"role={role}, path={path}, reason=missing_or_empty"
            )
        else:
            normalized[role] = path
    if diagnostics:
        raise RuntimeError(
            "Atlas canonical texture mapping is invalid for material="
            f"{entry['material_name'] or '<unnamed>'}: "
            + " | ".join(diagnostics)
            + ". Generate/export the missing T_* maps in PCG ST9 Texture."
        )
    return {
        "material": entry["material_name"],
        "status": "ok",
        "texture_source_mode": "managed_texture_set",
        "texture_contract_status": ATLAS_CANONICAL_TEXTURE_STATUS,
        "texture_base": texture_base,
        "texture_dir": str(texture_root),
        "files": {role: str(path) for role, path in normalized.items()},
        "missing_roles": [],
        "manifest_path": str(Path(manifest_path).resolve()),
    }


def _validate_atlas_cluster_origin_receipt(
    contract,
    source_paths,
    manifest_path,
    asset_root,
    material_name,
    diagnostics,
):
    receipt = contract.get("origin_receipt")
    if not isinstance(receipt, dict):
        diagnostics.append("origin_receipt=missing")
        return None
    if (
        receipt.get("kind") != ATLAS_CLUSTER_RECEIPT_KIND
        or str(receipt.get("version") or "") != "1"
        or receipt.get("source_origin")
        != ATLAS_BLENDER_CLUSTER_BAKE_STATUS
    ):
        diagnostics.append("origin_receipt=unsupported_identity")
    receipt_material = str(receipt.get("material") or "").strip()
    if (
        not receipt_material
        or _speedtree_material_name_key(receipt_material)
        != _speedtree_material_name_key(material_name)
    ):
        diagnostics.append("origin_receipt.material=mismatch")
    capture_hash = str(
        receipt.get("physical_capture_contract_sha256") or ""
    ).strip()
    if not capture_hash:
        diagnostics.append(
            "origin_receipt.physical_capture_contract_sha256=missing"
        )
    cluster_root = Path(asset_root) / "cluster"
    for role, path in source_paths.items():
        if not _path_is_under(path, cluster_root):
            diagnostics.append(
                f"role={role}, path={path}, reason=outside_asset_cluster"
            )

    capture_maps = receipt.get("capture_maps")
    if not isinstance(capture_maps, list) or not capture_maps:
        diagnostics.append("origin_receipt.capture_maps=missing")
        return receipt
    receipt_paths = {}
    for row in capture_maps:
        if (
            not isinstance(row, dict)
            or not row.get("role")
            or not row.get("path")
        ):
            diagnostics.append("origin_receipt.capture_maps=invalid_row")
            continue
        role = _texture_semantic_role(row.get("role"))
        path = _manifest_path_value(row.get("path"), manifest_path)
        if role in receipt_paths and _path_identity(
            receipt_paths[role]
        ) != _path_identity(path):
            diagnostics.append(
                f"origin_receipt.capture_maps role={role}, reason=ambiguous"
            )
            continue
        expected_sha = str(row.get("sha256") or "").strip().casefold()
        if expected_sha:
            try:
                if _file_sha256(path).casefold() != expected_sha:
                    diagnostics.append(
                        f"origin_receipt.capture_maps role={role}, "
                        "reason=sha256_mismatch"
                    )
            except OSError:
                diagnostics.append(
                    f"origin_receipt.capture_maps role={role}, "
                    "reason=unreadable"
                )
        receipt_paths[role] = path
    for role, path in source_paths.items():
        if (
            role not in receipt_paths
            or _path_identity(receipt_paths[role])
            != _path_identity(path)
        ):
            diagnostics.append(
                f"origin_receipt.capture_maps role={role}, "
                f"path={path}, reason=role_path_mismatch"
            )
    receipt_roles = {
        _texture_semantic_role(role)
        for role in receipt.get("source_roles") or []
    }
    if receipt_roles != set(source_paths):
        diagnostics.append(
            "origin_receipt.source_roles=source_mapping_mismatch"
        )
    return receipt


def _validate_atlas_cluster_entry(
    entry,
    manifest_path,
    source_fbx_path=None,
):
    """Validate one receipt-backed Blender physical Cluster texture bake."""
    contract = entry["contract"]
    material_name = str(
        entry.get("material_name")
        or contract.get("material_name")
        or contract.get("material")
        or ""
    ).strip()
    diagnostics = []
    if (
        contract.get("kind")
        != "blender_cluster_bake_texture_contract"
        or str(contract.get("version") or "") != "1"
        or contract.get("texture_contract_status")
        != ATLAS_BLENDER_CLUSTER_BAKE_STATUS
        or contract.get("source_origin")
        != ATLAS_BLENDER_CLUSTER_BAKE_STATUS
    ):
        diagnostics.append("contract=unsupported_identity")
    if not material_name:
        diagnostics.append("material=missing")

    raw_files = contract.get("files")
    if not isinstance(raw_files, dict) or not raw_files:
        diagnostics.append("files=missing")
        raw_files = {}
    asset_root = _atlas_manifest_asset_root(manifest_path, contract)
    cluster_root = asset_root / "cluster"
    normalized = {}
    for raw_role, raw_path in sorted(raw_files.items()):
        role = _texture_semantic_role(raw_role)
        path = _manifest_path_value(raw_path, manifest_path)
        blocked = _blocked_atlas_texture_path(path)
        try:
            ready = path.is_file() and path.stat().st_size > 0
        except OSError:
            ready = False
        if not role:
            diagnostics.append(f"role={raw_role!r}, reason=missing")
        elif blocked:
            diagnostics.append(
                f"role={role}, path={path}, blocked={','.join(blocked)}"
            )
        elif not _path_is_under(path, cluster_root):
            diagnostics.append(
                f"role={role}, path={path}, reason=outside_asset_cluster"
            )
        elif not ready:
            diagnostics.append(
                f"role={role}, path={path}, reason=missing_or_empty"
            )
        elif (
            role in normalized
            and _path_identity(normalized[role]) != _path_identity(path)
        ):
            diagnostics.append(
                f"role={role}, reason=multiple_source_paths"
            )
        else:
            normalized[role] = path

    declared_roles = {
        _texture_semantic_role(role)
        for role in contract.get("source_roles") or []
    }
    if declared_roles != set(normalized):
        diagnostics.append("source_roles=source_mapping_mismatch")
    origin_receipt = _validate_atlas_cluster_origin_receipt(
        contract,
        normalized,
        manifest_path,
        asset_root,
        material_name,
        diagnostics,
    )
    if diagnostics:
        raise RuntimeError(
            "Atlas Blender Cluster bake texture mapping is invalid for "
            f"material={material_name or '<unnamed>'}: "
            + " | ".join(diagnostics)
            + ". Invalid manifests never fall back to exported PNGs, cache, "
            "or directory scanning."
        )
    return {
        "material": material_name,
        "status": "ok",
        "texture_source_mode": "preserve_declared_sources",
        "texture_contract_status": ATLAS_BLENDER_CLUSTER_BAKE_STATUS,
        "source_origin": ATLAS_BLENDER_CLUSTER_BAKE_STATUS,
        "source_evidence": "blender_cluster_bake_receipt",
        "source_paths": {
            role: str(path)
            for role, path in normalized.items()
        },
        "source_roles": sorted(normalized),
        "origin_receipt": dict(origin_receipt or {}),
        "manifest_path": str(Path(manifest_path).resolve()),
    }


def _validate_atlas_provisional_entry(
    entry,
    manifest_path,
    source_fbx_path=None,
):
    contract = entry["contract"]
    warning = str(contract.get("warning") or "").strip()
    remediation = str(contract.get("remediation") or "").strip()
    expected_texture_base = str(
        contract.get("expected_texture_base") or ""
    ).strip()
    expected_t_paths = contract.get("expected_t_paths")
    expected_roles = (
        {
            str(role).casefold()
            for role in expected_t_paths
        }
        if isinstance(expected_t_paths, dict)
        else set()
    )
    asset_root = _atlas_manifest_asset_root(manifest_path, contract)
    texture_root_value = str(contract.get("texture_root") or "").strip()
    if texture_root_value:
        texture_root = _manifest_path_value(
            texture_root_value,
            manifest_path,
        )
    else:
        expected_parents = {
            _manifest_path_value(value, manifest_path).parent
            for value in (expected_t_paths or {}).values()
            if str(value or "").strip()
        }
        inferred_root = (
            next(iter(expected_parents))
            if len(expected_parents) == 1
            else None
        )
        allowed_asset_roots = {asset_root}
        if asset_root.name.casefold() == "cluster":
            allowed_asset_roots.add(asset_root.parent)
        if (
            inferred_root is not None
            and inferred_root.name.casefold() in {"texture", "textures"}
            and inferred_root.parent in allowed_asset_roots
        ):
            texture_root = inferred_root
        else:
            texture_root = asset_root / "texture"
    source_paths = contract.get("source_paths")
    if not isinstance(source_paths, dict):
        source_paths = {}
    normalized = {}
    diagnostics = []
    _validate_atlas_target_identity(
        contract.get("provisional_receipt")
        if isinstance(contract.get("provisional_receipt"), dict)
        else contract,
        manifest_path,
        source_fbx_path,
        diagnostics,
    )
    if not warning:
        diagnostics.append("warning=missing")
    if not remediation or "pcg st9 texture" not in remediation.casefold():
        diagnostics.append(
            "remediation=missing_or_not_pcg_st9_texture"
        )
    if not expected_texture_base.casefold().startswith("t_"):
        diagnostics.append(
            "expected_texture_base=missing_or_not_T_output"
        )
    if expected_roles != set(SPEEDTREE_TEXTURE_ROLES):
        diagnostics.append(
            "expected_t_paths=roles_must_be_exactly_"
            + ",".join(SPEEDTREE_TEXTURE_ROLES)
        )
    normalized_expected = {}
    if isinstance(expected_t_paths, dict):
        for role in SPEEDTREE_TEXTURE_ROLES:
            value = str(expected_t_paths.get(role) or "").strip()
            if not value:
                diagnostics.append(
                    f"expected_role={role}, reason=missing_mapping"
                )
                continue
            path = _manifest_path_value(value, manifest_path)
            blocked = _blocked_atlas_texture_path(path)
            expected = (
                texture_root / f"{expected_texture_base}_{role}.tga"
            )
            if blocked:
                diagnostics.append(
                    f"expected_role={role}, path={path}, "
                    f"blocked={','.join(blocked)}"
                )
            elif not _path_is_under(path, texture_root):
                diagnostics.append(
                    f"expected_role={role}, path={path}, "
                    "reason=outside_texture_root"
                )
            elif path.stem.casefold() != expected.stem.casefold():
                diagnostics.append(
                    f"expected_role={role}, path={path}, "
                    f"expected={expected}, reason=role_filename_mismatch"
                )
            else:
                normalized_expected[role] = str(path)
    for role, value in sorted(source_paths.items()):
        path = _manifest_path_value(value, manifest_path)
        blocked = _blocked_atlas_texture_path(path)
        generated_png = (
            path.suffix.casefold() == ".png"
            and (
                path.name.casefold().startswith("t_")
                or any(
                    token in path.stem.casefold()
                    for token in ("_generated", "_export", "_copied")
                )
            )
        )
        try:
            ready = path.is_file() and path.stat().st_size > 0
        except OSError:
            ready = False
        if blocked:
            diagnostics.append(
                f"role={role}, path={path}, blocked={','.join(blocked)}"
            )
        elif "_pcgtex_generated" in {
            part.casefold() for part in path.parts
        }:
            diagnostics.append(
                f"role={role}, path={path}, reason=generated_output_not_source"
            )
        elif generated_png:
            diagnostics.append(
                f"role={role}, path={path}, reason=export_generated_png"
            )
        elif not ready:
            diagnostics.append(
                f"role={role}, path={path}, reason=missing_or_empty"
            )
        else:
            semantic_role = _atlas_provisional_source_role(role)
            if (
                semantic_role in normalized
                and _path_identity(normalized[semantic_role])
                != _path_identity(path)
            ):
                diagnostics.append(
                    f"role={semantic_role}, reason=multiple_source_paths"
                )
            else:
                normalized[semantic_role] = str(path)
    if "color" not in normalized:
        diagnostics.append(
            "role=albedo, reason=original_atlas_source_required"
        )

    provisional_receipt = contract.get("provisional_receipt")
    source_origin = str(
        contract.get("source_origin")
        or (provisional_receipt or {}).get("source_origin")
        or ""
    ).strip()
    if not isinstance(provisional_receipt, dict):
        diagnostics.append("provisional_receipt=missing")
    else:
        if (
            provisional_receipt.get("kind")
            != ATLAS_PROVISIONAL_RECEIPT_KIND
            or str(provisional_receipt.get("version") or "") != "1"
            or provisional_receipt.get("status")
            != ATLAS_SOURCE_FALLBACK_STATUS
            or not provisional_receipt.get("canonical_promotion_required")
        ):
            diagnostics.append("provisional_receipt=unsupported_identity")
        receipt_material = str(
            provisional_receipt.get("material") or ""
        ).strip()
        if (
            not receipt_material
            or _speedtree_material_name_key(receipt_material)
            != _speedtree_material_name_key(entry["material_name"])
        ):
            diagnostics.append("provisional_receipt.material=mismatch")
        receipt_roles = {
            _atlas_provisional_source_role(role)
            for role in provisional_receipt.get("source_roles") or []
        }
        if receipt_roles != set(normalized):
            diagnostics.append(
                "provisional_receipt.source_roles=source_mapping_mismatch"
            )
        if str(provisional_receipt.get("warning") or "").strip() != warning:
            diagnostics.append("provisional_receipt.warning=mismatch")
        if (
            str(provisional_receipt.get("remediation") or "").strip()
            != remediation
        ):
            diagnostics.append("provisional_receipt.remediation=mismatch")

    origin_receipt = None
    source_evidence = ""
    if source_origin == "atlas_mesh_build_source":
        if normalized and all(
            any(
                _path_is_under(path, root)
                for root in SPEEDTREE_ORIGINAL_TEXTURE_ROOTS
            )
            for path in normalized.values()
        ):
            source_evidence = "authoritative_global_original_root"
        elif isinstance(provisional_receipt, dict):
            # The structured Atlas manifest binds these exact paths, roles,
            # material and target.  This is the only accepted alternative to
            # the configured global source root; directory-name heuristics are
            # deliberately not used.
            source_evidence = "structured_atlas_manifest_exact_paths"
    elif source_origin == ATLAS_BLENDER_CLUSTER_BAKE_STATUS:
        origin_receipt = _validate_atlas_cluster_origin_receipt(
            contract,
            {
                role: Path(path)
                for role, path in normalized.items()
            },
            manifest_path,
            asset_root,
            entry["material_name"],
            diagnostics,
        )
        source_evidence = "blender_cluster_bake_receipt"
    else:
        diagnostics.append(
            "source_origin=missing_or_unsupported"
        )
    if diagnostics:
        raise RuntimeError(
            "Atlas provisional texture mapping is invalid for material="
            f"{entry['material_name'] or '<unnamed>'}: "
            + " | ".join(diagnostics)
            + ". Invalid manifests never fall back to exported PNGs, cache, "
            "or directory scanning."
        )
    result = {
        "material": entry["material_name"],
        "status": (
            "ok"
            if source_origin == ATLAS_BLENDER_CLUSTER_BAKE_STATUS
            else ATLAS_SOURCE_FALLBACK_STATUS
        ),
        "texture_source_mode": "preserve_declared_sources",
        "texture_contract_status": (
            ATLAS_BLENDER_CLUSTER_BAKE_STATUS
            if source_origin == ATLAS_BLENDER_CLUSTER_BAKE_STATUS
            else ATLAS_SOURCE_FALLBACK_STATUS
        ),
        "source_origin": source_origin,
        "source_evidence": source_evidence,
        "source_paths": normalized,
        "source_roles": sorted(normalized),
        "expected_t_paths": normalized_expected,
        "expected_texture_base": expected_texture_base,
        "warning": warning,
        "remediation": remediation,
        "provisional_receipt": dict(provisional_receipt or {}),
        "manifest_path": str(Path(manifest_path).resolve()),
    }
    if origin_receipt is not None:
        result["origin_receipt"] = dict(origin_receipt)
    return result


def _atlas_manifest_contract_signature(binding):
    if binding.get("texture_contract_status") == ATLAS_CANONICAL_TEXTURE_STATUS:
        payload = binding.get("files") or {}
    else:
        payload = binding.get("source_paths") or {}
    return (
        str(binding.get("texture_contract_status") or ""),
        tuple(
            sorted(
                (
                    str(role).casefold(),
                    os.path.normcase(str(path)).casefold(),
                )
                for role, path in payload.items()
            )
        ),
    )


def _validate_atlas_manifest_entry(
    entry,
    manifest_path,
    source_fbx_path=None,
):
    status = entry.get("status")
    if status == ATLAS_CANONICAL_TEXTURE_STATUS:
        return _validate_atlas_canonical_entry(
            entry,
            manifest_path,
            source_fbx_path=source_fbx_path,
        )
    if status == ATLAS_BLENDER_CLUSTER_BAKE_STATUS:
        return _validate_atlas_cluster_entry(
            entry,
            manifest_path,
            source_fbx_path=source_fbx_path,
        )
    return _validate_atlas_provisional_entry(
        entry,
        manifest_path,
        source_fbx_path=source_fbx_path,
    )


def _speedtree_manifest_texture_binding(
    source_fbx_path,
    material,
    stmat_data=None,
    manifest_cache=None,
):
    """Resolve and validate the authoritative Atlas texture handoff."""
    stmat_data = stmat_data or _speedtree_stmat_materials(source_fbx_path)
    manifest_cache = manifest_cache if manifest_cache is not None else {}
    stmat_material = stmat_data.get("materials", {}).get(
        _speedtree_material_name_key(material.name), {}
    )
    stmat_scope = str(
        (stmat_material.get("user_data") or {}).get("scope") or ""
    ).strip()
    material_scope = str(
        material.get("codex_speedtree_export_scope_id", "")
    ).strip()
    if stmat_scope and material_scope and stmat_scope != material_scope:
        raise RuntimeError(
            "SpeedTree material scope identity conflicts with STMAT: "
            f"material={material.name}, stmat_scope={stmat_scope!r}, "
            f"material_scope={material_scope!r}"
        )
    expected_scope = stmat_scope or material_scope
    explicit_manifest = str(
        material.get("codex_speedtree_import_manifest", "")
    ).strip()
    manifest_paths = []
    if explicit_manifest:
        manifest_paths.append(Path(explicit_manifest))
    manifest_paths.extend(
        _speedtree_manifest_paths(source_fbx_path, stmat_material)
    )

    seen = set()
    authoritative_seen = []
    identity_errors = []
    for manifest_path in manifest_paths:
        cache_key = os.path.normcase(str(manifest_path))
        if cache_key in seen:
            continue
        seen.add(cache_key)
        if cache_key not in manifest_cache:
            manifest_cache[cache_key] = _load_speedtree_import_manifest(
                manifest_path
            )
        manifest = manifest_cache[cache_key]
        if not manifest or not manifest.get("texture_contract_status"):
            continue
        manifest_scope = str(
            manifest.get("export_scope_id") or ""
        ).strip()
        consumer_scoped_identity = False
        if manifest_scope:
            consumer_path = _speedtree_consumer_scoped_manifest_path(
                source_fbx_path, manifest
            )
            consumer_key = os.path.normcase(str(consumer_path or ""))
            if consumer_path is not None and consumer_path.is_file():
                if consumer_key not in manifest_cache:
                    manifest_cache[consumer_key] = (
                        _load_speedtree_import_manifest(consumer_path)
                    )
                consumer_manifest = manifest_cache[consumer_key]
                if (
                    consumer_manifest
                    and str(
                        consumer_manifest.get("export_scope_id") or ""
                    ).strip()
                    == manifest_scope
                ):
                    manifest_path = consumer_path
                    manifest = consumer_manifest
                    cache_key = consumer_key
                    consumer_scoped_identity = True
        entries = _manifest_texture_entries(manifest)
        material_key = _speedtree_material_name_key(material.name)
        exact_entries = [
            entry
            for entry in entries
            if _speedtree_material_name_key(
                entry.get("material_name")
            )
            == material_key
        ]
        target_name = _speedtree_manifest_target_name(manifest)
        target_matches = (
            _speedtree_material_name_key(target_name) == material_key
        )
        if not exact_entries and not target_matches:
            # A root-level Atlas manifest can be visible to every FBX
            # material in the asset.  Its scope is authoritative only for
            # mappings it actually owns; unrelated bark/stem materials must
            # continue through their own declared-source contract.
            continue
        manifest_scope = str(manifest.get("export_scope_id") or "").strip()
        if expected_scope and manifest_scope != expected_scope:
            identity_errors.append(
                f"{Path(manifest_path).resolve()}: "
                f"expected_scope={expected_scope!r}, "
                f"manifest_scope={manifest_scope!r}"
            )
            if explicit_manifest and _path_identity(manifest_path) == _path_identity(
                explicit_manifest
            ):
                raise RuntimeError(
                    "Explicit Atlas import manifest scope identity mismatch: "
                    + identity_errors[-1]
                )
            continue
        if (
            not expected_scope
            and manifest_scope
            and not consumer_scoped_identity
        ):
            identity_errors.append(
                f"{Path(manifest_path).resolve()}: "
                f"consumer_scope=missing, manifest_scope={manifest_scope!r}"
            )
            continue
        authoritative_seen.append(str(Path(manifest_path).resolve()))

        candidate_entries = exact_entries or entries
        validated = [
            _validate_atlas_manifest_entry(
                entry,
                manifest_path,
                source_fbx_path=source_fbx_path,
            )
            for entry in candidate_entries
        ]
        matches = [
            row
            for row in validated
            if _speedtree_material_name_key(row.get("material"))
            == material_key
        ]
        if not matches:
            signatures = {
                _atlas_manifest_contract_signature(row)
                for row in validated
            }
            if (
                _speedtree_material_name_key(target_name) == material_key
                and len(signatures) == 1
            ):
                matches = validated[:1]
        if len(matches) > 1:
            signatures = {
                _atlas_manifest_contract_signature(row) for row in matches
            }
            if len(signatures) == 1:
                matches = matches[:1]
        if len(matches) != 1:
            if not matches:
                continue
            raise RuntimeError(
                "Atlas import manifest texture mapping is ambiguous for "
                f"material={material.name}: {manifest_path}"
            )
        result = dict(matches[0])
        result["material"] = material.name
        return result
    if authoritative_seen:
        raise RuntimeError(
            "Atlas import manifest has no exact texture mapping for "
            f"material={material.name}; manifests={authoritative_seen}. "
            "An authoritative but incomplete manifest never falls back to "
            "FBX copies or directory scanning."
        )
    if identity_errors:
        raise RuntimeError(
            "Atlas import manifest identity could not be proven for "
            f"material={material.name}: "
            + " | ".join(identity_errors)
        )
    return None


def _speedtree_manifest_target_name(manifest):
    """Return a manifest-proven atlas base without using a collection label."""
    explicit = str(
        manifest.get("atlas_asset_name") or manifest.get("material") or ""
    ).strip()
    if explicit:
        return explicit

    group_parts = []
    for group in manifest.get("material_groups") or []:
        material_name = str(group.get("material") or "").strip()
        if not material_name:
            return ""
        base_name, group_suffix = _production_group_parts(material_name)
        if not base_name or not group_suffix:
            return ""
        group_parts.append((base_name, group_suffix))
    if len(group_parts) < 2:
        return ""

    common_keys = {
        _speedtree_material_name_key(base_name)
        for base_name, _group_suffix in group_parts
    }
    if len(common_keys) != 1:
        return ""
    common_base = group_parts[0][0]

    blend_file = str(manifest.get("blend_file") or "").strip()
    blend_stem = Path(blend_file.replace("\\", "/")).stem if blend_file else ""
    if (
        blend_stem
        and _speedtree_material_name_key(blend_stem)
        == _speedtree_material_name_key(common_base)
    ):
        return blend_stem
    return common_base


def _speedtree_manifest_binding(
    source_fbx_path,
    material,
    stmat_data=None,
    manifest_cache=None,
    *,
    validate_texture_compatibility=True,
    tolerate_unavailable=False,
):
    """Resolve an intermediate atlas group material to its final atlas material."""
    stmat_data = stmat_data or _speedtree_stmat_materials(source_fbx_path)
    manifest_cache = manifest_cache if manifest_cache is not None else {}
    material_key = _speedtree_material_name_key(material.name)
    stmat_material = stmat_data.get("materials", {}).get(material_key, {})
    stmat_scope = str((stmat_material.get("user_data") or {}).get("scope") or "").strip()
    material_scope = str(
        material.get("codex_speedtree_export_scope_id", "")
    ).strip()
    if stmat_scope and material_scope and stmat_scope != material_scope:
        raise RuntimeError(
            "SpeedTree material scope identity conflicts with STMAT: "
            f"material={material.name}, stmat_scope={stmat_scope!r}, "
            f"material_scope={material_scope!r}"
        )
    expected_scope = stmat_scope or material_scope
    identity_errors = []

    for manifest_path in _speedtree_manifest_paths(source_fbx_path, stmat_material):
        cache_key = os.path.normcase(str(manifest_path))
        if cache_key not in manifest_cache:
            try:
                manifest_cache[cache_key] = _load_speedtree_import_manifest(
                    manifest_path
                )
            except RuntimeError:
                if not tolerate_unavailable:
                    raise
                manifest_cache[cache_key] = None
        manifest = manifest_cache[cache_key]
        if not manifest:
            continue
        manifest_scope = str(
            manifest.get("export_scope_id") or ""
        ).strip()
        consumer_scoped_identity = False
        if manifest_scope:
            consumer_path = _speedtree_consumer_scoped_manifest_path(
                source_fbx_path, manifest
            )
            consumer_key = os.path.normcase(str(consumer_path or ""))
            if consumer_path is not None and consumer_path.is_file():
                if consumer_key not in manifest_cache:
                    try:
                        manifest_cache[consumer_key] = (
                            _load_speedtree_import_manifest(consumer_path)
                        )
                    except RuntimeError:
                        if not tolerate_unavailable:
                            raise
                        manifest_cache[consumer_key] = None
                consumer_manifest = manifest_cache[consumer_key]
                if (
                    consumer_manifest
                    and str(
                        consumer_manifest.get("export_scope_id") or ""
                    ).strip()
                    == manifest_scope
                ):
                    manifest_path = consumer_path
                    manifest = consumer_manifest
                    cache_key = consumer_key
                    consumer_scoped_identity = True
        matching_groups = [
            group
            for group in manifest.get("material_groups") or []
            if (
                isinstance(group, dict)
                and _speedtree_material_name_key(group.get("material"))
                == material_key
            )
        ]
        if not matching_groups:
            # The shared root manifest does not own this material.  Do not
            # impose its Atlas export scope on unrelated bark/stem slots.
            continue
        manifest_scope = str(manifest.get("export_scope_id") or "").strip()
        if expected_scope and manifest_scope != expected_scope:
            identity_errors.append(
                f"{Path(manifest_path).resolve()}: "
                f"expected_scope={expected_scope!r}, "
                f"manifest_scope={manifest_scope!r}"
            )
            continue
        if (
            not expected_scope
            and manifest_scope
            and not consumer_scoped_identity
        ):
            identity_errors.append(
                f"{Path(manifest_path).resolve()}: "
                f"consumer_scope=missing, manifest_scope={manifest_scope!r}"
            )
            continue
        if (
            validate_texture_compatibility
            and manifest.get("texture_contract_status")
        ):
            entries = _manifest_texture_entries(manifest)
            validated = [
                _validate_atlas_manifest_entry(
                    entry,
                    manifest_path,
                    source_fbx_path=source_fbx_path,
                )
                for entry in entries
            ]
            if len(
                {
                    _atlas_manifest_contract_signature(row)
                    for row in validated
                }
            ) > 1:
                # Distinct material texture contracts cannot be collapsed into
                # one Blender material merely because Atlas groups share a
                # production naming base.
                return None
        # New manifests persist atlas_asset_name. For legacy multi-group
        # manifests, prove the target from their shared numeric-boundary base
        # (and prefer a matching .blend stem). A generic source_collection such
        # as Atlas_Leaf_Meshes is ownership metadata, not a material name.
        target_name = _speedtree_manifest_target_name(manifest)
        if not target_name:
            continue
        for group in matching_groups:
            if _speedtree_material_name_key(target_name) == material_key:
                return None
            return {
                "target_name": target_name,
                "manifest_path": str(manifest_path),
                "export_scope_id": manifest_scope,
                "group": str(group.get("collection") or ""),
            }
    if identity_errors:
        raise RuntimeError(
            "Atlas import manifest identity could not be proven for "
            f"material={material.name}: "
            + " | ".join(identity_errors)
        )
    return None


def _speedtree_material_texture_dirs(source_fbx_path, material, stmat_data=None):
    """Return managed texture locations, including shared paths recorded by SpeedTree."""
    texture_root = _speedtree_texture_dir(source_fbx_path)
    paths = [texture_root, texture_root / "substance"]
    asset_root = _speedtree_asset_root(source_fbx_path)
    if asset_root.name.casefold() == "cluster":
        # A canonical Cluster source lives at <tree>/cluster/SK_*.spm while
        # its SBS/PCG T_ outputs remain at <tree>/texture. SpeedTree preserves
        # SPM-relative "../texture" references, but the exported STMAT is
        # written one directory deeper at <tree>/cluster/fbx and can therefore
        # make that same spelling look like <tree>/cluster/texture. Keep the
        # explicit Blender Cluster bake exception receipt-driven; this sibling
        # lookup is only another candidate location for canonical T_ sets.
        sibling_texture_root = asset_root.parent / "texture"
        paths.extend(
            (
                sibling_texture_root,
                sibling_texture_root / "substance",
            )
        )
    stmat_data = stmat_data or _speedtree_stmat_materials(source_fbx_path)
    stmat_material = stmat_data.get("materials", {}).get(
        _speedtree_material_name_key(material.name), {}
    )
    for source in stmat_material.get("source_paths", []):
        try:
            paths.append(Path(source).expanduser().resolve().parent)
        except (OSError, ValueError):
            continue
    unique = []
    seen = set()
    for path in paths:
        key = os.path.normcase(str(path))
        if key not in seen:
            seen.add(key)
            unique.append(path)
    return unique


def _canonical_speedtree_texture_base(spelling_counts):
    """Pick one canonical spelling among case-variant texture base names.

    Windows filesystems treat differently cased spellings as one managed set,
    so the spelling used by the most role files wins; a single stray file such
    as ``T_leaf_x_atlas_02_extra`` cannot rename or split the set.
    """
    return min(spelling_counts, key=lambda name: (-spelling_counts[name], name))


def _speedtree_texture_sets(texture_dir):
    """Index only the managed top-level T_<set>_<role> batch outputs.

    Base names that differ only by case are one set; ``bases`` holds one
    canonical spelling per set.
    """
    texture_dir = Path(texture_dir)
    indexed = defaultdict(
        lambda: {"bases": set(), "files": {}, "file_candidates": {}}
    )
    try:
        if not texture_dir.is_dir():
            return indexed
        entries = list(texture_dir.iterdir())
    except OSError:
        return indexed
    role_pattern = "|".join(re.escape(role) for role in SPEEDTREE_TEXTURE_ROLES)
    pattern = re.compile(rf"^(T_.+)_({role_pattern})$", re.IGNORECASE)
    extension_rank = {
        extension: index for index, extension in enumerate(SPEEDTREE_TEXTURE_EXTENSIONS)
    }
    base_spellings = defaultdict(dict)
    for path in entries:
        try:
            usable = (
                path.is_file()
                and path.suffix.lower() in extension_rank
            )
        except OSError:
            usable = False
        if not usable:
            continue
        match = pattern.match(path.stem)
        if not match:
            continue
        texture_base, role = match.group(1), match.group(2).lower()
        key = _speedtree_texture_set_key(texture_base)
        row = indexed[key]
        row["file_candidates"].setdefault(role, []).append(path)
        spelling_counts = base_spellings[key].setdefault(texture_base.casefold(), {})
        spelling_counts[texture_base] = spelling_counts.get(texture_base, 0) + 1
        current = row["files"].get(role)
        if current is None or extension_rank[path.suffix.lower()] < extension_rank[current.suffix.lower()]:
            row["files"][role] = path
    for key, variants in base_spellings.items():
        indexed[key]["bases"] = {
            _canonical_speedtree_texture_base(spelling_counts)
            for spelling_counts in variants.values()
        }
        for role, paths in indexed[key]["file_candidates"].items():
            indexed[key]["file_candidates"][role] = sorted(
                set(paths),
                key=lambda candidate: (
                    extension_rank[candidate.suffix.lower()],
                    candidate.name.casefold(),
                ),
            )
    return indexed


def _speedtree_stmat_texture_set(source_fbx_path, material, stmat_data=None):
    """Return one unambiguous live managed T_ subset referenced by STMAT.

    PCG can intentionally connect differently named SpeedTree materials to one
    shared texture set.  In that case ``M_material -> T_material`` name matching
    is not the contract: the exported STMAT source paths are. Missing roles are
    ordinary availability and do not invalidate the live roles on that base.
    """
    stmat_data = stmat_data or _speedtree_stmat_materials(source_fbx_path)
    stmat_material = stmat_data.get("materials", {}).get(
        _speedtree_material_name_key(material.name), {}
    )
    role_pattern = "|".join(re.escape(role) for role in SPEEDTREE_TEXTURE_ROLES)
    pattern = re.compile(rf"^(T_.+)_({role_pattern})$", re.IGNORECASE)
    referenced = defaultdict(lambda: {"bases": set(), "roles": set()})
    for source in stmat_material.get("source_paths", []):
        try:
            path = _resolved_speedtree_source_path(source_fbx_path, source)
        except (OSError, ValueError):
            continue
        if path.suffix.lower() not in SPEEDTREE_TEXTURE_EXTENSIONS:
            continue
        match = pattern.match(path.stem)
        if not match:
            continue
        try:
            if not path.is_file() or path.stat().st_size <= 0:
                continue
        except OSError:
            continue
        if _blocked_atlas_texture_path(path):
            continue
        texture_base, role = match.group(1), match.group(2).lower()
        row = referenced[
            (os.path.normcase(str(path.parent)), _speedtree_texture_set_key(texture_base))
        ]
        # Case-variant spellings of one base are one reference on Windows;
        # the canonical spelling comes from the directory scan below.
        row["bases"].add(texture_base.casefold())
        row["roles"].add(role)

    candidates = []
    for (texture_dir, texture_key), row in referenced.items():
        if len(row["bases"]) != 1:
            continue
        match = _speedtree_texture_sets(texture_dir).get(texture_key)
        if not match or len(match["bases"]) != 1:
            continue
        safe_file_candidates = {
            role: [
                path
                for path in match.get("file_candidates", {}).get(role, [])
                if not _blocked_atlas_texture_path(path)
            ]
            for role in SPEEDTREE_TEXTURE_ROLES
        }
        safe_file_candidates = {
            role: paths
            for role, paths in safe_file_candidates.items()
            if paths
        }
        available = {
            role: safe_file_candidates[role][0]
            for role in SPEEDTREE_TEXTURE_ROLES
            if role in safe_file_candidates
        }
        if not available:
            continue
        candidates.append(
            {
                "texture_dir": str(texture_dir),
                "texture_base": next(iter(match["bases"])),
                "files": available,
                "file_candidates": {
                    role: list(safe_file_candidates[role])
                    for role in available
                },
                "stmat_roles": sorted(row["roles"]),
                "available_roles": sorted(available),
                "missing_roles": sorted(
                    set(SPEEDTREE_TEXTURE_ROLES) - set(available)
                ),
            }
        )
    return candidates[0] if len(candidates) == 1 else None


def _complete_speedtree_texture_set(
    material, target_name, stmat_cache=None, texture_index_cache=None
):
    """Find the first unambiguous live canonical T_ subset for a material."""
    if material is None:
        return None
    source_fbx = str(material.get("codex_source_fbx", "")).strip()
    if not source_fbx:
        return None
    stmat_cache = stmat_cache if stmat_cache is not None else {}
    texture_index_cache = (
        texture_index_cache if texture_index_cache is not None else {}
    )
    source_key = normalized_source_fbx_path(source_fbx)
    if source_key not in stmat_cache:
        stmat_cache[source_key] = _speedtree_stmat_materials(source_fbx)
    texture_key = _speedtree_texture_set_key(target_name)
    candidates = []
    for texture_dir in _speedtree_material_texture_dirs(
        source_fbx, material, stmat_cache[source_key]
    ):
        if _blocked_atlas_texture_path(texture_dir):
            continue
        cache_key = os.path.normcase(str(texture_dir))
        if cache_key not in texture_index_cache:
            texture_index_cache[cache_key] = _speedtree_texture_sets(texture_dir)
        match = texture_index_cache[cache_key].get(texture_key)
        if not match or len(match["bases"]) != 1:
            continue
        safe_file_candidates = {
            role: [
                str(path)
                for path in match.get("file_candidates", {}).get(role, [])
                if not _blocked_atlas_texture_path(path)
            ]
            for role in SPEEDTREE_TEXTURE_ROLES
        }
        safe_file_candidates = {
            role: paths
            for role, paths in safe_file_candidates.items()
            if paths
        }
        available = {
            role: paths[0] for role, paths in safe_file_candidates.items()
        }
        if not available:
            continue
        candidates.append(
            {
                "texture_dir": str(texture_dir),
                "texture_base": next(iter(match["bases"])),
                "files": available,
                "file_candidates": {
                    role: list(safe_file_candidates[role])
                    for role in available
                },
                "available_roles": sorted(available),
                "missing_roles": sorted(
                    set(SPEEDTREE_TEXTURE_ROLES) - set(available)
                ),
            }
        )
    signatures = {
        _speedtree_texture_file_signature(candidate)
        for candidate in candidates
    }
    signatures.discard(())
    if len(signatures) != 1:
        return None
    signature = next(iter(signatures))
    return next(
        candidate
        for candidate in candidates
        if _speedtree_texture_file_signature(candidate) == signature
    )


def _speedtree_texture_file_signature(texture_set):
    if not texture_set:
        return ()
    files = texture_set.get("files") or {}
    available_roles = [
        role for role in SPEEDTREE_TEXTURE_ROLES if files.get(role)
    ]
    if not available_roles:
        return ()
    return tuple(
        (
            role,
            os.path.normcase(
                os.path.normpath(os.path.abspath(os.path.expanduser(str(files[role]))))
            ).casefold(),
        )
        for role in available_roles
    )


def _remove_speedtree_image_nodes(material):
    removed_images = set()
    if not material or not material.node_tree:
        return removed_images
    for node in list(material.node_tree.nodes):
        if node.type != "TEX_IMAGE" or not node.image:
            continue
        removed_images.add(node.image.name)
        material.node_tree.nodes.remove(node)
    return removed_images


def _clear_speedtree_texture_binding_properties(material):
    """Remove cached path authority when a material is left unassigned."""
    if material is None:
        return False
    changed = False
    for key in (
        "codex_speedtree_texture_base",
        "codex_speedtree_texture_contract_status",
        "codex_speedtree_texture_manifest",
        "codex_speedtree_expected_t_paths",
    ):
        if key in material:
            del material[key]
            changed = True
    return changed


def _speedtree_material_images_decodable(material):
    if not material or not material.node_tree:
        return False
    image_nodes = [
        node
        for node in material.node_tree.nodes
        if node.type == "TEX_IMAGE" and node.image
    ]
    return bool(image_nodes) and all(
        int(node.image.size[0]) > 0 and int(node.image.size[1]) > 0
        for node in image_nodes
    )


def _load_speedtree_image(path, role, *, require_decodable=False):
    image = bpy.data.images.load(str(path), check_existing=True)
    if (
        require_decodable
        and (int(image.size[0]) <= 0 or int(image.size[1]) <= 0)
    ):
        if image.users == 0:
            bpy.data.images.remove(image)
        raise RuntimeError(f"Texture image could not be decoded: {path}")
    image.name = path.stem
    if role not in {"color", "subsurface"}:
        try:
            image.colorspace_settings.name = "Non-Color"
        except TypeError:
            pass
    return image


def _replace_speedtree_material_nodes(
    material,
    texture_files,
    *,
    tolerate_load_errors=False,
    texture_file_candidates=None,
):
    material.use_nodes = True
    nodes = material.node_tree.nodes
    links = material.node_tree.links
    removed_images = _remove_speedtree_image_nodes(material)
    for node in list(nodes):
        if node.type == "NORMAL_MAP":
            nodes.remove(node)

    bsdf = next((node for node in nodes if node.type == "BSDF_PRINCIPLED"), None)
    image_nodes = {}
    load_errors = {}
    selected_files = {}
    texture_file_candidates = texture_file_candidates or {}
    for index, role in enumerate(SPEEDTREE_TEXTURE_ROLES):
        path = texture_files.get(role)
        if path is None:
            continue
        candidates = list(texture_file_candidates.get(role) or [path])
        if path not in candidates and str(path) not in {
            str(candidate) for candidate in candidates
        }:
            candidates.insert(0, path)
        unique_candidates = []
        seen_candidates = set()
        for candidate in candidates:
            candidate_path = Path(candidate)
            try:
                candidate_key = _path_identity(candidate_path)
            except (OSError, ValueError):
                candidate_key = str(candidate_path).casefold()
            if candidate_key in seen_candidates:
                continue
            seen_candidates.add(candidate_key)
            unique_candidates.append(candidate_path)
        image = None
        candidate_errors = []
        for candidate_path in unique_candidates:
            try:
                image = _load_speedtree_image(
                    candidate_path,
                    role,
                    require_decodable=tolerate_load_errors,
                )
            except (OSError, RuntimeError, TypeError, ValueError) as exc:
                if not tolerate_load_errors:
                    raise
                candidate_errors.append(
                    f"{candidate_path}: {exc}"
                )
                continue
            path = candidate_path
            break
        if image is None:
            load_errors[role] = " | ".join(candidate_errors)
            continue
        node = nodes.new("ShaderNodeTexImage")
        node.name = path.stem
        node.label = path.stem
        node.image = image
        node.location = (-720, 360 - index * 190)
        image_nodes[role] = node
        selected_files[role] = path

    if bsdf is not None:
        color_socket = bsdf.inputs.get("Base Color")
        if color_socket is not None and "color" in image_nodes:
            links.new(image_nodes["color"].outputs["Color"], color_socket)

        normal_socket = bsdf.inputs.get("Normal")
        if normal_socket is not None and "normal" in image_nodes:
            normal_map = nodes.new("ShaderNodeNormalMap")
            normal_map.name = "SpeedTree T_ Normal"
            normal_map.label = "SpeedTree T_ Normal"
            normal_map.location = (-360, 120)
            links.new(image_nodes["normal"].outputs["Color"], normal_map.inputs["Color"])
            links.new(normal_map.outputs["Normal"], normal_socket)

        alpha_socket = bsdf.inputs.get("Alpha")
        if alpha_socket is not None and "opacity" in image_nodes:
            links.new(image_nodes["opacity"].outputs["Color"], alpha_socket)

    for image_name in removed_images:
        image = bpy.data.images.get(image_name)
        if image and image.users == 0:
            bpy.data.images.remove(image)
    return {
        "loaded_roles": sorted(image_nodes),
        "failed_roles": sorted(load_errors),
        "load_errors": load_errors,
        "selected_files": selected_files,
    }


def _provisional_speedtree_role_files(source_paths):
    aliases = {
        "color": ("albedo", "color", "diffuse"),
        "normal": ("normal",),
        "extra": ("extra", "roughness", "gloss", "ao"),
        "height": ("height", "displacement"),
        "opacity": ("alpha", "opacity"),
        "subsurface": (
            "translucency",
            "subsurface",
            "transmission",
        ),
    }
    normalized = {
        str(role).casefold(): Path(str(path))
        for role, path in (source_paths or {}).items()
    }
    result = {}
    for role, role_aliases in aliases.items():
        for alias in role_aliases:
            if alias in normalized:
                result[role] = normalized[alias]
                break
    return result


def _replace_speedtree_provisional_material_nodes(
    material,
    texture_files,
    *,
    tolerate_load_errors=False,
):
    """Wire only explicitly declared original sources for provisional Atlas."""
    material.use_nodes = True
    nodes = material.node_tree.nodes
    links = material.node_tree.links
    removed_images = _remove_speedtree_image_nodes(material)
    for node in list(nodes):
        if node.type == "NORMAL_MAP":
            nodes.remove(node)

    bsdf = next(
        (node for node in nodes if node.type == "BSDF_PRINCIPLED"),
        None,
    )
    image_nodes = {}
    load_errors = {}
    for index, role in enumerate(SPEEDTREE_TEXTURE_ROLES):
        path = texture_files.get(role)
        if path is None:
            continue
        try:
            image = _load_speedtree_image(
                path,
                role,
                require_decodable=tolerate_load_errors,
            )
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            if not tolerate_load_errors:
                raise
            load_errors[role] = str(exc)
            continue
        node = nodes.new("ShaderNodeTexImage")
        node.name = path.stem
        node.label = (
            path.stem + " [provisional: generate PCG ST9 T_*]"
        )
        node.image = image
        node.location = (-720, 360 - index * 190)
        image_nodes[role] = node

    if bsdf is not None:
        if "color" in image_nodes:
            socket = bsdf.inputs.get("Base Color")
            if socket is not None:
                links.new(image_nodes["color"].outputs["Color"], socket)
        if "normal" in image_nodes:
            socket = bsdf.inputs.get("Normal")
            if socket is not None:
                normal_map = nodes.new("ShaderNodeNormalMap")
                normal_map.name = "SpeedTree provisional Normal"
                normal_map.label = "SpeedTree provisional Normal"
                normal_map.location = (-360, 120)
                links.new(
                    image_nodes["normal"].outputs["Color"],
                    normal_map.inputs["Color"],
                )
                links.new(normal_map.outputs["Normal"], socket)
        if "opacity" in image_nodes:
            socket = bsdf.inputs.get("Alpha")
            if socket is not None:
                links.new(
                    image_nodes["opacity"].outputs["Color"], socket
                )

    for image_name in removed_images:
        image = bpy.data.images.get(image_name)
        if image and image.users == 0:
            bpy.data.images.remove(image)
    return {
        "loaded_roles": sorted(image_nodes),
        "failed_roles": sorted(load_errors),
        "load_errors": load_errors,
    }


def _validate_atlas_overlay_compatibility(
    material,
    binding,
    existing,
):
    if len(existing) > 1:
        raise RuntimeError(
            "SpeedTree material-intent bindings are ambiguous before "
            f"authoritative Atlas manifest overlay: {material.name}"
        )
    if not existing:
        return
    previous = existing[0]
    manifest_status = str(
        binding.get("texture_contract_status") or ""
    )
    previous_mode = str(
        previous.get("texture_source_mode") or ""
    )
    if (
        manifest_status in {
            ATLAS_SOURCE_FALLBACK_STATUS,
            ATLAS_BLENDER_CLUSTER_BAKE_STATUS,
        }
        and previous_mode != "preserve_declared_sources"
    ):
        raise RuntimeError(
            "Atlas source texture manifest conflicts with a managed strict "
            f"material binding: {material.name}"
        )
    if manifest_status in {
        ATLAS_SOURCE_FALLBACK_STATUS,
        ATLAS_BLENDER_CLUSTER_BAKE_STATUS,
    }:
        previous_paths = previous.get("source_paths") or {}
        if previous_paths:
            previous_path_ids = {
                _path_identity(path)
                for path in previous_paths.values()
            }
            manifest_path_ids = {
                _path_identity(path)
                for path in (binding.get("source_paths") or {}).values()
            }
            if not previous_path_ids.issubset(manifest_path_ids):
                raise RuntimeError(
                    "Atlas source texture manifest conflicts with the strict "
                    f"material binding: {material.name}"
                )
    if (
        manifest_status == ATLAS_CANONICAL_TEXTURE_STATUS
        and previous_mode == "managed_texture_set"
        and previous.get("status") == "ok"
    ):
        previous_files = {
            role: _path_identity(path)
            for role, path in (previous.get("files") or {}).items()
        }
        manifest_files = {
            role: _path_identity(path)
            for role, path in (binding.get("files") or {}).items()
        }
        if previous_files != manifest_files:
            raise RuntimeError(
                "Atlas canonical texture manifest conflicts with the strict "
                f"material binding: {material.name}"
            )


def preflight_speedtree_material_texture_contracts(
    objects,
    texture_contract=None,
    *,
    source_fbx_override="",
):
    """Resolve one normalized 3-state binding per material without mutation."""
    materials = collect_object_materials(objects)
    strict_contract = bool(
        isinstance(texture_contract, dict)
        and texture_contract.get("strict_speedtree_pipeline_contract")
    )
    runtime_tolerant = _runtime_tolerant_texture_contract(
        texture_contract
    )
    original_bindings = []
    if isinstance(texture_contract, dict):
        for row in texture_contract.get("bindings") or []:
            if isinstance(row, dict):
                original_bindings.append(dict(row))
    bindings = defaultdict(list)
    group_bindings = defaultdict(list)
    for row in original_bindings:
        bindings[
            _speedtree_material_name_key(row.get("material"))
        ].append(row)
        group_base = str(row.get("production_group_base") or "").strip()
        if group_base:
            group_bindings[
                _speedtree_material_name_key(group_base)
            ].append(row)

    stmat_cache = {}
    manifest_cache = {}
    reports = []
    diagnostics = list(
        (texture_contract or {}).get("texture_diagnostics") or []
    )
    normalized_by_material = {}
    overlay_statuses = set()
    for material in materials:
        material_key = _speedtree_material_name_key(material.name)
        material_diagnostics = []
        source_fbx = str(
            source_fbx_override
            or material.get("codex_source_fbx", "")
        ).strip()
        if not source_fbx:
            if strict_contract and not runtime_tolerant:
                raise RuntimeError(
                    "Strict SpeedTree material has no source FBX identity: "
                    + material.name
                )
            if runtime_tolerant:
                effective, diagnostic = _quarantine_texture_binding(
                    None,
                    material,
                    "missing_source_fbx_identity",
                    "No source FBX identity is available for local texture lookup",
                    allow_local_search=False,
                )
                normalized_by_material[material_key] = effective
                diagnostics.append(diagnostic)
                reports.append(
                    {
                        "material": material.name,
                        "source_fbx": "",
                        "manifest_path": "",
                        "texture_contract_status": "",
                        "status": "unassigned",
                        "binding_disposition": "leave_unassigned",
                        "diagnostics": [diagnostic],
                    }
                )
            continue
        source_key = normalized_source_fbx_path(source_fbx)
        if source_key not in stmat_cache:
            stmat_cache[source_key] = _speedtree_stmat_materials(source_fbx)
        existing = list(
            bindings.get(material_key) or []
        )
        if not existing:
            group_base = handoff_contract.production_group_base_name(
                material.name
            )
            if group_base:
                existing = list(
                    group_bindings.get(
                        _speedtree_material_name_key(group_base)
                    )
                    or []
                )
        if len(existing) > 1:
            signatures = {
                json.dumps(
                    {
                        "status": row.get("status"),
                        "texture_source_mode": row.get(
                            "texture_source_mode"
                        ),
                        "origin_state": row.get("origin_state"),
                        "set_key": row.get("set_key"),
                        "texture_base": row.get("texture_base"),
                        "files": row.get("files") or {},
                        "slot_files": row.get("slot_files") or [],
                        "origin_receipt": row.get(
                            "origin_receipt"
                        ) or {},
                        "missing_roles": row.get("missing_roles") or [],
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                    default=str,
                )
                for row in existing
            }
            if len(signatures) == 1:
                existing = existing[:1]
        manifest_binding_rejected = False
        try:
            manifest_binding = _speedtree_manifest_texture_binding(
                source_fbx,
                material,
                stmat_data=stmat_cache[source_key],
                manifest_cache=manifest_cache,
            )
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            if not runtime_tolerant:
                raise
            manifest_binding_rejected = True
            manifest_binding = None
            atlas_statuses = {
                ATLAS_CANONICAL_TEXTURE_STATUS,
                ATLAS_SOURCE_FALLBACK_STATUS,
                ATLAS_BLENDER_CLUSTER_BAKE_STATUS,
            }
            existing = [
                row
                for row in existing
                if not (
                    str(row.get("manifest_path") or "").strip()
                    or str(row.get("texture_contract_status") or "")
                    in atlas_statuses
                )
            ]
            message = str(exc)
            lowered_message = message.casefold()
            ordinary_mismatch = (
                "ambiguous" in lowered_message
                or "no exact texture mapping" in lowered_message
            )
            material_diagnostics.append(
                _texture_diagnostic(
                    material,
                    (
                        "ambiguous_texture_authority"
                        if "ambiguous" in lowered_message
                        else "unmatched_atlas_manifest_texture"
                        if ordinary_mismatch
                        else "atlas_manifest_binding_rejected"
                    ),
                    message,
                    severity="info",
                )
            )
        if strict_contract and manifest_binding is not None:
            try:
                _validate_atlas_overlay_compatibility(
                    material,
                    manifest_binding,
                    existing,
                )
            except (RuntimeError, TypeError, ValueError) as exc:
                if not runtime_tolerant:
                    raise
                material_diagnostics.append(
                    _texture_diagnostic(
                        material,
                        "ambiguous_texture_authority",
                        str(exc),
                    )
                )
                # Neither conflicting authority may be consumed.
                existing = []
                manifest_binding = None
        effective = None
        if manifest_binding is not None:
            effective = dict(existing[0]) if len(existing) == 1 else {}
            effective.update(manifest_binding)
        if effective is None and len(existing) == 1:
            effective = dict(existing[0])
        if (
            effective is None
            and runtime_tolerant
            and manifest_binding_rejected
        ):
            effective, _ignored_diagnostic = _quarantine_texture_binding(
                None,
                material,
                "atlas_manifest_binding_rejected",
                "Rejected Atlas manifest authority was left unassigned",
            )
        origin_state = str(
            (effective or {}).get("origin_state") or ""
        ).strip()
        effective_origin_receipt = (
            (effective or {}).get("origin_receipt")
        )
        if (
            receipt_declares_preview_fallback(
                effective_origin_receipt
            )
            and (
                origin_state == "canonical_t"
                or (effective or {}).get("texture_source_mode")
                == "managed_texture_set"
            )
        ):
            message = (
                "Preview-only texture receipt cannot satisfy a canonical "
                f"production binding: {material.name}"
            )
            if not runtime_tolerant:
                raise RuntimeError(message)
            effective, diagnostic = _quarantine_texture_binding(
                effective,
                material,
                "preview_receipt_not_production_capable",
                message,
            )
            material_diagnostics.append(diagnostic)
            origin_state = ""
        if origin_state == "canonical_t":
            effective["texture_contract_status"] = (
                ATLAS_CANONICAL_TEXTURE_STATUS
            )
            effective["texture_source_mode"] = "managed_texture_set"
            effective["status"] = "ok"
        elif origin_state == ATLAS_BLENDER_CLUSTER_BAKE_STATUS:
            effective["texture_contract_status"] = (
                ATLAS_BLENDER_CLUSTER_BAKE_STATUS
            )
            effective["texture_source_mode"] = (
                "preserve_declared_sources"
            )
            effective["status"] = "ok"
        elif origin_state == ATLAS_SOURCE_FALLBACK_STATUS:
            effective["texture_contract_status"] = (
                ATLAS_SOURCE_FALLBACK_STATUS
            )
            effective["texture_source_mode"] = (
                "preserve_declared_sources"
            )
        if (
            effective is not None
            and effective.get("texture_contract_status")
            == ATLAS_SOURCE_FALLBACK_STATUS
            and not str(effective.get("source_evidence") or "").strip()
        ):
            source_paths = effective.get("source_paths") or {}
            source_files = []
            if isinstance(source_paths, dict):
                for value in source_paths.values():
                    try:
                        path = Path(str(value)).expanduser().resolve()
                        ready = path.is_file() and path.stat().st_size > 0
                    except (OSError, ValueError):
                        ready = False
                        path = None
                    if (
                        not ready
                        or path is None
                        or _blocked_atlas_texture_path(path)
                        or not any(
                            _path_is_under(path, root)
                            for root in SPEEDTREE_ORIGINAL_TEXTURE_ROOTS
                        )
                    ):
                        source_files = []
                        break
                    source_files.append(path)
            if source_files:
                effective["source_evidence"] = (
                    "authoritative_global_original_root"
                )
        if strict_contract and effective is None:
            reason = "missing" if not existing else "ambiguous"
            message = (
                "SpeedTree material-intent texture binding is "
                f"{reason} before material mutation: {material.name}"
            )
            if not runtime_tolerant:
                raise RuntimeError(message)
            effective, diagnostic = _quarantine_texture_binding(
                None,
                material,
                f"{reason}_material_texture_binding",
                message,
            )
            if reason == "missing" and not material_diagnostics:
                effective["allow_local_search"] = True
            material_diagnostics.append(diagnostic)
        try:
            cluster_proof = _speedtree_preserved_cluster_sources(
                source_fbx,
                material,
                stmat_cache[source_key],
                expected_binding=effective,
            )
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            if not runtime_tolerant:
                raise
            cluster_proof = None
            material_diagnostics.append(
                _texture_diagnostic(
                    material,
                    "cluster_texture_receipt_rejected",
                    str(exc),
                    severity="info",
                )
            )
        if (
            (
                effective is None
                or (
                    runtime_tolerant
                    and effective.get("status") == "unassigned"
                    and effective.get("allow_local_search")
                )
            )
            and cluster_proof is not None
        ):
            effective = {
                "material": material.name,
                "material_key": material_key,
                "status": "ok",
                "texture_source_mode": "preserve_declared_sources",
                "texture_contract_status":
                    ATLAS_BLENDER_CLUSTER_BAKE_STATUS,
                "source_origin": ATLAS_BLENDER_CLUSTER_BAKE_STATUS,
                "source_paths": dict(cluster_proof["source_maps"]),
                "source_roles": sorted(cluster_proof["source_maps"]),
                "origin_receipt": dict(cluster_proof["origin_receipt"]),
            }
        elif (
            effective is not None
            and effective.get("texture_contract_status")
            == ATLAS_BLENDER_CLUSTER_BAKE_STATUS
        ):
            if cluster_proof is None:
                message = (
                    "Blender Cluster bake binding does not match the exact "
                    "STMAT material/map-slot/path/hash receipt before material "
                    "mutation: "
                    + material.name
                )
                if not runtime_tolerant:
                    raise RuntimeError(message)
                effective, diagnostic = _quarantine_texture_binding(
                    effective,
                    material,
                    "cluster_texture_receipt_mismatch",
                    message,
                    severity="info",
                )
                material_diagnostics.append(diagnostic)
            else:
                effective["origin_receipt"] = dict(
                    cluster_proof["origin_receipt"]
                )
                effective["source_paths"] = dict(
                    cluster_proof["source_maps"]
                )
                effective["source_roles"] = sorted(
                    cluster_proof["source_maps"]
                )
        elif (
            effective is not None
            and effective.get("texture_source_mode")
            == "preserve_declared_sources"
            and cluster_proof is not None
        ):
            effective.update({
                "status": "ok",
                "texture_contract_status":
                    ATLAS_BLENDER_CLUSTER_BAKE_STATUS,
                "source_origin": ATLAS_BLENDER_CLUSTER_BAKE_STATUS,
                "source_paths": dict(cluster_proof["source_maps"]),
                "source_roles": sorted(cluster_proof["source_maps"]),
                "origin_receipt": dict(cluster_proof["origin_receipt"]),
            })
        if (
            effective is not None
            and effective.get("texture_contract_status")
            == ATLAS_SOURCE_FALLBACK_STATUS
            and not _has_authoritative_fallback_evidence(effective)
        ):
            message = (
                "Provisional SpeedTree binding lacks authoritative original "
                "root or structured Atlas manifest evidence before material "
                "mutation: "
                + material.name
            )
            if not runtime_tolerant:
                raise RuntimeError(message)
            effective, diagnostic = _quarantine_texture_binding(
                effective,
                material,
                "provisional_texture_evidence_missing",
                message,
                severity="info",
            )
            material_diagnostics.append(diagnostic)
        if (
            effective is not None
            and effective.get("texture_source_mode")
            == "preserve_declared_sources"
            and effective.get("texture_contract_status") not in {
                ATLAS_BLENDER_CLUSTER_BAKE_STATUS,
                ATLAS_SOURCE_FALLBACK_STATUS,
            }
        ):
            declared = _speedtree_preserved_declared_sources(
                source_fbx,
                material,
                stmat_cache[source_key],
            )
            if declared is None:
                message = (
                    "SpeedTree declared texture sources are stale before "
                    "material mutation: "
                    + material.name
                )
                if not runtime_tolerant:
                    raise RuntimeError(message)
                effective, diagnostic = _quarantine_texture_binding(
                    effective,
                    material,
                    "declared_texture_sources_stale",
                    message,
                    severity="info",
                )
                material_diagnostics.append(diagnostic)
            else:
                effective["declared_source_receipt"] = declared
        if (
            effective is not None
            and effective.get("texture_source_mode")
            == "managed_texture_set"
        ):
            live_files = {}
            stale_roles = []
            unsafe_roles = []
            for role in SPEEDTREE_TEXTURE_ROLES:
                file_path = Path(
                    str((effective.get("files") or {}).get(role) or "")
                )
                try:
                    ready = (
                        file_path.is_file()
                        and file_path.stat().st_size > 0
                    )
                except OSError:
                    ready = False
                blocked_parts = _blocked_atlas_texture_path(file_path)
                if ready and not blocked_parts:
                    live_files[role] = str(file_path)
                else:
                    stale_roles.append(role)
                    if blocked_parts:
                        unsafe_roles.append(role)
            if (
                effective.get("status") != "ok"
                or stale_roles
            ):
                message = (
                    "SpeedTree managed texture binding is incomplete before "
                    f"material mutation: {material.name}; missing="
                    + ",".join(stale_roles)
                )
                if not runtime_tolerant:
                    if effective.get("status") != "ok":
                        raise RuntimeError(
                            "SpeedTree managed texture binding is unresolved before "
                            "material mutation: "
                            + material.name
                        )
                    raise RuntimeError(
                        "SpeedTree managed texture binding is stale before "
                        f"material mutation: {material.name} {stale_roles[0]}"
                    )
                effective["files"] = live_files
                effective["available_roles"] = sorted(live_files)
                effective["missing_roles"] = sorted(stale_roles)
                effective["status"] = (
                    "partial" if live_files else "unassigned"
                )
                effective["binding_disposition"] = (
                    "bind_available"
                    if live_files
                    else "leave_unassigned"
                )
                effective["allow_local_search"] = not bool(live_files)
                diagnostic = _texture_diagnostic(
                    material,
                    "unsafe_texture_path_quarantined"
                    if unsafe_roles
                    else "partial_texture_binding"
                    if live_files
                    else "unassigned_texture_binding",
                    message,
                    severity="info",
                    unsafe_roles=unsafe_roles,
                )
                material_diagnostics.append(diagnostic)
            elif runtime_tolerant:
                effective["files"] = live_files
                effective["available_roles"] = sorted(live_files)
                effective["missing_roles"] = []
                effective["binding_disposition"] = "bind_available"
        if effective is not None:
            if (
                runtime_tolerant
                and effective.get("status") == "unassigned"
            ):
                effective.setdefault("allow_local_search", True)
            effective["material"] = material.name
            effective["material_key"] = material_key
            effective["origin_state"] = str(
                effective.get("origin_state")
                or effective.get("texture_contract_status")
                or ""
            )
            status = str(
                effective.get("texture_contract_status") or ""
            )
            if status:
                overlay_statuses.add(status)
            normalized_by_material[material_key] = effective
        diagnostics.extend(material_diagnostics)
        reports.append({
            "material": material.name,
            "source_fbx": source_fbx,
            "manifest_path": (
                (manifest_binding or {}).get("manifest_path", "")
            ),
            "texture_contract_status": (
                (effective or {}).get("texture_contract_status", "")
            ),
            "status": (effective or {}).get("status", "unassigned"),
            "binding_disposition": (effective or {}).get(
                "binding_disposition", "leave_unassigned"
            ),
            "diagnostics": material_diagnostics,
        })
    normalized_contract = dict(texture_contract or {})
    normalized_contract["bindings"] = [
        row
        for row in original_bindings
        if _speedtree_material_name_key(row.get("material"))
        not in normalized_by_material
    ] + list(normalized_by_material.values())
    normalized_contract[
        "atlas_manifest_prevalidated"
    ] = True
    normalized_contract[
        "atlas_manifest_statuses"
    ] = sorted(overlay_statuses)
    normalized_contract[
        "material_texture_preflight"
    ] = reports
    normalized_contract["texture_diagnostics"] = diagnostics
    normalized_contract["texture_warnings"] = [
        row for row in diagnostics if row.get("severity") == "warning"
    ]
    report_states = {str(row.get("status") or "") for row in reports}
    texture_outcome = (
        "partial"
        if "partial" in report_states
        else "unassigned"
        if report_states & {"unassigned", "missing"}
        else "complete"
    )
    normalized_contract["texture_outcome"] = texture_outcome
    if runtime_tolerant:
        normalized_contract["status"] = "ok"
    return {
        # Texture availability is an output dimension, never an operation
        # result.  Keeping the operation successful prevents this field from
        # becoming another downstream admission checkpoint.
        "status": "ok",
        "texture_outcome": texture_outcome,
        "strict_speedtree_pipeline_contract": strict_contract,
        "materials": reports,
        "diagnostics": diagnostics,
        "warnings": [
            row for row in diagnostics
            if row.get("severity") == "warning"
        ],
        "blocking": [],
        "texture_contract": normalized_contract,
    }


def normalize_speedtree_material_textures(objects, texture_contract=None):
    """Wire only safe available textures and leave unresolved roles unassigned."""
    if not (
        isinstance(texture_contract, dict)
        and texture_contract.get("atlas_manifest_prevalidated")
    ):
        texture_contract = preflight_speedtree_material_texture_contracts(
            objects,
            texture_contract,
        )["texture_contract"]

    rows = []
    removed_dummy_images = set()
    stmat_cache = {}
    texture_index_cache = {}
    contract_bindings = defaultdict(list)
    contract_bindings_by_group_base = defaultdict(list)
    contract_status = ""
    overlay_contract_statuses = set(
        texture_contract.get("atlas_manifest_statuses") or []
    )
    strict_contract = bool(
        isinstance(texture_contract, dict)
        and texture_contract.get("strict_speedtree_pipeline_contract")
    )
    runtime_tolerant = _runtime_tolerant_texture_contract(
        texture_contract
    )
    if isinstance(texture_contract, dict):
        contract_status = str(texture_contract.get("status") or "")
        for binding in texture_contract.get("bindings") or []:
            if not isinstance(binding, dict):
                continue
            contract_bindings[
                _speedtree_material_name_key(binding.get("material"))
            ].append(binding)
            group_base = str(binding.get("production_group_base") or "")
            if group_base:
                contract_bindings_by_group_base[
                    _speedtree_material_name_key(group_base)
                ].append(binding)
    materials = collect_object_materials(objects)

    def strict_binding_candidates(material):
        candidates = contract_bindings.get(
            _speedtree_material_name_key(material.name), []
        )
        if not candidates:
            group_base = handoff_contract.production_group_base_name(
                material.name
            )
            if group_base:
                candidates = contract_bindings_by_group_base.get(
                    _speedtree_material_name_key(group_base), []
                )
        if len(candidates) > 1:
            signatures = {
                json.dumps(
                    {
                        "status": row.get("status"),
                        "texture_source_mode": row.get("texture_source_mode"),
                        "origin_state": row.get("origin_state"),
                        "set_key": row.get("set_key"),
                        "texture_base": row.get("texture_base"),
                        "files": row.get("files") or {},
                        "slot_files": row.get("slot_files") or [],
                        "origin_receipt": row.get(
                            "origin_receipt"
                        ) or {},
                        "missing_roles": row.get("missing_roles") or [],
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                    default=str,
                )
                for row in candidates
            }
            if len(signatures) == 1:
                candidates = candidates[:1]
        return candidates

    if strict_contract and not runtime_tolerant:
        # Preflight already proved every file/receipt.  Downstream consumes the
        # normalized binding only and never reclassifies provenance.
        for material in materials:
            candidates = strict_binding_candidates(material)
            if len(candidates) != 1:
                reason = "missing" if not candidates else "ambiguous"
                raise RuntimeError(
                    f"SpeedTree material-intent texture binding is {reason} "
                    f"after consolidation: {material.name}"
                )
            binding = candidates[0]
            mode = str(binding.get("texture_source_mode") or "")
            if (
                binding.get("texture_contract_status")
                == ATLAS_SOURCE_FALLBACK_STATUS
            ):
                if not _has_authoritative_fallback_evidence(binding):
                    raise RuntimeError(
                        "Normalized provisional SpeedTree binding has no "
                        "authoritative evidence: "
                        + material.name
                    )
                continue
            if (
                binding.get("texture_contract_status")
                == ATLAS_BLENDER_CLUSTER_BAKE_STATUS
            ):
                if not isinstance(binding.get("origin_receipt"), dict):
                    raise RuntimeError(
                        "Normalized Blender Cluster bake binding has no exact "
                        "origin receipt: "
                        + material.name
                    )
                continue
            if mode == "preserve_declared_sources":
                if not isinstance(
                    binding.get("declared_source_receipt"),
                    dict,
                ):
                    raise RuntimeError(
                        "Normalized SpeedTree declared-source binding has no "
                        "preflight receipt: "
                        + material.name
                    )
                continue
            if mode != "managed_texture_set" or binding.get("status") != "ok":
                raise RuntimeError(
                    "SpeedTree managed texture binding is unresolved: "
                    + material.name
                )

    for material in materials:
        source_fbx = str(material.get("codex_source_fbx", "")).strip()
        if not source_fbx:
            if runtime_tolerant:
                removed = _remove_speedtree_image_nodes(material)
                removed_dummy_images.update(removed)
                metadata_cleared = (
                    _clear_speedtree_texture_binding_properties(material)
                )
                rows.append(
                    {
                        "material": material.name,
                        "texture_dirs": [],
                        "texture_attempts": [],
                        "matched_texture_bases": [],
                        "missing_roles": list(SPEEDTREE_TEXTURE_ROLES),
                        "match_source": "none",
                        "status": "unassigned",
                        "binding_disposition": "leave_unassigned",
                        "reason": "missing_source_fbx_identity",
                        "diagnostic_codes": [
                            "missing_source_fbx_identity"
                        ],
                        "changed": bool(removed) or metadata_cleared,
                    }
                )
            continue
        source_key = normalized_source_fbx_path(source_fbx)
        if source_key not in stmat_cache:
            stmat_cache[source_key] = _speedtree_stmat_materials(source_fbx)
        stmat_data = stmat_cache[source_key]
        key = _speedtree_material_name_key(material.name)
        binding_candidates = contract_bindings.get(key, [])
        if strict_contract:
            binding_candidates = strict_binding_candidates(material)
        if len(binding_candidates) > 1:
            signatures = {
                json.dumps(
                    {
                        "status": row.get("status"),
                        "texture_source_mode": row.get("texture_source_mode"),
                        "origin_state": row.get("origin_state"),
                        "set_key": row.get("set_key"),
                        "texture_base": row.get("texture_base"),
                        "files": row.get("files") or {},
                        "slot_files": row.get("slot_files") or [],
                        "origin_receipt": row.get(
                            "origin_receipt"
                        ) or {},
                        "missing_roles": row.get("missing_roles") or [],
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                    default=str,
                )
                for row in binding_candidates
            }
            if len(signatures) == 1:
                binding_candidates = binding_candidates[:1]
        contract_binding = (
            binding_candidates[0] if len(binding_candidates) == 1 else None
        )
        contract_blocked = len(binding_candidates) > 1 or (
            strict_contract
            and not runtime_tolerant
            and not binding_candidates
        )
        contract_reason = "ambiguous_material_binding" if contract_blocked else ""
        if strict_contract and not binding_candidates:
            contract_reason = "missing_material_intent_binding"
        stale_roles = []
        unsafe_roles = []
        referenced_set = None
        source_mode = str(
            (contract_binding or {}).get("texture_source_mode") or ""
        )
        provisional_manifest_binding = (
            contract_binding
            if (
                (contract_binding or {}).get("texture_contract_status")
                == ATLAS_SOURCE_FALLBACK_STATUS
                and (contract_binding or {}).get("status")
                != "unassigned"
                and (contract_binding or {}).get(
                    "binding_disposition"
                )
                != "leave_unassigned"
            )
            else None
        )
        cluster_manifest_binding = (
            contract_binding
            if (
                (contract_binding or {}).get("texture_contract_status")
                == ATLAS_BLENDER_CLUSTER_BAKE_STATUS
                and (contract_binding or {}).get("status")
                != "unassigned"
                and (contract_binding or {}).get(
                    "binding_disposition"
                )
                != "leave_unassigned"
            )
            else None
        )
        if cluster_manifest_binding is not None:
            origin_receipt = dict(
                cluster_manifest_binding.get("origin_receipt") or {}
            )
            preserved_files = dict(
                cluster_manifest_binding.get("source_paths") or {}
            )
            preserved_cluster = {
                "cluster_root": str(
                    _speedtree_asset_root(source_fbx) / "cluster"
                ),
                "source_maps": preserved_files,
                "preserved_files": preserved_files,
                "origin_kind": ATLAS_BLENDER_CLUSTER_BAKE_STATUS,
                "origin_receipt": origin_receipt,
            }
            rows.append(
                {
                    "material": material.name,
                    "texture_contract_status":
                        ATLAS_BLENDER_CLUSTER_BAKE_STATUS,
                    "source_paths": dict(
                        cluster_manifest_binding.get("source_paths") or {}
                    ),
                    "source_roles": list(
                        cluster_manifest_binding.get("source_roles") or []
                    ),
                    "manifest_path": cluster_manifest_binding.get(
                        "manifest_path", ""
                    ),
                    "match_source": "atlas_import_manifest",
                    "status": "preserved_cluster",
                    "changed": False,
                    **preserved_cluster,
                }
            )
            continue
        if provisional_manifest_binding is not None:
            texture_files = _provisional_speedtree_role_files(
                provisional_manifest_binding.get("source_paths") or {}
            )
            target_paths = {
                str(Path(path).resolve()).casefold()
                for path in texture_files.values()
            }
            expected_base = str(
                provisional_manifest_binding.get(
                    "expected_texture_base"
                )
                or ""
            )
            already_normalized = (
                material.get(
                    "codex_speedtree_texture_contract_status", ""
                )
                == ATLAS_SOURCE_FALLBACK_STATUS
                and material_texture_signature(material)
                == tuple(sorted(target_paths))
                and (
                    not runtime_tolerant
                    or _speedtree_material_images_decodable(material)
                )
            )
            load_result = {
                "loaded_roles": sorted(texture_files),
                "failed_roles": [],
                "load_errors": {},
            }
            if not already_normalized:
                load_result = _replace_speedtree_provisional_material_nodes(
                    material,
                    texture_files,
                    tolerate_load_errors=runtime_tolerant,
                )
                texture_files = {
                    role: path
                    for role, path in texture_files.items()
                    if role in load_result["loaded_roles"]
                }
            metadata_cleared = False
            if texture_files:
                material[
                    "codex_speedtree_texture_contract_status"
                ] = ATLAS_SOURCE_FALLBACK_STATUS
                material["codex_speedtree_texture_base"] = expected_base
                material["codex_speedtree_expected_t_paths"] = json.dumps(
                    provisional_manifest_binding.get(
                        "expected_t_paths"
                    )
                    or {},
                    sort_keys=True,
                )
                material[
                    "codex_speedtree_texture_manifest"
                ] = provisional_manifest_binding.get(
                    "manifest_path", ""
                )
            else:
                metadata_cleared = (
                    _clear_speedtree_texture_binding_properties(material)
                )
            failed_roles = list(load_result["failed_roles"])
            rows.append(
                {
                    "material": material.name,
                    "texture_contract_status": (
                        ATLAS_SOURCE_FALLBACK_STATUS
                    ),
                    "source_paths": dict(
                        provisional_manifest_binding.get(
                            "source_paths"
                        )
                        or {}
                    ),
                    "source_roles": list(
                        provisional_manifest_binding.get(
                            "source_roles"
                        )
                        or []
                    ),
                    "expected_texture_base": expected_base,
                    "expected_t_paths": dict(
                        provisional_manifest_binding.get(
                            "expected_t_paths"
                        )
                        or {}
                    ),
                    "warning": provisional_manifest_binding.get(
                        "warning", ""
                    ),
                    "remediation": provisional_manifest_binding.get(
                        "remediation", ""
                    ),
                    "manifest_path": provisional_manifest_binding.get(
                        "manifest_path", ""
                    ),
                    "match_source": "atlas_import_manifest",
                    "status": (
                        "needs_pcg_generation"
                        if texture_files
                        else "unassigned"
                    ),
                    "binding_disposition": (
                        "bind_available"
                        if texture_files
                        else "leave_unassigned"
                    ),
                    "files": {
                        role: str(path)
                        for role, path in texture_files.items()
                    },
                    "available_roles": sorted(texture_files),
                    "missing_roles": sorted(
                        set(failed_roles)
                        | (
                            set(SPEEDTREE_TEXTURE_ROLES)
                            - set(texture_files)
                        )
                    ),
                    "texture_load_errors": dict(
                        load_result["load_errors"]
                    ),
                    "diagnostic_codes": (
                        ["texture_image_load_failed"]
                        if failed_roles
                        else []
                    ),
                    "changed": (
                        not already_normalized or metadata_cleared
                    ),
                }
            )
            continue
        preserve_declared = (
            strict_contract and source_mode == "preserve_declared_sources"
        )
        if preserve_declared:
            preserved_cluster = None
            preserved_declared = dict(
                (contract_binding or {}).get(
                    "declared_source_receipt"
                )
                or {}
            )
            if preserved_declared:
                rows.append(
                    {
                        "material": material.name,
                        "texture_dirs": [
                            str(path)
                            for path in _speedtree_material_texture_dirs(
                                source_fbx, material, stmat_data
                            )
                        ],
                        "matched_texture_bases": [],
                        "missing_roles": [],
                        "match_source": "speedtree_material_intent",
                        "status": (
                            "preserved_cluster"
                            if preserved_cluster
                            else "preserved_declared_sources"
                        ),
                        "changed": False,
                        **preserved_declared,
                    }
                )
                continue
            contract_blocked = True
            contract_reason = "declared_sources_missing"
            stale_roles = ["declared_source"]
        elif contract_binding and contract_binding.get("status") in {
            "ok",
            "partial",
        }:
            contract_files = {}
            stale_roles = []
            unsafe_contract_roles = []
            for role in SPEEDTREE_TEXTURE_ROLES:
                path = Path(
                    str(
                        (contract_binding.get("files") or {}).get(role)
                        or ""
                    )
                )
                try:
                    ready = path.is_file() and path.stat().st_size > 0
                except OSError:
                    ready = False
                blocked_parts = _blocked_atlas_texture_path(path)
                if ready and not blocked_parts:
                    contract_files[role] = path
                else:
                    stale_roles.append(role)
                    if blocked_parts:
                        unsafe_contract_roles.append(role)
            unsafe_roles = list(unsafe_contract_roles)
            if stale_roles and not runtime_tolerant:
                contract_blocked = True
                contract_reason = "contract_files_missing"
            elif contract_files:
                referenced_set = {
                    "texture_dir": contract_binding.get("texture_dir", ""),
                    "texture_base": contract_binding.get("texture_base", ""),
                    "files": contract_files,
                    "stmat_roles": contract_binding.get("stmat_roles", []),
                    "missing_roles": stale_roles,
                    "unsafe_roles": unsafe_contract_roles,
                }
            else:
                contract_blocked = not bool(
                    runtime_tolerant
                    and contract_binding.get("allow_local_search")
                )
                contract_reason = (
                    "unsafe_texture_path_quarantined"
                    if unsafe_contract_roles
                    else "contract_files_unassigned"
                )
        elif contract_binding and contract_binding.get("status") != "not_managed":
            contract_blocked = not bool(
                runtime_tolerant
                and contract_binding.get("allow_local_search")
            )
            contract_reason = str(
                contract_binding.get("status") or "contract_binding_incomplete"
            )
            stale_roles = list(contract_binding.get("missing_roles") or [])

        if (
            referenced_set is None
            and not contract_blocked
            and (not strict_contract or runtime_tolerant)
        ):
            referenced_set = _speedtree_stmat_texture_set(
                source_fbx, material, stmat_data
            )
        texture_dirs = _speedtree_material_texture_dirs(
            source_fbx, material, stmat_data
        )
        key = _speedtree_texture_set_key(material.name)
        material_base = re.sub(r"(\.\d{3})$", "", material.name)
        if material_base[:2].lower() in {"m_", "t_"}:
            material_base = material_base[2:]
        expected_base = "T_" + material_base
        missing_roles = list(SPEEDTREE_TEXTURE_ROLES)
        texture_base = expected_base
        texture_files = {}
        texture_file_candidates = {}
        ambiguity = []
        texture_dir = texture_dirs[0] if texture_dirs else _speedtree_texture_dir(source_fbx)
        attempts = []
        best_attempt = None
        match_source = "material_name"
        if contract_blocked:
            missing_roles = stale_roles or ["texture_binding"]
            ambiguity = list(
                (contract_binding or {}).get("referenced_set_keys") or []
            )
            match_source = (
                "speedtree_material_intent"
                if strict_contract
                else "shared_texture_contract"
            )
            attempts.append(
                {
                    "texture_dir": str(texture_dir),
                    "matched_texture_bases": ambiguity,
                    "missing_roles": missing_roles,
                    "match_source": match_source,
                    "reason": contract_reason,
                }
            )
        elif referenced_set:
            texture_dir_value = str(
                referenced_set.get("texture_dir") or ""
            )
            texture_dir = (
                Path(texture_dir_value)
                if texture_dir_value
                else Path(next(iter(referenced_set["files"].values()))).parent
            )
            texture_base = referenced_set["texture_base"]
            texture_files = dict(referenced_set["files"])
            texture_file_candidates = dict(
                referenced_set.get("file_candidates") or {}
            )
            missing_roles = list(
                referenced_set.get("missing_roles") or [
                    role
                    for role in SPEEDTREE_TEXTURE_ROLES
                    if role not in texture_files
                ]
            )
            unsafe_roles = list(referenced_set.get("unsafe_roles") or [])
            ambiguity = [texture_base]
            match_source = (
                "atlas_import_manifest"
                if (contract_binding or {}).get(
                    "texture_contract_status"
                )
                == ATLAS_CANONICAL_TEXTURE_STATUS
                else "speedtree_material_intent"
                if strict_contract
                else "shared_texture_contract"
                if contract_binding and contract_binding.get("status") == "ok"
                else "stmat_reference"
            )
            attempts.append(
                {
                    "texture_dir": str(texture_dir),
                    "matched_texture_bases": [texture_base],
                    "missing_roles": missing_roles,
                    "match_source": match_source,
                }
            )
        elif not strict_contract or runtime_tolerant:
            usable_attempts = []
            for candidate_dir in texture_dirs:
                blocked_parts = _blocked_atlas_texture_path(candidate_dir)
                if blocked_parts:
                    attempts.append(
                        {
                            "texture_dir": str(candidate_dir),
                            "matched_texture_bases": [],
                            "missing_roles": list(SPEEDTREE_TEXTURE_ROLES),
                            "match_source": match_source,
                            "reason": "unsafe_texture_root",
                            "blocked_path_parts": blocked_parts,
                        }
                    )
                    continue
                cache_key = os.path.normcase(str(candidate_dir))
                if cache_key not in texture_index_cache:
                    texture_index_cache[cache_key] = _speedtree_texture_sets(
                        candidate_dir
                    )
                match = texture_index_cache[cache_key].get(key)
                candidate_bases = (
                    sorted(match["bases"], key=str.casefold)
                    if match
                    else []
                )
                candidate_file_options = (
                    {
                        role: [
                            path
                            for path in paths
                            if not _blocked_atlas_texture_path(path)
                        ]
                        for role, paths in (
                            match.get("file_candidates", {})
                        ).items()
                    }
                    if match and len(candidate_bases) == 1
                    else {}
                )
                candidate_file_options = {
                    role: paths
                    for role, paths in candidate_file_options.items()
                    if paths
                }
                candidate_files = {
                    role: paths[0]
                    for role, paths in candidate_file_options.items()
                }
                candidate_missing = [
                    role
                    for role in SPEEDTREE_TEXTURE_ROLES
                    if role not in candidate_files
                ]
                attempt = {
                    "texture_dir": str(candidate_dir),
                    "matched_texture_bases": candidate_bases,
                    "missing_roles": candidate_missing,
                    "files": {
                        role: str(path)
                        for role, path in candidate_files.items()
                    },
                    "file_candidates": {
                        role: [str(path) for path in paths]
                        for role, paths in candidate_file_options.items()
                    },
                    "match_source": match_source,
                }
                attempts.append(attempt)
                if candidate_files and len(candidate_bases) == 1:
                    usable_attempts.append(attempt)
                if (
                    not runtime_tolerant
                    and len(candidate_bases) == 1
                    and not candidate_missing
                ):
                    texture_dir = candidate_dir
                    texture_base = candidate_bases[0]
                    texture_files = candidate_files
                    missing_roles = []
                    ambiguity = candidate_bases
                    break
            if runtime_tolerant and usable_attempts:
                best_missing_count = min(
                    len(row["missing_roles"])
                    for row in usable_attempts
                )
                best_rows = [
                    row for row in usable_attempts
                    if len(row["missing_roles"]) == best_missing_count
                ]
                signatures = {
                    tuple(
                        sorted(
                            (role, _path_identity(path))
                            for role, path in row["files"].items()
                        )
                    )
                    for row in best_rows
                }
                if len(signatures) == 1:
                    best_attempt = best_rows[0]
                    texture_dir = Path(best_attempt["texture_dir"])
                    texture_base = best_attempt[
                        "matched_texture_bases"
                    ][0]
                    texture_files = {
                        role: Path(path)
                        for role, path in best_attempt["files"].items()
                    }
                    texture_file_candidates = dict(
                        best_attempt.get("file_candidates") or {}
                    )
                    ambiguity = [texture_base]
                    missing_roles = list(best_attempt["missing_roles"])
                else:
                    contract_blocked = True
                    contract_reason = "ambiguous_local_texture_candidates"
                    ambiguity = sorted(
                        {
                            base
                            for row in best_rows
                            for base in row["matched_texture_bases"]
                        },
                        key=str.casefold,
                    )
            elif not texture_files and attempts:
                best_attempt = min(
                    attempts,
                    key=lambda row: len(row.get("missing_roles") or []),
                )
                texture_dir = Path(best_attempt["texture_dir"])
                ambiguity = best_attempt["matched_texture_bases"]
                missing_roles = best_attempt["missing_roles"]
        else:
            contract_blocked = True
            contract_reason = contract_reason or "material_intent_binding_unresolved"
            missing_roles = stale_roles or ["texture_binding"]
            match_source = "speedtree_material_intent"

        unresolved_runtime = runtime_tolerant and (
            contract_blocked
            or not texture_files
            or len(ambiguity) > 1
        )
        strict_missing = (
            not runtime_tolerant
            and (contract_blocked or missing_roles or len(ambiguity) > 1)
        )
        if unresolved_runtime or strict_missing:
            removed = _remove_speedtree_image_nodes(material)
            removed_dummy_images.update(removed)
            metadata_cleared = False
            if runtime_tolerant:
                metadata_cleared = (
                    _clear_speedtree_texture_binding_properties(material)
                )
            row_status = "unassigned" if runtime_tolerant else "missing"
            rows.append(
                {
                    "material": material.name,
                    "texture_dir": str(texture_dir),
                    "texture_dirs": [str(path) for path in texture_dirs],
                    "texture_attempts": attempts,
                    "expected_texture_base": expected_base,
                    "matched_texture_bases": ambiguity,
                    "missing_roles": missing_roles,
                    "match_source": match_source,
                    "status": row_status,
                    "binding_disposition": "leave_unassigned",
                    "reason": contract_reason or "no_safe_texture_candidate",
                    "unsafe_roles": unsafe_roles,
                    "diagnostic_codes": [
                        contract_reason or "no_safe_texture_candidate"
                    ],
                    "changed": bool(removed) or metadata_cleared,
                }
            )
            continue

        # Verified large blends get re-saved on every push if we rebuild the
        # node tree unconditionally; skip materials already wired to exactly
        # this texture set so callers can tell nothing was mutated.
        target_paths = set()
        for role, texture_path in texture_files.items():
            try:
                target_paths.add(str(Path(texture_path).resolve()).lower())
            except (OSError, ValueError):
                target_paths.add(str(texture_path).lower())
        already_normalized = (
            str(material.get("codex_speedtree_texture_base", "")) == texture_base
            and material_texture_signature(material) == tuple(sorted(target_paths))
            and (
                not runtime_tolerant
                or _speedtree_material_images_decodable(material)
            )
        )
        load_result = {
            "loaded_roles": sorted(texture_files),
            "failed_roles": [],
            "load_errors": {},
            "selected_files": dict(texture_files),
        }
        if not already_normalized:
            load_result = _replace_speedtree_material_nodes(
                material,
                texture_files,
                tolerate_load_errors=runtime_tolerant,
                texture_file_candidates=texture_file_candidates,
            )
            texture_files = {
                role: Path(path)
                for role, path in load_result["selected_files"].items()
            }
            missing_roles = sorted(
                set(missing_roles) | set(load_result["failed_roles"])
            )
        metadata_cleared = False
        if texture_files:
            material["codex_speedtree_texture_base"] = texture_base
            if (
                (contract_binding or {}).get(
                    "texture_contract_status"
                )
                == ATLAS_CANONICAL_TEXTURE_STATUS
            ):
                material[
                    "codex_speedtree_texture_contract_status"
                ] = ATLAS_CANONICAL_TEXTURE_STATUS
                material[
                    "codex_speedtree_texture_manifest"
                ] = contract_binding.get("manifest_path", "")
        elif runtime_tolerant:
            metadata_cleared = (
                _clear_speedtree_texture_binding_properties(material)
            )
        row_status = (
            "unassigned"
            if not texture_files
            else "partial"
            if missing_roles
            else "ok"
        )
        rows.append(
            {
                "material": material.name,
                "texture_dir": str(texture_dir),
                "texture_dirs": [str(path) for path in texture_dirs],
                "texture_base": texture_base,
                "files": {
                    role: str(path)
                    for role, path in texture_files.items()
                },
                "available_roles": sorted(texture_files),
                "missing_roles": missing_roles,
                "unsafe_roles": unsafe_roles,
                "match_source": match_source,
                "status": row_status,
                "binding_disposition": (
                    "bind_available"
                    if texture_files
                    else "leave_unassigned"
                ),
                "texture_load_errors": dict(
                    load_result["load_errors"]
                ),
                "diagnostic_codes": (
                    ["texture_image_load_failed"]
                    if load_result["failed_roles"]
                    else []
                ),
                "changed": (
                    not already_normalized or metadata_cleared
                ),
                "manifest_path": (
                    (contract_binding or {}).get("manifest_path", "")
                ),
            }
        )

    for image_name in removed_dummy_images:
        image = bpy.data.images.get(image_name)
        if image and image.users == 0:
            bpy.data.images.remove(image)

    missing = [
        row for row in rows
        if row["status"] in {"missing", "partial", "unassigned"}
    ]
    partial = [row for row in rows if row["status"] == "partial"]
    unassigned = [
        row for row in rows if row["status"] == "unassigned"
    ]
    preserved_cluster_count = sum(
        1 for row in rows if row["status"] == "preserved_cluster"
    )
    needs_pcg_generation_count = sum(
        1 for row in rows if row["status"] == "needs_pcg_generation"
    )
    if runtime_tolerant:
        status = "ok"
    elif missing:
        status = "missing"
    elif needs_pcg_generation_count:
        status = "needs_pcg_generation"
    elif preserved_cluster_count:
        status = "preserved_cluster"
    else:
        status = "ok"
    manifest_paths = sorted(
        {
            str(row.get("manifest_path") or "")
            for row in rows
            if str(row.get("manifest_path") or "")
        },
        key=str.casefold,
    )
    explicit_contract_path = (
        texture_contract.get("contract_path", "")
        if isinstance(texture_contract, dict)
        else ""
    )
    if ATLAS_SOURCE_FALLBACK_STATUS in overlay_contract_statuses:
        aggregate_contract_status = ATLAS_SOURCE_FALLBACK_STATUS
    elif ATLAS_CANONICAL_TEXTURE_STATUS in overlay_contract_statuses:
        aggregate_contract_status = ATLAS_CANONICAL_TEXTURE_STATUS
    elif ATLAS_BLENDER_CLUSTER_BAKE_STATUS in overlay_contract_statuses:
        aggregate_contract_status = ATLAS_BLENDER_CLUSTER_BAKE_STATUS
    else:
        aggregate_contract_status = contract_status
    normalization_diagnostics = list(
        texture_contract.get("texture_diagnostics") or []
    )
    diagnostic_rows = list(partial) + list(unassigned)
    diagnostic_rows.extend(
        row
        for row in rows
        if row.get("texture_load_errors")
        and row not in diagnostic_rows
    )
    for row in diagnostic_rows:
        unsafe = bool(row.get("unsafe_roles")) or any(
            attempt.get("reason") == "unsafe_texture_root"
            for attempt in row.get("texture_attempts") or []
        )
        load_failed = bool(row.get("texture_load_errors"))
        code = (
            "unsafe_texture_path_quarantined"
            if unsafe
            else "texture_image_load_failed"
            if load_failed
            else str(
                (row.get("diagnostic_codes") or [row["status"]])[0]
            )
        )
        normalization_diagnostics.append(
            _texture_diagnostic(
                row.get("material", ""),
                code,
                row.get("reason")
                or (
                    "One or more texture files could not be loaded; their "
                    "parameters remain unassigned"
                    if load_failed
                    else "Texture parameters remain partially or wholly unassigned"
                ),
                severity=(
                    "warning" if load_failed else "info"
                ),
            )
        )
    normalization_warnings = [
        row for row in normalization_diagnostics
        if row.get("severity") == "warning"
    ]
    return {
        "status": status,
        "texture_outcome": (
            "partial"
            if partial
            else "unassigned"
            if unassigned or needs_pcg_generation_count
            else "complete"
        ),
        handoff_contract.TEXTURE_CONTRACT_MODE_FIELD: (
            texture_contract.get(
                handoff_contract.TEXTURE_CONTRACT_MODE_FIELD,
                handoff_contract.STRICT_PUBLICATION_TEXTURE_MODE,
            )
        ),
        "strict_speedtree_pipeline_contract": strict_contract,
        "texture_contract_status": aggregate_contract_status,
        "texture_contract_statuses": sorted(overlay_contract_statuses),
        "texture_contract_path": (
            explicit_contract_path
            or (manifest_paths[0] if len(manifest_paths) == 1 else "")
        ),
        "texture_contract_paths": manifest_paths,
        "materials": rows,
        "missing": missing,
        "partial": partial,
        "unassigned": unassigned,
        "diagnostics": normalization_diagnostics,
        "warnings": normalization_warnings,
        "blocking": [],
        "material_count": len(rows),
        "preserved_cluster_count": preserved_cluster_count,
        "needs_pcg_generation_count": needs_pcg_generation_count,
        "changed_count": sum(1 for row in rows if row.get("changed")),
    }


def rebind_blocked_speedtree_group_variants(objects):
    """Replace stale isolated variant images with one canonical T_ group set.

    Cluster normalization can retain a generated material variant such as
    ``M_leaf_x_atlas_01_green`` while a later canonical source repair creates
    the authoritative base material ``M_leaf_x_atlas_01``.  A saved prototype
    must not keep image paths inside ``.sk_batch_isolated_bark`` merely because
    its material name includes that production-group suffix.  The imported FBX
    itself may intentionally live in that isolated workspace; its provenance
    alone is not evidence that already-normalized production images are unsafe.
    """
    targets = collect_object_materials(objects)
    stmat_cache = {}
    texture_index_cache = {}
    candidates_by_group = defaultdict(list)

    for material in bpy.data.materials:
        texture_base = str(
            material.get("codex_speedtree_texture_base", "")
        ).strip()
        source_fbx = str(material.get("codex_source_fbx", "")).strip()
        if (
            not texture_base.casefold().startswith("t_")
            or not source_fbx
        ):
            continue
        group_base = handoff_contract.production_group_base_name(material.name)
        group_key = _speedtree_material_name_key(group_base)
        texture_set = _complete_speedtree_texture_set(
            material,
            texture_base,
            stmat_cache=stmat_cache,
            texture_index_cache=texture_index_cache,
        )
        if texture_set:
            candidates_by_group[group_key].append(
                {
                    "material": material,
                    "source_fbx": source_fbx,
                    "source_identity": str(
                        material.get("codex_source_identity", "")
                    ),
                    "texture_set": texture_set,
                }
            )

    rows = []
    for material in targets:
        source_fbx = str(material.get("codex_source_fbx", "")).strip()
        blocked_images = sorted(
            {
                blocked
                for path in material_texture_signature(material)
                for blocked in _blocked_atlas_texture_path(path)
            }
        )
        blocked_source = _blocked_atlas_texture_path(source_fbx)
        # The batch deliberately imports an FBX from its isolated bark
        # workspace.  Only image paths can leak that workspace into the saved
        # prototype, so preserve a material whose current images already point
        # at safe production files even when its source-FBX provenance remains
        # isolated.
        if not blocked_images:
            continue
        group_base = handoff_contract.production_group_base_name(material.name)
        group_key = _speedtree_material_name_key(group_base)
        raw_candidates = candidates_by_group.get(group_key, [])
        consensus_candidates = {}
        for candidate in raw_candidates:
            source_identity = str(
                candidate.get("source_identity")
                or candidate.get("source_fbx")
                or ""
            ).strip()
            try:
                source_key = normalized_source_fbx_path(source_identity)
            except (OSError, RuntimeError, ValueError):
                source_key = source_identity.casefold()
            signature = _speedtree_texture_file_signature(
                candidate["texture_set"]
            )
            consensus_candidates.setdefault(
                (source_key, signature), candidate
            )
        candidates = list(consensus_candidates.values())
        if len(candidates) != 1:
            before = material_texture_signature(material)
            removed = _remove_speedtree_image_nodes(material)
            metadata_cleared = (
                _clear_speedtree_texture_binding_properties(material)
            )
            rows.append(
                {
                    "material": material.name,
                    "status": "unassigned",
                    "binding_disposition": "leave_unassigned",
                    "production_group_base": group_base,
                    "candidate_count": len(candidates),
                    "raw_candidate_count": len(raw_candidates),
                    "blocked_source_parts": blocked_source,
                    "blocked_image_parts": blocked_images,
                    "diagnostic_codes": [
                        "unsafe_texture_path_quarantined"
                    ],
                    "message": (
                        "Unsafe isolated texture paths were removed; no unique "
                        "safe replacement was available"
                    ),
                    "changed": (
                        bool(removed) or bool(before) or metadata_cleared
                    ),
                }
            )
            continue
        candidate = candidates[0]
        texture_set = candidate["texture_set"]
        texture_files = {
            role: Path(path)
            for role, path in texture_set["files"].items()
        }
        before = material_texture_signature(material)
        load_result = _replace_speedtree_material_nodes(
            material,
            texture_files,
            tolerate_load_errors=True,
            texture_file_candidates=(
                texture_set.get("file_candidates") or {}
            ),
        )
        loaded_roles = list(load_result["loaded_roles"])
        texture_files = {
            role: Path(path)
            for role, path in load_result["selected_files"].items()
        }
        failed_roles = list(load_result["failed_roles"])
        missing_roles = sorted(
            set(texture_set.get("missing_roles") or [])
            | set(failed_roles)
        )
        metadata_cleared = False
        if loaded_roles:
            material["codex_source_fbx"] = candidate["source_fbx"]
            if candidate["source_identity"]:
                material["codex_source_identity"] = candidate[
                    "source_identity"
                ]
            material["codex_speedtree_texture_base"] = texture_set[
                "texture_base"
            ]
        else:
            metadata_cleared = (
                _clear_speedtree_texture_binding_properties(material)
            )
        row_status = (
            "rebound"
            if not missing_roles
            else "partial"
            if loaded_roles
            else "unassigned"
        )
        rows.append(
            {
                "material": material.name,
                "status": row_status,
                "binding_disposition": (
                    "bind_available"
                    if loaded_roles
                    else "leave_unassigned"
                ),
                "production_group_base": group_base,
                "canonical_material": candidate["material"].name,
                "texture_base": texture_set["texture_base"],
                "source_fbx": candidate["source_fbx"],
                "available_roles": loaded_roles,
                "missing_roles": missing_roles,
                "texture_load_errors": dict(
                    load_result["load_errors"]
                ),
                "diagnostic_codes": (
                    ["texture_image_load_failed"]
                    if failed_roles
                    else ["texture_roles_unassigned"]
                    if missing_roles
                    else []
                ),
                "warning": (
                    "One or more safe replacement textures could not be "
                    "loaded; those parameters remain unassigned"
                    if failed_roles
                    else ""
                ),
                "changed": (
                    before != material_texture_signature(material)
                    or metadata_cleared
                ),
            }
        )

    unresolved = [
        row
        for row in rows
        if row["status"] in {"partial", "unassigned"}
    ]
    return {
        "status": "ok",
        "texture_outcome": (
            "partial"
            if any(row["status"] == "partial" for row in unresolved)
            else "unassigned"
            if unresolved
            else "complete"
        ),
        "materials": rows,
        "rebound_count": sum(
            1 for row in rows if row["status"] == "rebound"
        ),
        "unresolved": unresolved,
        "blocking": [],
        "warnings": [
            {
                "code": (
                    "texture_image_load_failed"
                    if row.get("texture_load_errors")
                    else "unsafe_texture_path_quarantined"
                ),
                "severity": "warning",
                "material": row["material"],
                "message": row["warning"],
            }
            for row in unresolved
            if row.get("texture_load_errors")
        ],
    }


def material_group_token(material):
    _base_name, group_suffix = _production_group_parts(
        material.name if material else ""
    )
    return group_suffix


def material_base_name(material):
    if material is None:
        return ""
    base_name, _group_suffix = _production_group_parts(material.name)
    return base_name


def material_texture_signature(material):
    if material is None or not material.use_nodes or not material.node_tree:
        return ()
    paths = []
    for node in material.node_tree.nodes:
        if node.type != "TEX_IMAGE" or not node.image:
            continue
        filepath = node.image.filepath_raw or node.image.filepath
        if not filepath:
            continue
        try:
            paths.append(str(Path(bpy.path.abspath(filepath)).resolve()).lower())
        except (OSError, ValueError):
            paths.append(bpy.path.abspath(filepath).lower())
    return tuple(sorted(set(paths)))


def unified_material_name(base_name, materials):
    if base_name:
        return base_name
    return "SpeedTree_Atlas_Material"


def remap_mesh_materials(mesh, slot_map, new_materials):
    remapped = []
    for poly in mesh.polygons:
        remapped.append(slot_map.get(poly.material_index, 0))
    mesh.materials.clear()
    for material in new_materials:
        mesh.materials.append(material)
    for poly, material_index in zip(mesh.polygons, remapped):
        poly.material_index = material_index
    mesh.update()


def _consolidate_speedtree_manifest_materials(
    mesh_objects, *, runtime_tolerant=False
):
    """Collapse Atlas Leaf Mesh Builder groups using its persisted manifest contract."""
    stmat_cache = {}
    manifest_cache = {}
    bindings = {}
    grouped = defaultdict(list)

    for obj in mesh_objects:
        for material in obj.data.materials:
            if material is None or material in bindings:
                continue
            source_fbx = str(material.get("codex_source_fbx", "")).strip()
            if not source_fbx:
                bindings[material] = None
                continue
            source_key = normalized_source_fbx_path(source_fbx)
            if source_key not in stmat_cache:
                stmat_cache[source_key] = _speedtree_stmat_materials(source_fbx)
            binding = _speedtree_manifest_binding(
                source_fbx,
                material,
                stmat_data=stmat_cache[source_key],
                manifest_cache=manifest_cache,
                # This pass establishes slot/material identity. Texture
                # availability is handled later and must not authorize or
                # veto the structural merge.
                validate_texture_compatibility=False,
                tolerate_unavailable=runtime_tolerant,
            )
            bindings[material] = binding
            if binding:
                group_key = (
                    source_key,
                    binding["target_name"],
                    binding["manifest_path"],
                    binding.get("export_scope_id", ""),
                )
                grouped[group_key].append(material)

    if not grouped:
        return {"groups": [], "changed_object_count": 0, "changed_face_count": 0}

    target_materials = {}
    material_targets = {}
    group_stats = {}
    source_material_pool = collect_object_materials(mesh_objects)
    target_material_pool = list(source_material_pool)
    target_material_pool.extend(
        material for material in bpy.data.materials if material not in target_material_pool
    )
    for group_key, source_materials in grouped.items():
        source_key, target_name, manifest_path, export_scope_id = group_key
        unique_sources = []
        seen = set()
        for material in source_materials:
            if material.name not in seen:
                seen.add(material.name)
                unique_sources.append(material)

        target_material = next(
            (
                material
                for material in target_material_pool
                if _speedtree_material_name_key(material.name)
                == _speedtree_material_name_key(target_name)
                and normalized_source_fbx_path(material.get("codex_source_fbx", "")) == source_key
            ),
            None,
        )
        if target_material is None:
            target_material = unique_sources[0].copy()
            target_material.name = target_name
        target_material["codex_source_fbx"] = str(unique_sources[0].get("codex_source_fbx", ""))
        target_material["codex_speedtree_consolidated_from"] = [
            material.name for material in unique_sources
        ]
        target_material["codex_speedtree_import_manifest"] = manifest_path
        if export_scope_id:
            target_material["codex_speedtree_export_scope_id"] = export_scope_id
        target_materials[group_key] = target_material
        for material in unique_sources:
            material_targets[material] = (group_key, target_material)
        group_stats[group_key] = {
            "objects": set(),
            "changed_faces": 0,
            "source_materials": [material.name for material in unique_sources],
            "groups": sorted(
                {
                    bindings[material].get("group", "")
                    for material in unique_sources
                    if bindings.get(material)
                }
                - {""}
            ),
        }

    for obj in mesh_objects:
        old_materials = list(obj.data.materials)
        if not any(material in material_targets for material in old_materials if material):
            continue
        if obj.data.users > 1:
            obj.data = obj.data.copy()
        mesh = obj.data
        old_materials = list(mesh.materials)
        slot_groups = {}
        if any(material is None for material in old_materials):
            # Preserve authored empty slots and their polygon assignments so
            # the structural validator can report them. Only replace slots
            # whose manifest identity was proven.
            for old_index, material in enumerate(old_materials):
                group_target = material_targets.get(material)
                if group_target:
                    mesh.materials[old_index] = group_target[1]
                    slot_groups[old_index] = group_target[0]
            for poly in mesh.polygons:
                group_key = slot_groups.get(poly.material_index)
                if group_key:
                    group_stats[group_key]["changed_faces"] += 1
            for group_key in set(slot_groups.values()):
                group_stats[group_key]["objects"].add(obj.name)
            continue

        slot_map = {}
        new_materials = []
        new_indices = {}
        for old_index, material in enumerate(old_materials):
            group_target = material_targets.get(material)
            replacement = group_target[1] if group_target else material
            if replacement is None:
                slot_map[old_index] = 0
                continue
            replacement_key = replacement.name
            if replacement_key not in new_indices:
                new_indices[replacement_key] = len(new_materials)
                new_materials.append(replacement)
            slot_map[old_index] = new_indices[replacement_key]
            if group_target:
                slot_groups[old_index] = group_target[0]
                group_stats[group_target[0]]["objects"].add(obj.name)
        for poly in mesh.polygons:
            group_key = slot_groups.get(poly.material_index)
            if group_key:
                group_stats[group_key]["changed_faces"] += 1
        remap_mesh_materials(mesh, slot_map, new_materials)

    reports = []
    for group_key, target_material in target_materials.items():
        _source_key, _target_name, manifest_path, export_scope_id = group_key
        stats = group_stats[group_key]
        reports.append(
            {
                "mode": "speedtree_import_manifest",
                "target_material": target_material.name,
                "source_materials": stats["source_materials"],
                "group_tokens": stats["groups"],
                "manifest": manifest_path,
                "export_scope_id": export_scope_id,
                "object_count": len(stats["objects"]),
                "objects": sorted(stats["objects"])[:200],
                "changed_faces": stats["changed_faces"],
            }
        )
    return {
        "groups": reports,
        "changed_object_count": sum(group["object_count"] for group in reports),
        "changed_face_count": sum(group["changed_faces"] for group in reports),
    }


def _strict_consolidation_intent_signature(material, texture_contract):
    """Return exact structural proof for one production-group material.

    Texture availability is intentionally absent.  A strict envelope may
    authorize consolidation only when this exact Blender material has one
    unambiguous intent and its production-group identity and Unreal semantics
    agree.
    """
    envelope = texture_contract.get("speedtree_pipeline_contract")
    if not isinstance(envelope, dict):
        return ()
    api = handoff_contract.central_contract_api()
    material_key = api.normalize_material_key(material.name)
    candidates = [
        row
        for row in (envelope.get("material_intents") or [])
        if isinstance(row, dict)
        and api.normalize_material_key(row.get("material_key")) == material_key
    ]
    if len(candidates) != 1:
        return ()
    intent = candidates[0]
    computed_base = api.production_group_base_name(material.name)
    group_tokens = api.production_group_tokens(material.name)
    recorded_base = str(intent.get("production_group_base") or "").strip()
    if (
        not computed_base
        or not group_tokens
        or api.normalize_material_key(recorded_base)
        != api.normalize_material_key(computed_base)
    ):
        return ()
    return (
        api.normalize_material_key(computed_base),
        str(intent.get("tree_part") or ""),
        str(intent.get("tree_shading") or ""),
        str(intent.get("instance_profile") or ""),
    )


def _strict_numeric_material_intent_signature(material, texture_contract):
    """Return exact semantic proof for a Blender ``.001`` collision.

    Numeric suffixes are not production-group tokens, so they need a smaller
    exact-intent proof than production-group consolidation. Texture fields are
    deliberately excluded.
    """
    envelope = texture_contract.get("speedtree_pipeline_contract")
    if not isinstance(envelope, dict):
        return ()
    api = handoff_contract.central_contract_api()
    material_key = api.normalize_material_key(material.name)
    candidates = [
        row
        for row in (envelope.get("material_intents") or [])
        if isinstance(row, dict)
        and api.normalize_material_key(row.get("material_key")) == material_key
    ]
    if len(candidates) != 1:
        return ()
    intent = candidates[0]
    return (
        material_key,
        str(intent.get("tree_part") or ""),
        str(intent.get("tree_shading") or ""),
        str(intent.get("instance_profile") or ""),
    )


def _blender_numeric_material_base(value):
    name = str(value or "").strip()
    return re.sub(r"(\.\d{3})$", "", name)


def _numeric_material_equivalence(left, right, texture_contract):
    """Return the proof that two Blender-collision materials are identical.

    A ``.001`` suffix is never accepted as an Unreal export name by itself.
    It only identifies a candidate pair.  The actual merge still requires
    shared source provenance, one strict managed texture binding, or the exact
    same non-empty image-file signature.
    """
    if left is None or right is None:
        return ""
    left_source = normalized_source_fbx_path(
        left.get("codex_source_fbx", "")
    )
    right_source = normalized_source_fbx_path(
        right.get("codex_source_fbx", "")
    )
    if (left_source or right_source) and left_source != right_source:
        # A same-named datablock left behind by another imported tree is never
        # a canonical target for this source, even when its semantic intent
        # happens to match.
        return ""

    if (
        isinstance(texture_contract, dict)
        and texture_contract.get("strict_speedtree_pipeline_contract")
    ):
        left_binding = _strict_numeric_material_intent_signature(
            left, texture_contract
        )
        right_binding = _strict_numeric_material_intent_signature(
            right, texture_contract
        )
        if left_binding and left_binding == right_binding:
            return "strict_texture_contract"

    if left_source and left_source == right_source:
        return "source_fbx"

    left_textures = material_texture_signature(left)
    right_textures = material_texture_signature(right)
    if left_textures and left_textures == right_textures:
        return "texture_signature"
    return ""


def _consolidate_blender_numeric_material_duplicates(
    mesh_objects, texture_contract=None
):
    """Remap proven ``Material``/``Material.001`` collisions to one datablock."""
    used_materials = []
    seen_materials = set()
    for obj in mesh_objects:
        for material in obj.data.materials:
            if material is None or material in seen_materials:
                continue
            seen_materials.add(material)
            used_materials.append(material)

    # A previous rebuild can remove the original ``Material`` datablock after
    # Blender has already named the replacement ``Material.001``.  With no
    # canonical peer left there is nothing to merge, but keeping the numeric
    # collision suffix would publish the stale Blender accident as an Unreal
    # material-slot identity.  A single used survivor is therefore the
    # canonical material and can be renamed without changing any slot, face,
    # texture, or provenance.
    suffixed_by_base = defaultdict(list)
    for material in used_materials:
        base_name = _blender_numeric_material_base(material.name)
        if base_name != material.name:
            suffixed_by_base[base_name.casefold()].append(material)
    canonical_keys = {
        material.name.casefold()
        for material in bpy.data.materials
        if _blender_numeric_material_base(material.name) == material.name
    }
    orphan_renames = []
    for base_key, materials in sorted(suffixed_by_base.items()):
        if base_key in canonical_keys or len(materials) != 1:
            continue
        material = materials[0]
        old_name = material.name
        target_name = _blender_numeric_material_base(old_name)
        objects = sorted(
            obj.name
            for obj in mesh_objects
            if material in obj.data.materials[:]
        )
        material.name = target_name
        orphan_renames.append(
            {
                "mode": "blender_orphan_numeric_suffix",
                "target_material": material.name,
                "source_materials": [old_name],
                "proofs": ["single_used_survivor"],
                "object_count": len(objects),
                "objects": objects[:200],
                "changed_faces": 0,
                "removed_source_materials": [],
            }
        )
        canonical_keys.add(material.name.casefold())

    numeric_bases = {
        _blender_numeric_material_base(material.name).casefold()
        for material in used_materials
        if _blender_numeric_material_base(material.name) != material.name
    }

    if not numeric_bases:
        return {
            "groups": orphan_renames,
            "skipped_groups": [],
            "changed_object_count": sum(
                group["object_count"] for group in orphan_renames
            ),
            "changed_face_count": 0,
        }

    all_materials = list(used_materials)
    all_materials.extend(
        material
        for material in bpy.data.materials
        if material not in seen_materials
    )
    targets_by_base = defaultdict(list)
    for material in all_materials:
        base_name = _blender_numeric_material_base(material.name)
        if (
            base_name == material.name
            and base_name.casefold() in numeric_bases
        ):
            targets_by_base[base_name.casefold()].append(material)

    material_targets = {}
    proofs = {}
    skipped_groups = []
    for material in used_materials:
        base_name = _blender_numeric_material_base(material.name)
        if base_name == material.name:
            continue
        candidates = []
        for target in targets_by_base.get(base_name.casefold(), []):
            proof = _numeric_material_equivalence(
                material, target, texture_contract
            )
            if proof:
                candidates.append((target, proof))
        if len(candidates) == 1:
            target, proof = candidates[0]
            material_targets[material] = target
            proofs[material] = proof
            continue
        skipped_groups.append(
            {
                "mode": "blender_numeric_collision",
                "target_material": base_name,
                "source_materials": [material.name],
                "reason": (
                    "no proven canonical material match"
                    if not candidates
                    else "multiple proven canonical material matches"
                ),
            }
        )

    if not material_targets:
        return {
            "groups": orphan_renames,
            "skipped_groups": skipped_groups,
            "changed_object_count": sum(
                group["object_count"] for group in orphan_renames
            ),
            "changed_face_count": 0,
        }

    changed_objects = defaultdict(set)
    changed_faces = defaultdict(int)
    for obj in mesh_objects:
        old_materials = list(obj.data.materials)
        if not any(material in material_targets for material in old_materials):
            continue
        if obj.data.users > 1:
            obj.data = obj.data.copy()
        mesh = obj.data
        old_materials = list(mesh.materials)
        replacements = [
            material_targets.get(material, material)
            for material in old_materials
        ]
        for polygon in mesh.polygons:
            if old_materials[polygon.material_index] in material_targets:
                changed_faces[
                    material_targets[old_materials[polygon.material_index]]
                ] += 1

        if any(material is None for material in replacements):
            # Preserve empty-slot indices for the later explicit preflight.
            # Only replace the proven collision slots in place.
            for index, replacement in enumerate(replacements):
                if old_materials[index] in material_targets:
                    mesh.materials[index] = replacement
        else:
            slot_map = {}
            new_materials = []
            new_indices = {}
            for old_index, replacement in enumerate(replacements):
                if replacement not in new_indices:
                    new_indices[replacement] = len(new_materials)
                    new_materials.append(replacement)
                slot_map[old_index] = new_indices[replacement]
            remap_mesh_materials(mesh, slot_map, new_materials)

        for source, target in material_targets.items():
            if source in old_materials:
                changed_objects[target].add(obj.name)

    reports = []
    for target, objects in changed_objects.items():
        sources = [
            source
            for source, candidate in material_targets.items()
            if candidate == target
        ]
        reports.append(
            {
                "mode": "blender_numeric_collision",
                "target_material": target.name,
                "source_materials": sorted(
                    source.name for source in sources
                ),
                "proofs": sorted({proofs[source] for source in sources}),
                "object_count": len(objects),
                "objects": sorted(objects)[:200],
                "changed_faces": changed_faces[target],
            }
        )
    removed_source_materials = []
    for source in list(material_targets):
        if source.users != 0:
            continue
        removed_source_materials.append(source.name)
        bpy.data.materials.remove(source)
    if removed_source_materials:
        for report in reports:
            report["removed_source_materials"] = sorted(
                name
                for name in removed_source_materials
                if _blender_numeric_material_base(name).casefold()
                == report["target_material"].casefold()
            )
    return {
        "groups": orphan_renames + reports,
        "skipped_groups": skipped_groups,
        "changed_object_count": sum(
            group["object_count"] for group in reports
        ) + sum(group["object_count"] for group in orphan_renames),
        "changed_face_count": sum(
            group["changed_faces"] for group in reports
        ),
    }


def consolidate_speedtree_group_materials(objects, texture_contract=None):
    # Atlas Builder child collections become numeric-boundary material suffixes.
    # Collapse proven shared-source slots before merge/weight export; do not
    # touch object transforms, UVs, vertex groups, or weights.
    mesh_objects = [obj for obj in objects if obj.type == "MESH" and obj.data]
    if not mesh_objects:
        return {
            "status": "skipped",
            "reason": "no mesh objects",
            "groups": [],
            "skipped_groups": [],
        }

    strict_contract = bool(
        isinstance(texture_contract, dict)
        and texture_contract.get("strict_speedtree_pipeline_contract")
    )
    numeric_result = _consolidate_blender_numeric_material_duplicates(
        mesh_objects, texture_contract=texture_contract
    )
    # The validated contract is authoritative. Legacy manifest consolidation
    # must not mutate a strict scene before its binding signatures are checked.
    manifest_result = (
        {"groups": [], "changed_object_count": 0, "changed_face_count": 0}
        if strict_contract
        else _consolidate_speedtree_manifest_materials(
            mesh_objects,
            runtime_tolerant=_runtime_tolerant_texture_contract(
                texture_contract
            ),
        )
    )
    skipped_groups = list(numeric_result["skipped_groups"])

    grouped = defaultdict(list)
    for obj in mesh_objects:
        for slot_index, material in enumerate(obj.data.materials):
            group_token = material_group_token(material)
            if not group_token:
                continue
            base_name = material_base_name(material)
            source_key = normalized_source_fbx_path(
                material.get("codex_source_fbx", "") if material else ""
            )
            texture_signature = material_texture_signature(material)
            if strict_contract:
                intent_signature = _strict_consolidation_intent_signature(
                    material, texture_contract
                )
                if not intent_signature:
                    continue
                provenance = (
                    "material_intent",
                    (source_key, intent_signature),
                )
            else:
                # Legacy/name-only metadata cannot prove that two variant
                # slots share one structural material identity.  A validated
                # import manifest was handled above; otherwise leave the
                # variants intact and let the pipeline continue.
                continue
            grouped[(provenance, base_name)].append(
                (obj, slot_index, material, group_token)
            )

    reports = []
    for (provenance, base_name), entries in grouped.items():
        provenance_type, provenance_value = provenance
        group_tokens = sorted({entry[3] for entry in entries})
        # A single Atlas Builder child material is still a group variant.  In
        # particular, a source FBX can contain only ``*_atlas_01_green`` while
        # its strict intent records ``*_atlas_01`` as the production-group
        # base.  Requiring two different suffixes left that proven dummy name
        # on the exported prototype and leaked it into Unreal Assembly slots.
        if not group_tokens:
            continue
        source_materials = []
        seen_materials = set()
        for _obj, _slot_index, material, _token in entries:
            if material and material.name not in seen_materials:
                seen_materials.add(material.name)
                source_materials.append(material)
        if not source_materials:
            continue

        texture_signatures = sorted({material_texture_signature(material) for material in source_materials})
        target_name = unified_material_name(base_name, source_materials)
        if strict_contract:
            try:
                target_intent = handoff_contract.resolve_material_intent(
                    target_name,
                    texture_contract["speedtree_pipeline_contract"],
                )
            except RuntimeError as exc:
                # Two differently classified child groups can share the same
                # textual base.  With no exact base intent, that ambiguity is
                # evidence to preserve both source slots, not to collapse one
                # just because it is the only member of its semantic group.
                if (
                    len(group_tokens) == 1
                    and "Conflicting SpeedTree material intents" in str(exc)
                ):
                    skipped_groups.append(
                        {
                            "mode": "production_group_suffix",
                            "target_material": target_name,
                            "source_materials": [
                                material.name for material in source_materials
                            ],
                            "reason": "conflicting production-group intents",
                        }
                    )
                    continue
                raise
            expected_semantics = tuple(provenance_value[1][1:])
            target_semantics = (
                str((target_intent or {}).get("tree_part") or ""),
                str((target_intent or {}).get("tree_shading") or ""),
                str((target_intent or {}).get("instance_profile") or ""),
            )
            if target_intent is None or target_semantics != expected_semantics:
                raise RuntimeError(
                    "SpeedTree production-group target intent conflicts with "
                    f"its source variants: {target_name}"
                )
            target_proof = json.dumps(
                {
                    "production_group_base": provenance_value[1][0],
                    "tree_part": target_semantics[0],
                    "tree_shading": target_semantics[1],
                    "instance_profile": target_semantics[2],
                },
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=True,
            )
        else:
            target_proof = ""
        readiness_mode = provenance_type
        if provenance_type == "material_intent":
            target_material = next(
                (
                    candidate
                    for candidate in source_materials
                    if _speedtree_material_name_key(candidate.name)
                    == _speedtree_material_name_key(target_name)
                ),
                None,
            )
            if target_material is None:
                # The Assembly builder can normalize multiple exact provider
                # contracts in one Blender scene. Reuse only a suffix-free
                # material previously created from the same strict production
                # identity; never merge an untagged name match.
                target_material = next(
                    (
                        candidate
                        for candidate in bpy.data.materials
                        if _speedtree_material_name_key(candidate.name)
                        == _speedtree_material_name_key(target_name)
                        and str(
                            candidate.get(
                                "codex_speedtree_consolidation_target_proof",
                                "",
                            )
                        )
                        == target_proof
                    ),
                    None,
                )
        else:
            target_material = next(
                (
                    candidate
                    for candidate in bpy.data.materials
                    if _speedtree_material_name_key(candidate.name)
                    == _speedtree_material_name_key(target_name)
                    and (
                        normalized_source_fbx_path(
                            candidate.get("codex_source_fbx", "")
                        )
                        == provenance_value
                        if provenance_type == "source_fbx"
                        else material_texture_signature(candidate)
                        == provenance_value
                    )
                ),
                None,
            )
        if target_material is None:
            target_material = source_materials[0].copy()
            target_material.name = target_name
            target_material["codex_speedtree_consolidated_from"] = [material.name for material in source_materials]
        if strict_contract:
            target_material[
                "codex_speedtree_consolidation_target_proof"
            ] = target_proof

        slots_by_object = defaultdict(list)
        for obj, slot_index, _material, _token in entries:
            slots_by_object[obj].append(slot_index)

        changed_objects = []
        changed_faces = 0
        for obj, candidate_slots in slots_by_object.items():
            if obj.data.users > 1:
                obj.data = obj.data.copy()
            mesh = obj.data
            candidate_slots = set(candidate_slots)
            for poly in mesh.polygons:
                if poly.material_index in candidate_slots:
                    changed_faces += 1
            if any(material is None for material in mesh.materials):
                # Preserve every empty slot and its face assignments for the
                # later structural validator. Only replace proven group slots
                # in place; never turn an authored None into the target.
                for slot_index in candidate_slots:
                    mesh.materials[slot_index] = target_material
            else:
                slot_map = {}
                new_materials = [target_material]
                non_candidate_indices = {}
                for old_index, material in enumerate(mesh.materials):
                    if old_index in candidate_slots:
                        slot_map[old_index] = 0
                        continue
                    if material.name not in non_candidate_indices:
                        non_candidate_indices[material.name] = len(new_materials)
                        new_materials.append(material)
                    slot_map[old_index] = non_candidate_indices[material.name]
                remap_mesh_materials(mesh, slot_map, new_materials)
            obj["codex_speedtree_unified_material"] = target_material.name
            changed_objects.append(obj.name)

        reports.append(
            {
                "mode": "production_group_suffix",
                "target_material": target_material.name,
                "source_materials": [material.name for material in source_materials],
                "group_tokens": group_tokens,
                "provenance_type": provenance_type,
                "source_fbx": str(
                    source_materials[0].get("codex_source_fbx", "")
                ),
                "readiness_mode": readiness_mode,
                "texture_signatures": [list(signature) for signature in texture_signatures],
                "object_count": len(changed_objects),
                "objects": changed_objects[:200],
                "changed_faces": changed_faces,
            }
        )

    all_reports = (
        list(numeric_result["groups"])
        + list(manifest_result["groups"])
        + reports
    )
    return {
        "status": "applied" if all_reports else "skipped",
        "groups": all_reports,
        "skipped_groups": skipped_groups,
        "changed_object_count": (
            numeric_result["changed_object_count"]
            + manifest_result["changed_object_count"]
            + sum(group["object_count"] for group in reports)
        ),
        "changed_face_count": (
            numeric_result["changed_face_count"]
            + manifest_result["changed_face_count"]
            + sum(group["changed_faces"] for group in reports)
        ),
    }


def apply_speedtree_material_intents(objects, texture_contract=None):
    """Apply explicit tree classification after material consolidation.

    New-schema contracts are matched only by the central exact material key or
    the central production-group base.  The unrelated PCG Atlas Auto Split
    namespace is intentionally not consulted here.
    """
    if not isinstance(texture_contract, dict) or not texture_contract.get(
        "strict_speedtree_pipeline_contract"
    ):
        return {
            "status": "legacy_fallback",
            "materials": [],
            "changed_materials": [],
        }
    envelope = texture_contract.get("speedtree_pipeline_contract")
    if not isinstance(envelope, dict):
        raise RuntimeError("Strict SpeedTree texture contract has no envelope")

    rows = []
    changed = []
    unmatched = []
    diagnostics = []
    resolved_materials = []
    for material in collect_object_materials(objects):
        intent = handoff_contract.resolve_material_intent(
            material.name, envelope
        )
        if intent is None:
            unmatched.append(material.name)
            continue
        resolved_materials.append((material, intent))

    if unmatched:
        raise RuntimeError(
            "SpeedTree material intent has no exact/canonical match after "
            "consolidation: " + ", ".join(sorted(unmatched, key=str.casefold))
        )

    for material, intent in resolved_materials:
        changed_properties = []
        tree_part = intent.get("tree_part")
        tree_shading = intent.get("tree_shading")
        if tree_part and material.get(UNREAL_TREE_PART_PROPERTY) != tree_part:
            material[UNREAL_TREE_PART_PROPERTY] = tree_part
            changed_properties.append(UNREAL_TREE_PART_PROPERTY)
        if (
            tree_shading
            and material.get(UNREAL_TREE_SHADING_PROPERTY) != tree_shading
        ):
            material[UNREAL_TREE_SHADING_PROPERTY] = tree_shading
            changed_properties.append(UNREAL_TREE_SHADING_PROPERTY)
        if changed_properties:
            changed.append(material.name)
        rows.append(
            {
                **intent,
                "changed_properties": changed_properties,
            }
        )

    return {
        "status": "applied" if rows else "not_applicable",
        "materials": rows,
        "changed_materials": changed,
        "unmatched_materials": sorted(unmatched, key=str.casefold),
        "diagnostics": diagnostics,
        "warnings": [],
        "blocking": [],
    }


def build_rigid_fallback_armature(objects, armature_name="Root", bone_name="Bone_1_Start"):
    if any(obj.type == "ARMATURE" for obj in objects):
        return None
    meshes = [obj for obj in objects if obj.type == "MESH" and obj.data and len(obj.data.vertices) > 0]
    if not meshes:
        return None

    world_points = [obj.matrix_world @ Vector(corner) for obj in meshes for corner in obj.bound_box]
    low = Vector((min(point.x for point in world_points), min(point.y for point in world_points), min(point.z for point in world_points)))
    high = Vector((max(point.x for point in world_points), max(point.y for point in world_points), max(point.z for point in world_points)))
    center = (low + high) * 0.5
    height = max(high.z - low.z, 0.1)

    armature_data = bpy.data.armatures.new(armature_name)
    armature = bpy.data.objects.new(armature_name, armature_data)
    bpy.context.scene.collection.objects.link(armature)
    ensure_object_mode()
    deselect_all()
    bpy.context.view_layer.objects.active = armature
    armature.select_set(True)
    bpy.ops.object.mode_set(mode="EDIT")
    edit_bone = armature.data.edit_bones.new(bone_name)
    edit_bone.head = (center.x, center.y, low.z)
    edit_bone.tail = (center.x, center.y, low.z + height)
    bpy.ops.object.mode_set(mode="OBJECT")

    for obj in meshes:
        group = obj.vertex_groups.get(bone_name) or obj.vertex_groups.new(name=bone_name)
        group.add(list(range(len(obj.data.vertices))), 1.0, "REPLACE")
        parent_keep_world(obj, armature)
        modifier = next((item for item in obj.modifiers if item.type == "ARMATURE"), None)
        if modifier is None:
            modifier = obj.modifiers.new(name=armature_name, type="ARMATURE")
        modifier.object = armature

    return {
        "armature": armature.name,
        "bone": bone_name,
        "meshes": [obj.name for obj in meshes],
        "reason": "SpeedTree FBX contained geometry but no armature",
    }


CLUSTER_XML_SCALE_CANDIDATES = (100.0, 1.0, 3.28084, 30.48, 0.01)
CLUSTER_AXIS_BONE_RE = re.compile(
    r"^Bone_(\d+)_(Start|End)$",
    flags=re.IGNORECASE,
)


def _cluster_xml_number(value, label):
    text = str("" if value is None else value).strip()
    if not text:
        raise RuntimeError(f"Cluster XML {label} is empty.")
    if "," in text:
        if "." in text or text.count(",") != 1:
            raise RuntimeError(
                f"Cluster XML {label} has ambiguous decimal separators: {text!r}"
            )
        text = text.replace(",", ".")
    number = float(text)
    if not math.isfinite(number):
        raise RuntimeError(f"Cluster XML {label} is not finite: {value!r}")
    return number


def _cluster_xml_structural_roots(xml_path):
    path = Path(str(xml_path or "")).expanduser()
    if not path.is_file():
        raise RuntimeError(
            "Cluster source skin normalization requires the matching Raw XML: "
            f"{path}"
        )
    try:
        root = ET.parse(path).getroot()
    except (ET.ParseError, OSError) as exc:
        raise RuntimeError(f"Cluster Raw XML could not be parsed: {path} ({exc})") from exc

    bones = []
    for element in root.iter("Bone"):
        attrs = element.attrib
        required = (
            "ID",
            "ParentID",
            "StartX",
            "StartY",
            "StartZ",
            "EndX",
            "EndY",
            "EndZ",
        )
        missing = [name for name in required if name not in attrs]
        if missing:
            raise RuntimeError(
                "Cluster Raw XML Bone is missing required attributes: "
                + ", ".join(missing)
            )
        bones.append(
            {
                "id": int(attrs["ID"]),
                "parent_id": int(attrs["ParentID"]),
                "generator": str(attrs.get("Generator") or ""),
                "start_raw": Vector(
                    (
                        _cluster_xml_number(attrs["StartX"], "StartX"),
                        _cluster_xml_number(attrs["StartY"], "StartY"),
                        _cluster_xml_number(attrs["StartZ"], "StartZ"),
                    )
                ),
                "end_raw": Vector(
                    (
                        _cluster_xml_number(attrs["EndX"], "EndX"),
                        _cluster_xml_number(attrs["EndY"], "EndY"),
                        _cluster_xml_number(attrs["EndZ"], "EndZ"),
                    )
                ),
            }
        )
    if not bones:
        raise RuntimeError(f"Cluster Raw XML contains no Bone entries: {path}")
    roots = [bone for bone in bones if bone["parent_id"] == -1]
    if not roots:
        raise RuntimeError(
            f"Cluster Raw XML contains no structural root (ParentID=-1): {path}"
        )
    return roots


def _cluster_geometry_scale(meshes):
    points = [
        obj.matrix_world @ Vector(corner)
        for obj in meshes
        for corner in obj.bound_box
    ]
    if not points:
        return 0.0
    return max(
        max(point[axis] for point in points)
        - min(point[axis] for point in points)
        for axis in range(3)
    )


def _cluster_named_axis_root_contract(axis_ordinals, roots):
    """Map FBX Bone_N identities to XML ID N-1 with B as a subset of A."""
    ordinals = sorted({int(value) for value in axis_ordinals})
    if not ordinals or any(ordinal <= 0 for ordinal in ordinals):
        raise RuntimeError(
            "Cluster axis bone ordinals must be positive; found "
            f"{ordinals}."
        )
    roots_by_id = {}
    for root in roots:
        root_id = int(root["id"])
        if root_id in roots_by_id:
            raise RuntimeError(
                f"Cluster Raw XML contains duplicate structural root ID {root_id}."
            )
        roots_by_id[root_id] = root
    requested_xml_ids = {ordinal - 1 for ordinal in ordinals}
    missing_xml_ids = sorted(requested_xml_ids.difference(roots_by_id))
    if missing_xml_ids:
        raise RuntimeError(
            "Cluster FBX axis names reference structural XML root IDs that do "
            f"not exist: {missing_xml_ids}."
        )
    return {
        "roots_by_ordinal": {
            ordinal: roots_by_id[ordinal - 1] for ordinal in ordinals
        },
        "unused_xml_root_ids": sorted(
            set(roots_by_id).difference(requested_xml_ids)
        ),
    }


def _canonicalize_cluster_axis_bones(armature, meshes, xml_path):
    """Replace SpeedTree Start/End markers with one real axis bone per XML root.

    Stage 1 authors Raw XML structural roots only for the first *renderable*
    Branch below each Tree root. Meshless Trunk/Branch placement splines are
    therefore absent from this contract. Imported ``Bone_N_Start/End`` names
    resolve only to XML root ID ``N - 1``. Extra XML roots are valid; geometry
    coordinates diagnose export drift and select units, but never select bone
    identity.
    """
    roots = _cluster_xml_structural_roots(xml_path)
    axes = {}
    unexpected = []
    for bone in armature.data.bones:
        match = CLUSTER_AXIS_BONE_RE.fullmatch(bone.name)
        if match is None:
            unexpected.append(bone.name)
            continue
        ordinal = int(match.group(1))
        role = match.group(2).casefold()
        row = axes.setdefault(ordinal, {"ordinal": ordinal})
        if role in row:
            raise RuntimeError(
                f"Cluster armature has duplicate {role} axis bone ordinal {ordinal}."
            )
        row[role] = bone
    if unexpected:
        raise RuntimeError(
            "Cluster armature contains non-axis bones after Stage 1 calibration: "
            + ", ".join(sorted(unexpected))
        )
    if not axes:
        raise RuntimeError(
            "Cluster armature contains no numbered Start/End axis bones."
        )
    named_root_contract = _cluster_named_axis_root_contract(axes, roots)
    roots_by_ordinal = named_root_contract["roots_by_ordinal"]

    named_pairs = []
    for ordinal in sorted(axes):
        axis = axes[ordinal]
        start_bone = axis.get("start")
        end_bone = axis.get("end")
        if start_bone is None and end_bone is None:
            raise RuntimeError(f"Cluster axis {ordinal} has no Start or End marker.")
        if start_bone is not None:
            source_start = armature.matrix_world @ start_bone.head_local
            source_end = (
                armature.matrix_world @ end_bone.head_local
                if end_bone is not None
                else armature.matrix_world @ start_bone.tail_local
            )
            marker_policy = "start_and_endpoint_present"
        else:
            source_start = None
            source_end = armature.matrix_world @ end_bone.head_local
            marker_policy = "orphan_end_recovers_named_missing_start"
        named_pairs.append(
            {
                "ordinal": ordinal,
                "root": roots_by_ordinal[ordinal],
                "source_start": source_start,
                "source_end": source_end,
                "marker_policy": marker_policy,
            }
        )

    scale_scores = []
    for scale in CLUSTER_XML_SCALE_CANDIDATES:
        distances = []
        for pair in named_pairs:
            root = pair["root"]
            if pair["source_start"] is not None:
                distances.append(
                    float(
                        (
                            pair["source_start"]
                            - root["start_raw"] / scale
                        ).length
                    )
                )
            distances.append(
                float(
                    (
                        pair["source_end"]
                        - root["end_raw"] / scale
                    ).length
                )
            )
        ordered = sorted(float(value) for value in distances)
        middle = len(ordered) // 2
        median = (
            ordered[middle]
            if len(ordered) % 2
            else (ordered[middle - 1] + ordered[middle]) * 0.5
        )
        scale_scores.append(
            {
                "scale": float(scale),
                "median_named_error": float(median),
                "max_named_error": float(max(ordered)),
            }
        )
    scale_scores.sort(
        key=lambda row: (
            row["median_named_error"],
            row["max_named_error"],
            row["scale"],
        )
    )
    xml_scale = scale_scores[0]["scale"]
    geometry_scale = _cluster_geometry_scale(meshes)
    tolerance = max(geometry_scale * 1.0e-4, 1.0e-6)
    matches = []
    for pair in named_pairs:
        root = {
            **pair["root"],
            "start_world": pair["root"]["start_raw"] / xml_scale,
            "end_world": pair["root"]["end_raw"] / xml_scale,
        }
        start_error = (
            float((pair["source_start"] - root["start_world"]).length)
            if pair["source_start"] is not None
            else 0.0
        )
        end_error = float(
            (pair["source_end"] - root["end_world"]).length
        )
        matches.append(
            {
                "ordinal": pair["ordinal"],
                "root": root,
                "policy": "exact_bone_ordinal_to_xml_root_id_v1",
                "marker_policy": pair["marker_policy"],
                "start_error": start_error,
                "end_error": end_error,
                "coordinate_validation": (
                    "within_tolerance"
                    if start_error <= tolerance and end_error <= tolerance
                    else "diagnostic_mismatch"
                ),
            }
        )

    by_ordinal = {row["ordinal"]: row for row in matches}
    ensure_object_mode()
    deselect_all()
    bpy.context.view_layer.objects.active = armature
    armature.select_set(True)
    bpy.ops.object.mode_set(mode="EDIT")
    try:
        edit_bones = armature.data.edit_bones
        for bone in list(edit_bones):
            edit_bones.remove(bone)
        world_to_armature = armature.matrix_world.inverted_safe()
        for ordinal in sorted(by_ordinal):
            match = by_ordinal[ordinal]
            root = match["root"]
            bone = edit_bones.new(f"Bone_{ordinal}_Start")
            bone.head = world_to_armature @ root["start_world"]
            bone.tail = world_to_armature @ root["end_world"]
            bone.use_deform = True
            if (bone.tail - bone.head).length <= max(tolerance * 1.0e-3, 1.0e-8):
                raise RuntimeError(
                    f"Cluster XML axis {ordinal} is geometrically degenerate."
                )
    finally:
        bpy.ops.object.mode_set(mode="OBJECT")
        armature.select_set(False)

    return {
        "status": "canonicalized_xml_render_roots",
        "xml_path": str(Path(xml_path).resolve()),
        "xml_scale": float(xml_scale),
        "xml_scale_scores": scale_scores,
        "axis_count": len(matches),
        "identity_contract": "fbx_named_axes_subset_of_xml_roots_v1",
        "unused_xml_root_ids": named_root_contract[
            "unused_xml_root_ids"
        ],
        "bone_names": [
            f"Bone_{ordinal}_Start" for ordinal in sorted(by_ordinal)
        ],
        "geometry_scale": float(geometry_scale),
        "match_tolerance": float(tolerance),
        "axes": [
            {
                "ordinal": row["ordinal"],
                "xml_bone_id": int(row["root"]["id"]),
                "xml_generator": row["root"]["generator"],
                "source_match_policy": row["policy"],
                "source_marker_policy": row["marker_policy"],
                "start_error": float(row["start_error"]),
                "end_error": float(row["end_error"]),
                "coordinate_validation": row["coordinate_validation"],
                "start_world": [
                    float(value)
                    for value in row["root"]["start_world"]
                ],
                "end_world": [
                    float(value)
                    for value in row["root"]["end_world"]
                ],
            }
            for row in sorted(matches, key=lambda item: item["ordinal"])
        ],
    }


def ensure_cluster_source_skin_contract(
    objects,
    armature_name="Root",
    xml_path="",
):
    """Canonicalize render-root axes and preserve authored Cluster skin.

    Imported ``*_End`` joints are endpoint markers, not valid mesh-axis bones.
    The matching Raw XML roots are converted to one complete ``*_Start`` deform
    bone spanning Start->End for every renderable structural root.

    Multi-piece authored skin remains partitioned across those axes. A
    completely unskinned source is rigid-bound only when the XML contract has
    exactly one render-root axis; multi-axis membership is never guessed.
    """
    armatures = [obj for obj in objects if obj.type == "ARMATURE"]
    if len(armatures) != 1:
        raise RuntimeError(
            "Cluster source skin contract requires exactly one imported armature; "
            f"found {len(armatures)}."
        )
    armature = armatures[0]
    if armature_name and armature.name != armature_name:
        raise RuntimeError(
            "Cluster source skin contract imported an unexpected armature: "
            f"expected {armature_name!r}, got {armature.name!r}."
        )
    meshes = [
        obj
        for obj in objects
        if obj.type == "MESH" and obj.data and len(obj.data.vertices) > 0
    ]
    if not meshes:
        raise RuntimeError(
            "Cluster source skin contract found no imported mesh geometry."
        )

    axis_normalization = _canonicalize_cluster_axis_bones(
        armature,
        meshes,
        xml_path,
    )
    deform_bones = [bone for bone in armature.data.bones if bone.use_deform]
    deform_bone_names = {bone.name for bone in deform_bones}
    skinned_meshes = []
    unskinned_meshes = []
    for obj in meshes:
        has_armature_modifier = any(
            modifier.type == "ARMATURE" and modifier.object == armature
            for modifier in obj.modifiers
        )
        deform_group_indices = {
            group.index
            for group in obj.vertex_groups
            if group.name in deform_bone_names
        }
        weighted_vertices = sum(
            1
            for vertex in obj.data.vertices
            if any(
                element.group in deform_group_indices
                and float(element.weight) > 0.0
                for element in vertex.groups
            )
        )
        if has_armature_modifier and weighted_vertices:
            skinned_meshes.append(
                {
                    "mesh": obj.name,
                    "vertices": len(obj.data.vertices),
                    "weighted_vertices": weighted_vertices,
                    "deform_groups": [
                        group.name
                        for group in obj.vertex_groups
                        if group.name in deform_bone_names
                    ],
                }
            )
        else:
            unskinned_meshes.append(
                {
                    "mesh": obj.name,
                    "vertices": len(obj.data.vertices),
                    "has_armature_modifier": has_armature_modifier,
                    "weighted_vertices": weighted_vertices,
                }
            )
    if skinned_meshes:
        return {
            "status": "preserved_authored_skin",
            "armature": armature.name,
            "bone_count": len(armature.data.bones),
            "deform_bone_count": len(deform_bones),
            "deform_bones": [bone.name for bone in deform_bones],
            "mesh_count": len(meshes),
            "skinned_mesh_count": len(skinned_meshes),
            "unskinned_mesh_count": len(unskinned_meshes),
            "skinned_meshes": skinned_meshes,
            "unskinned_meshes": unskinned_meshes,
            "axis_normalization": axis_normalization,
            "reason": (
                "Authored Cluster axis weights are present and were preserved "
                "for connected deform-cluster normalization"
            ),
        }

    if len(deform_bones) != 1:
        raise RuntimeError(
            "Cluster source is completely unskinned, so the single-axis repair "
            "requires exactly one deform bone; "
            f"found {len(deform_bones)} in {armature.name!r}."
        )
    bone = deform_bones[0]
    bound = []
    for obj in meshes:
        while obj.vertex_groups:
            obj.vertex_groups.remove(obj.vertex_groups[0])
        group = obj.vertex_groups.new(name=bone.name)
        group.add(list(range(len(obj.data.vertices))), 1.0, "REPLACE")
        for modifier in list(obj.modifiers):
            if modifier.type == "ARMATURE":
                obj.modifiers.remove(modifier)
        modifier = obj.modifiers.new(name=armature.name, type="ARMATURE")
        modifier.object = armature
        parent_keep_world(obj, armature)
        bound.append(
            {
                "mesh": obj.name,
                "vertices": len(obj.data.vertices),
                "bone": bone.name,
            }
        )

    return {
        "status": "bound_unskinned_single_axis",
        "armature": armature.name,
        "bone": bone.name,
        "bone_head": [float(value) for value in bone.head_local],
        "bone_tail": [float(value) for value in bone.tail_local],
        "mesh_count": len(bound),
        "vertex_count": sum(row["vertices"] for row in bound),
        "meshes": bound,
        "axis_normalization": axis_normalization,
        "reason": (
            "Cluster FBX contained one XML render-root axis but no skin deformers"
        ),
    }


UNASSIGNED_GEOMETRY_CLEANUP_POLICY = (
    "discard_unassigned_geometry_before_repair"
)
UNASSIGNED_GEOMETRY_CLEANUP_CONTRACT_VERSION = 2


def _cleanup_default_material_authorized(material, texture_contract):
    """Accept only the strict STMAT Default intent, never a name guess."""
    if material is None or not isinstance(texture_contract, dict):
        return False
    envelope = texture_contract.get("speedtree_pipeline_contract")
    if not isinstance(envelope, dict):
        return False
    api = handoff_contract.central_contract_api()
    material_base = api.production_group_base_name(material.name)
    if api.normalize_material_key(material_base) != "default":
        return False
    matches = _strict_material_intents_for_name(material_base, envelope)
    return len(matches) == 1 and _is_unmanaged_empty_default_intent(
        matches[0]
    )


def _cleanup_live_identity(texture_contract, spm_path, source_fbx_path):
    """Authorize deletion only from the current strict FBX handoff inputs."""
    if not isinstance(texture_contract, dict) or not texture_contract.get(
        "strict_speedtree_pipeline_contract"
    ):
        return None
    if not spm_path or not source_fbx_path:
        return None
    stmat_path = Path(source_fbx_path).with_suffix(".stmat")
    return _live_speedtree_source_identity(spm_path, [stmat_path])


def _cleanup_face_reason(polygon, materials, texture_contract):
    slot_index = int(polygon.material_index)
    if slot_index < 0 or slot_index >= len(materials):
        return "material_slot_out_of_range"
    material = materials[slot_index]
    if material is None:
        return "empty_material_slot"
    if _cleanup_default_material_authorized(material, texture_contract):
        return "canonical_unmanaged_default_material"
    return ""


def renderable_geometry_evidence(objects):
    meshes = [
        obj
        for obj in objects or []
        if obj is not None
        and obj.type == "MESH"
        and obj.data is not None
        and len(obj.data.polygons) > 0
    ]
    return {
        "status": "ok" if meshes else "empty",
        "mesh_object_count": len(meshes),
        "face_count": sum(len(obj.data.polygons) for obj in meshes),
    }


def _cleanup_record_base(
    *, texture_contract, spm_path, source_fbx_path, objects
):
    live_identity = _cleanup_live_identity(
        texture_contract, spm_path, source_fbx_path
    )
    if live_identity is None:
        return None
    fingerprint = hashlib.sha256(
        json.dumps(
            live_identity,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        ).encode("utf-8")
    ).hexdigest()
    return {
        "policy": UNASSIGNED_GEOMETRY_CLEANUP_POLICY,
        "cleanup_contract_version": (
            UNASSIGNED_GEOMETRY_CLEANUP_CONTRACT_VERSION
        ),
        "status": "not_applicable",
        "strict_speedtree_pipeline_contract": True,
        "cleanup_authorized": True,
        "live_source_identity_validated": True,
        "source_identity": str(Path(spm_path).resolve()),
        "source_fbx": str(Path(source_fbx_path).resolve()),
        "live_source_identity": live_identity,
        "live_source_identity_fingerprint": fingerprint,
        "inspected_mesh_object_count": len(objects),
        "changed_object_count": 0,
        "removed_object_count": 0,
        "removed_face_count": 0,
        "removed_edge_count": 0,
        "removed_vertex_count": 0,
        "removed_material_slot_count": 0,
        "objects": [],
        "removed_objects": [],
    }


def discard_unassigned_geometry_before_repair(
    objects, *, texture_contract, spm_path, source_fbx_path
):
    """Discard only Full-FBX faces that have no production material.

    This runs before Blender joins material-grouped imports. Otherwise a
    no-slot object is silently mapped to the first valid join material, which
    can make collision/unused geometry masquerade as cluster planes.
    """
    mesh_objects = [
        obj
        for obj in objects or []
        if obj is not None and obj.type == "MESH" and obj.data is not None
    ]
    record = _cleanup_record_base(
        texture_contract=texture_contract,
        spm_path=spm_path,
        source_fbx_path=source_fbx_path,
        objects=mesh_objects,
    )

    candidates = []
    for obj in mesh_objects:
        materials = list(obj.data.materials)
        invalid = [
            (int(polygon.index), reason)
            for polygon in obj.data.polygons
            if (
                reason := _cleanup_face_reason(
                    polygon, materials, texture_contract
                )
            )
        ]
        if invalid:
            candidates.append((obj, materials, invalid))

    if record is None:
        if candidates:
            raise RuntimeError(
                "Full FBX contains unassigned/Default geometry, but the "
                "current strict SpeedTree SPM/STMAT handoff is unavailable; "
                "refusing to let Blender promote it during join"
            )
        return {
            "status": "not_authorized",
            "reason": "strict_speedtree_pipeline_contract_not_present",
        }

    for obj, old_materials, invalid in candidates:
        mesh = obj.data
        object_name = obj.name
        faces_before = len(mesh.polygons)
        edges_before = len(mesh.edges)
        vertices_before = len(mesh.vertices)
        slots_before = len(old_materials)
        reason_counts = dict(sorted(Counter(reason for _, reason in invalid).items()))
        invalid_indices = {index for index, _reason in invalid}
        remove_entire_object = len(invalid_indices) == faces_before

        if remove_entire_object:
            removed_slot_names = [
                material.name if material is not None else None
                for material in old_materials
            ]
            remove_object_and_orphan_mesh(obj)
            row = {
                "object": object_name,
                "removed_object": True,
                "faces_before": faces_before,
                "faces_after": 0,
                "removed_face_count": faces_before,
                "edges_before": edges_before,
                "edges_after": 0,
                "removed_edge_count": edges_before,
                "vertices_before": vertices_before,
                "vertices_after": 0,
                "removed_vertex_count": vertices_before,
                "material_slots_before": slots_before,
                "material_slots_after": 0,
                "removed_material_slots": removed_slot_names,
                "removed_face_reasons": reason_counts,
            }
        else:
            bm = bmesh.new()
            try:
                bm.from_mesh(mesh)
                doomed_faces = [
                    face for face in bm.faces
                    if int(face.index) in invalid_indices
                ]
                bmesh.ops.delete(bm, geom=doomed_faces, context="FACES")
                orphan_vertices = [
                    vertex for vertex in bm.verts if not vertex.link_faces
                ]
                if orphan_vertices:
                    bmesh.ops.delete(
                        bm, geom=orphan_vertices, context="VERTS"
                    )
                bm.to_mesh(mesh)
            finally:
                bm.free()
            mesh.update()

            surviving_old_slots = [
                int(polygon.material_index) for polygon in mesh.polygons
            ]
            used_slots = sorted(set(surviving_old_slots))
            slot_remap = {
                old_index: new_index
                for new_index, old_index in enumerate(used_slots)
            }
            kept_materials = [old_materials[index] for index in used_slots]
            mesh.materials.clear()
            for material in kept_materials:
                mesh.materials.append(material)
            for polygon, old_index in zip(
                mesh.polygons, surviving_old_slots
            ):
                polygon.material_index = slot_remap[old_index]
            removed_slot_names = [
                material.name if material is not None else None
                for index, material in enumerate(old_materials)
                if index not in used_slots
            ]
            row = {
                "object": object_name,
                "removed_object": False,
                "faces_before": faces_before,
                "faces_after": len(mesh.polygons),
                "removed_face_count": faces_before - len(mesh.polygons),
                "edges_before": edges_before,
                "edges_after": len(mesh.edges),
                "removed_edge_count": edges_before - len(mesh.edges),
                "vertices_before": vertices_before,
                "vertices_after": len(mesh.vertices),
                "removed_vertex_count": vertices_before - len(mesh.vertices),
                "material_slots_before": slots_before,
                "material_slots_after": len(mesh.materials),
                "removed_material_slots": removed_slot_names,
                "removed_face_reasons": reason_counts,
            }

        record["objects"].append(row)
        if row["removed_object"]:
            record["removed_objects"].append(object_name)
        record["removed_object_count"] += int(row["removed_object"])
        record["removed_face_count"] += row["removed_face_count"]
        record["removed_edge_count"] += row["removed_edge_count"]
        record["removed_vertex_count"] += row["removed_vertex_count"]
        record["removed_material_slot_count"] += len(
            row["removed_material_slots"]
        )

    record["changed_object_count"] = len(record["objects"])
    if record["changed_object_count"]:
        record["status"] = "applied"
    return record


def run_import_source_fbx(
    source_fbx_path,
    source_collection_name="SpeedTree_Source",
    rigid_fallback=False,
    armature_name="Root",
    true_root="Bone_1_Start",
    spm_path="",
    texture_contract=None,
    cluster_source_skin_contract=False,
    cluster_source_xml_path="",
    source_identity_path="",
):
    path = Path(source_fbx_path)
    if not source_fbx_path or not path.exists():
        raise RuntimeError(f"Source FBX does not exist: {source_fbx_path}")
    if texture_contract is None:
        texture_contract = _bat_runtime_texture_contract(None)

    before = {obj.name for obj in bpy.data.objects}
    bpy.ops.import_scene.fbx(filepath=str(path))
    imported = [obj for obj in bpy.data.objects if obj.name not in before]
    # Keep raw imports out of the Export collection (the FBX importer links to
    # the active collection) — send2ue exports every unit found in Export, so
    # stray source objects there would split the asset into multiple FBX files.
    source_collection = ensure_scene_collection(source_collection_name)
    for obj in imported:
        obj["codex_source_fbx"] = str(path)
        if source_identity_path:
            obj["codex_source_identity"] = str(source_identity_path)
        ensure_only_collection(obj, source_collection)
    tag_speedtree_import_materials(
        imported,
        path,
        source_identity_path=source_identity_path,
    )
    unassigned_geometry_cleanup = (
        discard_unassigned_geometry_before_repair(
            imported,
            texture_contract=texture_contract,
            spm_path=spm_path,
            source_fbx_path=str(path),
        )
    )
    imported = [obj for obj in bpy.data.objects if obj.name not in before]
    renderable_geometry = renderable_geometry_evidence(imported)
    texture_preflight = preflight_speedtree_material_texture_contracts(
        imported,
        texture_contract,
        source_fbx_override=str(path),
    )
    texture_contract = texture_preflight["texture_contract"]
    applied_scales = apply_object_scales(imported)
    renamed_materials = strip_speedtree_material_suffixes(imported)
    material_consolidation = consolidate_speedtree_group_materials(
        imported, texture_contract=texture_contract
    )
    material_intents = apply_speedtree_material_intents(
        imported, texture_contract=texture_contract
    )
    instance_profile = (
        apply_spm_unreal_instance_profile(imported, spm_path)
        if spm_path
        else {"status": "not_requested", "profile": ""}
    )
    texture_normalization = normalize_speedtree_material_textures(
        imported, texture_contract=texture_contract
    )
    removed_phantoms = remove_phantom_image_nodes(imported)
    rigid_fallback_result = None
    cluster_source_skin_result = None
    if cluster_source_skin_contract:
        cluster_source_skin_started = perf_counter()
        cluster_source_skin_result = ensure_cluster_source_skin_contract(
            imported,
            armature_name,
            cluster_source_xml_path,
        )
        cluster_source_skin_result["duration_seconds"] = round(
            perf_counter() - cluster_source_skin_started,
            6,
        )
    elif rigid_fallback:
        rigid_fallback_result = build_rigid_fallback_armature(imported, armature_name, true_root)
        if rigid_fallback_result:
            armature = bpy.data.objects.get(rigid_fallback_result["armature"])
            if armature:
                armature["codex_source_fbx"] = str(path)
                ensure_only_collection(armature, source_collection)
                imported.append(armature)

    return {
        "source_fbx": str(path),
        "source_identity": str(spm_path or ""),
        "source_collection": source_collection.name,
        "imported_object_count": len(imported),
        "imported_armature_count": sum(1 for obj in imported if obj.type == "ARMATURE"),
        "imported_mesh_count": sum(1 for obj in imported if obj.type == "MESH"),
        "imported_objects": [obj.name for obj in imported[:200]],
        "applied_scales": applied_scales,
        "removed_phantom_texture_nodes": removed_phantoms,
        "renamed_materials": renamed_materials,
        "material_consolidation": material_consolidation,
        "speedtree_material_intents": material_intents,
        "speedtree_material_texture_preflight": texture_preflight,
        "unreal_instance_profile": instance_profile,
        "texture_normalization": texture_normalization,
        "rigid_fallback": rigid_fallback_result,
        "cluster_source_skin_contract": cluster_source_skin_result,
        "unassigned_geometry_cleanup": unassigned_geometry_cleanup,
        "renderable_geometry": renderable_geometry,
    }


# ---------------------------------------------------------------------------
# Scene / view-layer safe helpers
# ---------------------------------------------------------------------------


def object_hidden(obj):
    # Objects in excluded collections are not in the view layer; hide_get()
    # returns False for them (Blender 5.1) so membership must be checked
    # explicitly, and select_set() on them would raise.
    if bpy.context.view_layer.objects.get(obj.name) is None:
        return True
    try:
        if obj.hide_get():
            return True
    except RuntimeError:
        return True
    return bool(obj.hide_viewport)


def deselect_all():
    for obj in bpy.context.view_layer.objects:
        obj.select_set(False)


def ensure_object_mode():
    if bpy.context.mode != "OBJECT" and bpy.context.view_layer.objects.active:
        bpy.ops.object.mode_set(mode="OBJECT")


def require_in_view_layer(obj, why):
    if bpy.context.view_layer.objects.get(obj.name) is None:
        raise RuntimeError(f"Object '{obj.name}' must be in the current view layer {why}.")


def remove_object_and_orphan_mesh(obj):
    if not obj:
        return
    mesh = obj.data if obj.type == "MESH" else None
    bpy.data.objects.remove(obj, do_unlink=True)
    if mesh and mesh.users == 0:
        bpy.data.meshes.remove(mesh)


def collection_in_scene(collection):
    def walk(node):
        if node == collection:
            return True
        return any(walk(child) for child in node.children)

    return walk(bpy.context.scene.collection)


def ensure_scene_collection(name):
    collection = bpy.data.collections.get(name)
    if collection is None:
        collection = bpy.data.collections.new(name)
    if not collection_in_scene(collection):
        bpy.context.scene.collection.children.link(collection)
    return collection


def parent_keep_world(obj, parent):
    world = obj.matrix_world.copy()
    obj.parent = parent
    obj.matrix_parent_inverse.identity()
    obj.matrix_world = world


def parent_depth(obj):
    depth = 0
    current = obj.parent
    while current:
        depth += 1
        current = current.parent
    return depth


def has_non_unit_scale(obj, tolerance=1e-6):
    return any(abs(float(value) - 1.0) > tolerance for value in obj.scale)


def apply_object_scales(objects, tolerance=1e-6, max_passes=4):
    # Blender's FBX importer can express centimeter conversion as object scale
    # 0.01 on the armature/root containers. Apply scale parent-first, then
    # repeat so child meshes that inherit the parent's inverse scale are baked
    # too. This keeps world-space size unchanged while leaving object scale 1.
    candidates = [
        obj
        for obj in objects
        if obj and obj.type in {"ARMATURE", "MESH", "EMPTY"} and bpy.data.objects.get(obj.name) is obj
    ]
    if not candidates:
        return []

    ensure_object_mode()
    applied = []
    for _pass_index in range(max_passes):
        changed = False
        for depth in sorted({parent_depth(obj) for obj in candidates}):
            batch = [
                obj
                for obj in candidates
                if parent_depth(obj) == depth
                and has_non_unit_scale(obj, tolerance=tolerance)
                and bpy.context.view_layer.objects.get(obj.name) is obj
            ]
            if not batch:
                continue
            deselect_all()
            for obj in batch:
                obj.select_set(True)
            bpy.context.view_layer.objects.active = batch[0]
            old_scales = {obj.name: tuple(float(value) for value in obj.scale) for obj in batch}
            bpy.ops.object.transform_apply(location=False, rotation=False, scale=True)
            for obj in batch:
                applied.append(
                    {
                        "object": obj.name,
                        "type": obj.type,
                        "old_scale": old_scales[obj.name],
                        "new_scale": tuple(float(value) for value in obj.scale),
                    }
                )
            changed = True
        if not changed:
            break
    deselect_all()
    return applied


def join_objects(objects):
    # Objects must be linked in the current view layer and visible.
    deselect_all()
    for obj in objects:
        obj.select_set(True)
    bpy.context.view_layer.objects.active = objects[0]
    if len(objects) > 1:
        bpy.ops.object.join()
    return objects[0]


def compile_optional_regex(pattern):
    if not pattern:
        return None
    return re.compile(pattern, re.IGNORECASE)


# ---------------------------------------------------------------------------
# Simulation-group vertex-color preview
# ---------------------------------------------------------------------------
#
# The grouping MegaPlant actually consumes is per-bone: every bone maps to an
# XML Generator simulation group (Trunk / Big N / Branch N / Roots / Root
# Twigs...). Material-split SpeedTree exports put the whole trunk+branch tree
# into a single bark material, so material/object grouping cannot separate
# trunk from branch. A per-vertex color attribute can: color each vertex by the
# simulation group of its dominant (highest-weight) bone.
#
# Colors are laid out in HSV so related generators read as one visual family:
# each generator family (Trunk, Big, Branch, Roots, Root Twigs...) gets a hue
# band and members within a family spread across value/saturation, so they stay
# distinct without leaving the family's color.

FAMILY_BASE_HUE = {
    "trunk": 0.07,
    "big": 0.11,
    "bifurcating": 0.17,
    "branch": 0.33,
    "leaf": 0.28,
    "leaves": 0.28,
    "frond": 0.40,
    "twig": 0.46,
    "twigs": 0.46,
    "root twigs": 0.55,
    "root": 0.83,
    "roots": 0.83,
    "cap": 0.02,
    "knot": 0.02,
    "cavity": 0.02,
}


def generator_family(name):
    # "Branch 6" -> ("branch", 6); "Root Twigs" -> ("root twigs", 0).
    low = (name or "").strip().lower()
    if not low:
        return "other", 0
    match = re.search(r"(\d+)\s*$", low)
    ordinal = int(match.group(1)) if match else 0
    base = re.sub(r"\s*\d+\s*$", "", low).strip()
    return base or "other", ordinal


def resolve_family_hue(base, unknown_slot, unknown_total):
    if base in FAMILY_BASE_HUE:
        return FAMILY_BASE_HUE[base]
    for key, hue in FAMILY_BASE_HUE.items():
        if key in base or base in key:
            return hue
    # Unknown generator family: spread across the unused blue/magenta arc.
    return (0.60 + 0.30 * (unknown_slot / max(unknown_total, 1))) % 1.0


def build_simgroup_color_map(simulation_groups):
    # simulation_groups: [{"index", "generators", "bone_count", ...}] from the
    # XML metadata. Returns ({group_index: (r, g, b, a)}, legend_list).
    families = defaultdict(list)
    for group in simulation_groups:
        generators = group.get("generators") or [""]
        base, _ordinal = generator_family(generators[0])
        families[base].append(group)

    ordered_families = sorted(
        families.keys(), key=lambda base: min(g["index"] for g in families[base])
    )
    unknown_families = [
        base
        for base in ordered_families
        if base not in FAMILY_BASE_HUE
        and not any(key in base or base in key for key in FAMILY_BASE_HUE)
    ]

    colors = {}
    legend = []
    for base in ordered_families:
        groups = sorted(families[base], key=lambda g: g["index"])
        if base in unknown_families:
            hue = resolve_family_hue(base, unknown_families.index(base), len(unknown_families))
        else:
            hue = resolve_family_hue(base, 0, 1)
        count = len(groups)
        # Spread members across a hue band around the family hue instead of
        # dropping value/saturation — keeps every member bright and saturated
        # (no muddy grey for crowded families like Branch) while similar groups
        # stay in the same color band.
        band = min(0.05 + 0.006 * count, 0.13)
        for member_index, group in enumerate(groups):
            if count == 1:
                hue_member, value, saturation = hue, 0.92, 0.85
            else:
                t = member_index / (count - 1)
                hue_member = (hue - band / 2.0 + band * t) % 1.0
                value = 0.98 - 0.18 * t
                saturation = 0.95 - 0.15 * t
            red, green, blue = colorsys.hsv_to_rgb(hue_member, saturation, value)
            colors[group["index"]] = (red, green, blue, 1.0)
            legend.append(
                {
                    "index": group["index"],
                    "family": base,
                    "generators": group.get("generators"),
                    "bone_count": group.get("bone_count"),
                    "color": [round(red, 3), round(green, 3), round(blue, 3)],
                }
            )
    legend.sort(key=lambda item: item["index"])
    return colors, legend


def clear_json_preview_props(obj):
    obj.color = (1.0, 1.0, 1.0, 1.0)
    for key in JSON_PREVIEW_OBJECT_KEYS:
        if key in obj:
            del obj[key]


def remove_json_preview_attribute(mesh):
    attr = mesh.color_attributes.get(JSON_PREVIEW_ATTR_NAME)
    if attr:
        mesh.color_attributes.remove(attr)


DEFAULT_PREVIEW_COLOR = (0.5, 0.5, 0.5, 1.0)


def build_bone_group_preview(settings, armature):
    # Recompute the per-bone simulation group metadata from the XML.
    xml_path = settings.get("xml_path", "")
    if not xml_path:
        raise RuntimeError(
            "Preview needs the SpeedTree XML (per-bone simulation groups). Set the XML path first."
        )
    bone_records, info = build_xml_bone_metadata(
        xml_path, armature, settings.get("xml_trunk_generator_regex", "trunk")
    )
    bone_group_index = {record["name"]: record["group"] for record in bone_records}
    return bone_group_index, info


def write_bone_group_vertex_colors(obj, bone_group_index, group_colors, default_color=DEFAULT_PREVIEW_COLOR):
    # Color every vertex by the simulation group of its highest-weight bone.
    mesh = obj.data
    vertex_count = len(mesh.vertices)

    vgroup_to_group = {}
    for vgroup in obj.vertex_groups:
        group_index = bone_group_index.get(vgroup.name)
        if group_index is not None:
            vgroup_to_group[vgroup.index] = group_index

    # Single pass over the vertices to find each vertex's dominant sim group.
    dominant = [-1] * vertex_count
    if vgroup_to_group:
        for vertex in mesh.vertices:
            best_weight = 0.0
            best_group = -1
            for group_ref in vertex.groups:
                weight = group_ref.weight
                if weight > best_weight:
                    mapped = vgroup_to_group.get(group_ref.group, -1)
                    if mapped >= 0:
                        best_weight = weight
                        best_group = mapped
            dominant[vertex.index] = best_group

    dominant_array = np.array(dominant, dtype=np.int64)
    highest = int(dominant_array.max()) if dominant_array.size else -1
    max_group = highest if highest >= 0 else -1
    # One extra palette row (index max_group + 1) holds the default color for
    # vertices with no mapped bone group.
    palette = np.tile(np.array(default_color, dtype=np.float32), (max_group + 2, 1))
    for group_index, color in group_colors.items():
        if 0 <= group_index <= max_group:
            palette[group_index] = color
    lookup = np.where(dominant_array >= 0, dominant_array, max_group + 1)
    vertex_colors = palette[lookup].astype(np.float32, copy=False)

    attr = mesh.color_attributes.get(JSON_PREVIEW_ATTR_NAME)
    if attr and (attr.domain != "POINT" or attr.data_type not in {"BYTE_COLOR", "FLOAT_COLOR"}):
        mesh.color_attributes.remove(attr)
        attr = None
    if attr is None:
        attr = mesh.color_attributes.new(name=JSON_PREVIEW_ATTR_NAME, type="BYTE_COLOR", domain="POINT")
    attr.data.foreach_set("color", vertex_colors.reshape(-1))
    try:
        mesh.color_attributes.active_color = attr
    except Exception:
        pass
    mesh.update()

    covered = int((dominant_array >= 0).sum())
    return {
        "object": obj.name,
        "vertex_count": vertex_count,
        "colored_vertices": covered,
        "uncolored_vertices": vertex_count - covered,
        "groups_present": sorted(int(value) for value in np.unique(dominant_array) if value >= 0),
    }


def restore_json_group_preview():
    restored = []
    for obj in bpy.context.scene.objects:
        if obj.type == "MESH" and "codex_json_group" in obj:
            remove_json_preview_attribute(obj.data)
            clear_json_preview_props(obj)
            restored.append(obj.name)
        if obj.type == "ARMATURE" and JSON_PREVIEW_ARMATURE_HIDE_KEY in obj:
            obj.hide_viewport = bool(obj[JSON_PREVIEW_ARMATURE_HIDE_KEY])
            del obj[JSON_PREVIEW_ARMATURE_HIDE_KEY]
    for collection in bpy.data.collections:
        if JSON_PREVIEW_COLLECTION_HIDE_KEY in collection:
            collection.hide_viewport = bool(collection[JSON_PREVIEW_COLLECTION_HIDE_KEY])
            del collection[JSON_PREVIEW_COLLECTION_HIDE_KEY]
    if JSON_PREVIEW_SCENE_KEY in bpy.context.scene:
        del bpy.context.scene[JSON_PREVIEW_SCENE_KEY]
    return {
        "status": "preview-restored",
        "restored_count": len(restored),
        "objects": restored,
    }


def run_preview_json_groups(settings):
    if bpy.context.scene.get(JSON_PREVIEW_SCENE_KEY):
        return restore_json_group_preview()

    for obj in bpy.data.objects:
        if obj.type == "MESH":
            remove_json_preview_attribute(obj.data)
            clear_json_preview_props(obj)

    armature = get_armature(settings.get("armature_name", ""))
    bone_group_index, info = build_bone_group_preview(settings, armature)
    simulation_groups = info.get("simulation_groups", [])

    # Two color modes off the same per-bone data:
    #  - wind influence (default): cold->hot by radius-derived sway, the intuitive
    #    "how much does this part move" view that tracks the Wind Flexibility knob.
    #  - sim groups: distinct HSV family per generator, to sanity-check grouping.
    if settings.get("preview_influence", True):
        flex_by_group = derive_group_flex(simulation_groups, settings.get("dynamic_wind_flexibility", 1.0))
        group_colors = influence_color_map(flex_by_group)
        legend = [
            {
                "index": group["index"],
                "generators": group.get("generators"),
                "flex": round(flex_by_group.get(group["index"], 0.0), 2),
            }
            for group in simulation_groups
        ]
        preview_mode = "wind_influence"
    else:
        group_colors, legend = build_simgroup_color_map(simulation_groups)
        preview_mode = "sim_groups"

    # Prefer the merged deliverable when it exists; otherwise color the skinned
    # source meshes so the preview also works before merge/export.
    merged_targets = []
    source_targets = []
    for obj in bpy.context.scene.objects:
        if obj.type != "MESH" or not obj.data or len(obj.data.vertices) == 0:
            continue
        if not armature_modifier_uses(obj, armature):
            continue
        if is_codex_merged_output_name(obj.name):
            merged_targets.append(obj)
        else:
            source_targets.append(obj)
    preview_targets = merged_targets or source_targets
    if not preview_targets:
        raise RuntimeError("No skinned mesh bound to the armature to preview.")

    source_collection = bpy.data.collections.get(settings.get("source_collection_name", "SpeedTree_Source"))
    if merged_targets and source_collection:
        source_collection[JSON_PREVIEW_COLLECTION_HIDE_KEY] = source_collection.hide_viewport
        source_collection.hide_viewport = True

    # The armature's bones (thousands of octahedra) otherwise bury the colored
    # mesh under a grey cloud; hide it for the duration of the preview.
    armature[JSON_PREVIEW_ARMATURE_HIDE_KEY] = armature.hide_viewport
    armature.hide_viewport = True

    previewed = []
    for obj in preview_targets:
        stats = write_bone_group_vertex_colors(obj, bone_group_index, group_colors)
        obj["codex_json_group"] = "bone_simulation_groups"
        obj["codex_json_group_matched_by"] = "dominant_bone"
        obj["codex_json_preview_source"] = "bone_simulation_group.vertex_color"
        previewed.append(stats)

    bpy.context.scene[JSON_PREVIEW_SCENE_KEY] = True
    return {
        "status": "preview-applied",
        "source": "bone_simulation_groups",
        "preview_mode": preview_mode,
        "simulation_group_count": len(simulation_groups),
        "match": info.get("match", {}),
        "legend": legend,
        "objects": previewed,
    }


# ---------------------------------------------------------------------------
# Dynamic wind JSON — the Unreal-ready import form
# ---------------------------------------------------------------------------
#
# Unreal's CodexDynamicWindImportLibrary.import_dynamic_wind_json_to_skeletal_mesh
# consumes {Joints:[{JointName, SimulationGroupIndex}], SimulationGroups:[...],
# bIsGroundCover, GustAttenuation}. This is the same conversion the old manual
# work-script did in the editor; it belongs here next to the bone/group data the
# add-on already computes, so the add-on emits the import-ready JSON directly and
# Unreal only has to call the stable C++ import function. Final values remain
# for legacy importers; the versioned response contract lets Unreal rebuild
# them from one shared profile per immutable preset ID.


def derive_group_flex(simulation_groups, flexibility=1.0):
    # Per-group "flexibility" 0..1 from the group's mean bone RADIUS, normalized
    # across the tree's non-trunk groups: thick (trunk-ward) -> 0 (stiff), thin
    # (twigs) -> 1 (sways most). Beam bending is radius-driven, so this reads the
    # tuning straight off the tree — no per-group hand-authoring. The single
    # "flexibility" knob scales the whole tree's sway. Trunk is always 0.
    non_trunk = [
        group for group in simulation_groups
        if not group.get("is_trunk_group", group.get("index") == 0)
    ]
    radii = [group.get("mean_radius") for group in non_trunk if group.get("mean_radius") is not None]
    radius_hi = max(radii) if radii else 1.0
    radius_lo = min(radii) if radii else 0.0
    span = max(radius_hi - radius_lo, 1e-6)

    flex_by_group = {}
    for group in simulation_groups:
        index = group["index"]
        if group.get("is_trunk_group", index == 0):
            flex_by_group[index] = 0.0
            continue
        radius = group.get("mean_radius", radius_lo)
        raw = (radius_hi - radius) / span  # 0 thick .. 1 thin
        flex_by_group[index] = min(1.0, max(0.0, raw * flexibility))
    return flex_by_group


def influence_color_map(flex_by_group):
    # Cold->hot heat map for the wind-influence preview: stiff (0) = blue,
    # flexible (1) = red, so the user reads "how much each part sways" straight
    # off the tree without touching any group.
    colors = {}
    for index, flex in flex_by_group.items():
        clamped = min(1.0, max(0.0, flex))
        hue = 0.66 * (1.0 - clamped)  # 0.66 blue -> 0.0 red
        red, green, blue = colorsys.hsv_to_rgb(hue, 0.9, 0.95)
        colors[index] = (red, green, blue, 1.0)
    return colors


def build_dynamic_wind_groups(simulation_groups, flexibility=1.0, ground_cover=False):
    flex_by_group = derive_group_flex(simulation_groups, flexibility)
    max_index = max((group["index"] for group in simulation_groups), default=0)
    by_index = {group["index"]: group for group in simulation_groups}
    groups = []
    zero_sway = flexibility <= 0.0
    for group_index in range(max_index + 1):
        source = by_index.get(group_index, {})
        if zero_sway:
            # Wind NONE: Unreal's trunk shader path rocks trunk bones regardless
            # of Influence, so a truly still plant must be all non-trunk with
            # zero influence — Influence 0 on the branch path is exact identity.
            groups.append(
                {
                    "bUseDualInfluence": False,
                    "Influence": 0.0,
                    "MinInfluence": 0.0,
                    "MaxInfluence": 0.0,
                    "ShiftTop": 0.0,
                    "bIsTrunkGroup": False,
                }
            )
            continue
        if source.get("is_trunk_group", group_index == 0):
            if ground_cover:
                # Ground cover (grass): a trunk group makes the whole clump rock
                # rigidly like a plate — single-generator grass SPMs put every
                # bone in group 0, so the entire field tilts in unison. Emit the
                # verified grass reference instead: non-trunk dual influence so
                # blades bend per-bone on the branch path.
                groups.append(
                    {
                        "bUseDualInfluence": True,
                        "Influence": 0.0,
                        "MinInfluence": 0.2,
                        "MaxInfluence": 0.6,
                        "ShiftTop": 0.3,
                        "bIsTrunkGroup": False,
                    }
                )
                continue
            groups.append(
                {
                    "bUseDualInfluence": False,
                    "Influence": 1.0,
                    "MinInfluence": 0.0,
                    "MaxInfluence": 0.0,
                    "ShiftTop": 0.0,
                    "bIsTrunkGroup": True,
                }
            )
            continue
        flex = flex_by_group.get(group_index, 0.0)
        min_influence = min(1.0, 0.2 + 0.6 * flex)
        max_influence = min(1.0, min_influence + 0.4)
        shift_top = max(0.0, 0.3 * (1.0 - flex))
        groups.append(
            {
                "bUseDualInfluence": True,
                "Influence": 0.0,
                "MinInfluence": round(min_influence, 2),
                "MaxInfluence": round(max_influence, 2),
                "ShiftTop": round(shift_top, 2),
                "bIsTrunkGroup": False,
            }
        )
    return groups


def build_dynamic_wind_group_bases(simulation_groups):
    """Build the asset-specific basis used by shared Unreal response profiles."""
    normalized_flex = derive_group_flex(simulation_groups, 1.0)
    max_index = max((group["index"] for group in simulation_groups), default=0)
    by_index = {group["index"]: group for group in simulation_groups}
    bases = []
    for group_index in range(max_index + 1):
        source = by_index.get(group_index, {})
        bases.append(
            {
                "SimulationGroupIndex": group_index,
                "BaseFlexibility": round(
                    min(1.0, max(0.0, normalized_flex.get(group_index, 0.0))),
                    6,
                ),
                "bSourceTrunkGroup": bool(
                    source.get("is_trunk_group", group_index == 0)
                ),
            }
        )
    return bases


def build_final_skeleton_wind_contract(bone_records, import_root_name):
    """Build the UE FBX identity, including the armature-object root bone.

    Send2UE exports the Blender armature object as the UE reference skeleton's
    index-0 root. The isolated Elm pilot proves the concrete mapping used by
    this pipeline: armature object ``Root`` followed by all Blender bones with
    indices shifted by one. The live armature object name is used here rather
    than guessing a constant.
    """
    blender_bones = []
    for bone in bone_records or []:
        name = str(bone.get("name") or "")
        if not name or bone.get("bone_index") is None:
            raise RuntimeError(
                "Cannot build DynamicWind skeleton contract: missing final bone name/index."
            )
        blender_bones.append(
            {
                "name": name,
                "bone_index": int(bone["bone_index"]),
                "parent_index": int(bone.get("parent_index", -1)),
                "group": bone.get("group"),
            }
        )

    blender_bones.sort(key=lambda item: item["bone_index"])
    expected_indices = list(range(len(blender_bones)))
    actual_indices = [item["bone_index"] for item in blender_bones]
    if not blender_bones or actual_indices != expected_indices:
        raise RuntimeError(
            "Cannot build DynamicWind skeleton contract: final bone indices "
            "must be unique and contiguous from zero."
        )
    root_name = str(import_root_name or "").strip()
    if not root_name:
        raise RuntimeError(
            "Cannot build DynamicWind skeleton contract: armature object root name missing."
        )
    names = [item["name"] for item in blender_bones]
    if root_name in names:
        raise RuntimeError(
            "Cannot build DynamicWind skeleton contract: armature object root "
            f"duplicates a Blender bone name ({root_name})."
        )
    if len(set(names)) != len(names):
        raise RuntimeError(
            "Cannot build DynamicWind skeleton contract: duplicate final bone names."
        )
    for item in blender_bones:
        parent_index = item["parent_index"]
        if parent_index < -1 or parent_index >= len(blender_bones):
            raise RuntimeError(
                "Cannot build DynamicWind skeleton contract: parent index out of range "
                f"for {item['name']}: {parent_index}."
            )
        if parent_index == item["bone_index"]:
            raise RuntimeError(
                "Cannot build DynamicWind skeleton contract: bone cannot parent itself "
                f"({item['name']})."
            )

    final_bones = [
        {
            "name": root_name,
            "bone_index": 0,
            "parent_index": -1,
            "group": None,
            "identity_source": "blender_armature_object",
        }
    ]
    for item in blender_bones:
        final_bones.append(
            {
                "name": item["name"],
                "bone_index": item["bone_index"] + 1,
                "parent_index": (
                    item["parent_index"] + 1
                    if item["parent_index"] >= 0
                    else 0
                ),
                "group": item["group"],
                "identity_source": "blender_armature_bone",
            }
        )

    digest = hashlib.sha1()
    identity_rows = []
    for item in final_bones:
        digest.update(
            (
                f"{item['bone_index']}\0{item['name']}\0"
                f"{item['parent_index']}\n"
            ).encode("utf-8")
        )
        identity_rows.append(
            {
                "BoneName": item["name"],
                "BoneIndex": item["bone_index"],
                "ParentIndex": item["parent_index"],
            }
        )
    return final_bones, {
        "SchemaVersion": 2,
        "BoneCount": len(final_bones),
        "BoneNameIndexParentSha1": digest.hexdigest(),
        "Bones": identity_rows,
        "ImportRoot": {
            "BoneName": root_name,
            "BoneIndex": 0,
            "ParentIndex": -1,
            "Source": "blender_armature_object",
            "ExportContract": "send2ue_fbx_armature_object_root",
        },
    }


def build_dynamic_wind_data(
    bone_records,
    simulation_groups,
    gust_attenuation=0.25,
    ground_cover=False,
    flexibility=1.0,
    import_root_name=None,
    wind_preset="TREE",
):
    indexed, skeleton_contract = build_final_skeleton_wind_contract(
        bone_records, import_root_name
    )
    joints = []
    for bone in indexed:
        group = bone.get("group")
        if group is None:
            # Import roots and intentionally non-simulated bones remain in the
            # full identity contract but are not DynamicWind joints.
            continue
        group_index = int(group)
        if group_index < 0:
            raise RuntimeError(
                "Cannot build dynamic wind JSON: simulation group index is negative "
                f"for {bone['name']}: {group_index}."
            )
        joints.append(
            {
                "JointName": bone["name"],
                "BoneIndex": bone["bone_index"],
                "ParentIndex": bone["parent_index"],
                "SimulationGroupIndex": group_index,
            }
        )
    if not joints:
        raise RuntimeError("Cannot build dynamic wind JSON: no joint→group entries (needs the SpeedTree XML).")
    canonical_preset = str(wind_preset or "TREE").strip().upper()
    if canonical_preset == "GRASS":
        canonical_preset = "WEED"
    if canonical_preset not in {"TREE", "BUSH", "WEED", "NONE"}:
        raise RuntimeError(
            "Cannot build dynamic wind JSON: unsupported response preset "
            f"{canonical_preset!r}."
        )
    return {
        "SkeletonContract": skeleton_contract,
        "Joints": joints,
        "SimulationGroups": build_dynamic_wind_groups(simulation_groups or [], flexibility, ground_cover),
        "WindResponsePresetContract": {
            "SchemaVersion": 1,
            "Preset": canonical_preset,
            "DefaultProfile": {
                "Flexibility": float(flexibility),
                "GustAttenuation": float(gust_attenuation),
                "bIsGroundCover": bool(ground_cover),
            },
            "SimulationGroupBases": build_dynamic_wind_group_bases(
                simulation_groups or []
            ),
        },
        "bIsGroundCover": bool(ground_cover),
        "GustAttenuation": float(gust_attenuation),
        # flexibility<=0 (wind NONE) marks the mesh disabled so the runtime can
        # skip it entirely; older importers ignore the extra key harmlessly.
        "bIsEnabled": flexibility > 0.0,
    }


def build_armature_fallback_metadata(xml_path, armature, reason):
    bones = list(armature.data.bones)
    names = [bone.name for bone in bones]
    records = [
        {
            "name": bone.name,
            "bone_index": index,
            "parent_index": (
                armature.data.bones.find(bone.parent.name)
                if bone.parent is not None
                else -1
            ),
            "xml_id": None,
            "generator": "RigidFallback",
            "mass": 0.0,
            "radius": 0.0,
            "group": 0,
            "match_distance": None,
        }
        for index, bone in enumerate(bones)
    ]
    group = {
        "index": 0,
        "generators": ["RigidFallback"],
        "is_trunk_group": True,
        "bone_count": len(names),
        "mean_mass": 0.0,
        "mean_radius": 0.0,
    }
    info = {
        "source": str(xml_path),
        "xml_bone_count": 0,
        "armature_bone_count": len(names),
        "synthetic": True,
        "fallback_reason": reason,
        "generators": {"RigidFallback": 0},
        "simulation_groups": [group],
        "match": {"median_distance": None, "max_distance": None},
    }
    return records, info


def write_unreal_json_from_scene(settings, paths, export_report=None):
    # Guard: never write the JSON from a scene without the repaired armature
    # (e.g. a fresh default scene) — that would overwrite a valid JSON with
    # garbage. get_armature already raises when the named armature is missing.
    armature = get_armature(settings.get("armature_name", ""))
    if len(armature.data.bones) == 0:
        raise RuntimeError("Armature has no bones; refusing to write an empty MegaPlant JSON.")

    json_path = settings.get("json_output_path") or paths["unreal_json"]

    xml_bones = None
    xml_info = None
    warnings = []
    xml_path = settings.get("xml_path", "")
    if xml_path:
        try:
            xml_bones, xml_info = build_xml_bone_metadata(
                xml_path,
                armature,
                settings.get("xml_trunk_generator_regex", "trunk"),
            )
        except RuntimeError as exc:
            if "No <Bone> entries" not in str(exc):
                raise
            xml_bones, xml_info = build_armature_fallback_metadata(xml_path, armature, str(exc))
            warnings.append(
                "SpeedTree XML had no bones; generated a rigid group-0 mapping from the fallback armature"
            )

    # Health check: MegaPlant consumes the per-bone simulation groups, so the
    # JSON is only meaningful ("not hollow") when there is XML and the bones
    # spread across more than one group.
    distinct_bone_groups = sorted({record.get("group") for record in (xml_bones or [])})
    grouping_health = {
        "has_bone_simulation_groups": bool(xml_info),
        "simulation_group_count": len(xml_info["simulation_groups"]) if xml_info else 0,
        "distinct_bone_group_count": len(distinct_bone_groups),
        "is_hollow": (not xml_info) or len(distinct_bone_groups) <= 1,
        "note": "MegaPlant consumes per-bone simulation groups (skeleton.bones[].group).",
    }
    if not xml_info:
        warnings.append("No SpeedTree XML set: JSON has no per-bone simulation groups (hollow for MegaPlant).")
    elif len(distinct_bone_groups) <= 1:
        warnings.append("All bones fell into a single simulation group; grouping is degenerate.")

    asset_id = settings.get("json_asset_id") or paths.get("name_stem") or Path(bpy.data.filepath).stem
    data = {
        "schema": "CodexMegaPlantTreeGroups",
        "schema_version": 2,
        "created_utc": utc_timestamp(),
        "grouping_health": grouping_health,
        "asset": {
            "asset_id": asset_id,
            "source_fbx": settings.get("source_fbx_path", ""),
            "source_spm": settings.get("spm_path", ""),
            "source_blend": bpy.data.filepath,
            "expected_unreal_content_path": settings.get("json_unreal_content_path", ""),
            "exported_fbx": paths.get("fbx", "") if settings.get("export_fbx", False) else "",
        },
        "skeleton": {
            "armature": armature.name,
            "true_root": settings.get("true_root", "Bone_1_Start"),
            "bone_count": len(armature.data.bones),
            "root_bones": [bone.name for bone in armature.data.bones if bone.parent is None],
            "xml": xml_info,
            "bones": xml_bones,
        },
        "runtime_boundary": {
            "note": "This JSON is the rich record. The Unreal-ready sibling carries an immutable response preset ID and group basis; shared preset values are edited in Unreal, not per mesh.",
        },
        "export_report": export_report or {},
    }
    write_report(json_path, data)
    result = {"path": json_path, "data": data, "warnings": warnings, "grouping_health": grouping_health}

    # Also emit the lean, Unreal-ready dynamic wind JSON (the import form). Needs
    # the XML — without per-bone groups there is nothing for Unreal to import.
    result["dynamic_wind_path"] = ""
    if xml_bones and settings.get("write_dynamic_wind_json", True):
        dynamic_wind_path = settings.get("dynamic_wind_output_path") or paths["dynamic_wind_json"]
        dynamic_wind = build_dynamic_wind_data(
            xml_bones,
            xml_info.get("simulation_groups", []) if xml_info else [],
            gust_attenuation=settings.get("dynamic_wind_gust_attenuation", 0.25),
            ground_cover=settings.get("dynamic_wind_ground_cover", False),
            flexibility=settings.get("dynamic_wind_flexibility", 1.0),
            import_root_name=armature.name,
            wind_preset=settings.get("wind_preset", "TREE"),
        )
        write_report(dynamic_wind_path, dynamic_wind)
        result["dynamic_wind_path"] = dynamic_wind_path
        result["dynamic_wind"] = {
            "joint_count": len(dynamic_wind["Joints"]),
            "simulation_group_count": len(dynamic_wind["SimulationGroups"]),
            "skeleton_contract": dynamic_wind["SkeletonContract"],
        }
    return result


# ---------------------------------------------------------------------------
# SPM parsing / bone reparent
# ---------------------------------------------------------------------------


def read_spm_xml(path):
    """Compatibility wrapper for callers that consume decoded SPM XML bytes."""
    return spm_reader.read_spm_xml(path)


def child_text(element, name, default=None):
    if element is None:
        return default
    child = element.find(name)
    if child is None or child.text is None:
        return default
    return child.text


def child_bool(element, name, default=False):
    value = child_text(element, name)
    if value is None:
        return default
    return str(value).strip().lower() in {"1", "true", "yes"}


def element_property_value(element, property_name):
    # SpeedTree uses several property container tags (Property,
    # SplineProperty, CurveProperty, ...). Match by their Name/Value children
    # instead of assuming one tag name.
    for prop in element.iter():
        if child_text(prop, "Name") == property_name:
            return child_text(prop, "Value")
    return None


def normalize_unreal_instance_profile(value):
    """Validate the opaque profile key authored in SpeedTree model User Data."""
    try:
        return handoff_contract.normalize_instance_profile(value)
    except RuntimeError:
        # Legacy/local installs without the shared repo keep the existing rule.
        pass
    profile = str(value or "").strip()
    if not profile:
        return ""
    if not UNREAL_INSTANCE_PROFILE_RE.fullmatch(profile):
        raise ValueError(
            "SpeedTree model User Data must be a single profile key using only "
            "letters, numbers, '_' or '-' (for example 'dead')."
        )
    return profile.casefold()


def inspect_spm_unreal_instance_profile(spm_path):
    """Read Tree Generator > SpeedTree SDK > User data directly from an SPM."""
    try:
        return spm_reader.get_derived(
            spm_path,
            "unreal_instance_profile_v1",
            lambda root: _inspect_spm_unreal_instance_profile_root(
                root,
                spm_path,
            ),
        )
    except Exception as exc:
        return {
            "status": "inspection_error",
            "spm": str(spm_path or ""),
            "property": SPEEDTREE_MODEL_USER_DATA_PROPERTY,
            "profile": "",
            "error": str(exc),
        }


def _inspect_spm_unreal_instance_profile_root(root, spm_path):
    tree_generators = [
        generator
        for generator in root.findall(".//Generator")
        if generator.attrib.get("Type") == "Tree"
    ]
    if not tree_generators:
        raise ValueError("SPM has no Tree Generator.")
    raw_value = element_property_value(
        tree_generators[0], SPEEDTREE_MODEL_USER_DATA_PROPERTY
    )
    profile = normalize_unreal_instance_profile(raw_value)
    return {
        "status": "ok" if profile else "empty",
        "spm": str(spm_path or ""),
        "property": SPEEDTREE_MODEL_USER_DATA_PROPERTY,
        "profile": profile,
        "raw_value": str(raw_value or ""),
        "tree_generator_count": len(tree_generators),
    }


def apply_spm_unreal_instance_profile(objects, spm_path):
    """Attach the model-wide profile to final Blender materials without renaming."""
    inspection = inspect_spm_unreal_instance_profile(spm_path)
    if inspection["status"] == "inspection_error":
        raise RuntimeError(
            "SpeedTree model User Data inspection failed: "
            + inspection.get("error", "unknown error")
        )

    profile = inspection.get("profile", "")
    materials = collect_object_materials(objects)
    changed = []
    cleared = []
    for material in materials:
        previous = str(material.get(UNREAL_INSTANCE_PROFILE_PROPERTY, ""))
        if profile:
            if previous != profile:
                material[UNREAL_INSTANCE_PROFILE_PROPERTY] = profile
                changed.append(material.name)
        elif UNREAL_INSTANCE_PROFILE_PROPERTY in material:
            del material[UNREAL_INSTANCE_PROFILE_PROPERTY]
            cleared.append(material.name)
    return {
        **inspection,
        "material_count": len(materials),
        "changed_materials": changed,
        "cleared_materials": cleared,
    }


def parse_coord(text):
    match = COORD_RE.search(text or "")
    if not match:
        return None
    return tuple(float(match.group(index)) for index in range(1, 4))


def coord_key(coord, places=4):
    if coord is None:
        return None
    return tuple(round(value, places) for value in coord)


def vec_div(value, scale):
    return (value[0] / scale, value[1] / scale, value[2] / scale)


def dist(a, b):
    return math.sqrt(sum((a[index] - b[index]) ** 2 for index in range(3)))


def distance_point_segment(point, start, end):
    line = end - start
    denom = line.dot(line)
    if denom <= 1e-12:
        return (point - start).length
    t = max(0.0, min(1.0, (point - start).dot(line) / denom))
    return (point - (start + line * t)).length


def tuple_point_segment_distance(point, start, end):
    return distance_point_segment(Vector(point), Vector(start), Vector(end))


def get_armature(name):
    if name:
        obj = bpy.data.objects.get(name)
        if obj and obj.type == "ARMATURE":
            return obj
        raise RuntimeError(f"Armature not found: {name}")
    armatures = [obj for obj in bpy.data.objects if obj.type == "ARMATURE"]
    if len(armatures) != 1:
        raise RuntimeError(f"Expected one armature, found {[obj.name for obj in armatures]}")
    return armatures[0]


def parse_speedtree(path):
    def build(root):
        generators = {}
        nodes = {}
        node_order = []

        for gen in root.findall(".//Generator"):
            guid = child_text(gen, "GUID")
            if not guid:
                continue
            bone_style = element_property_value(gen, "Physics:Bone style")
            bone_count = element_property_value(gen, "Physics:Bones")
            hidden = child_bool(gen, "Hidden", False)
            generators[guid] = {
                "guid": guid,
                "type": gen.attrib.get("Type"),
                "name": child_text(gen, "Name", ""),
                "level": child_text(gen, "Level"),
                "hidden": hidden,
                "bone_style": float(bone_style) if bone_style is not None else None,
                "bone_count": float(bone_count) if bone_count is not None else None,
                "bone_enabled": (
                    not hidden
                    and bone_style is not None
                    and bone_count is not None
                    and not (float(bone_style) == 0.0 and float(bone_count) == 0.0)
                ),
            }

        for node in root.findall(".//Node"):
            guid = child_text(node, "GUID")
            if not guid:
                continue
            name = child_text(node, "Name", "")
            extra = node.find("Extra")
            nodes[guid] = {
                "guid": guid,
                "type": node.attrib.get("Type"),
                "gen": child_text(node, "GeneratorGUID"),
                "parent": child_text(node, "ParentGUID"),
                "name": name,
                "coord": parse_coord(name),
                "has_skin": child_bool(extra, "m_bHasSkin", False),
                "valid_position": child_bool(extra, "m_bValidPosition", True),
            }
            node_order.append(guid)

        return {
            "generators": generators,
            "nodes": nodes,
            "node_order": node_order,
            "version": root.attrib.get("VersionString", root.attrib.get("Version")),
        }

    return spm_reader.get_derived(path, "parse_speedtree_v1", build)


def find_base_ref_pairs(tree, raw_tolerance=0.05):
    nodes = tree["nodes"]
    generators = tree.get("generators", {})
    base_refs = [
        node
        for node in nodes.values()
        if node["type"] == "BaseRef" and node["coord"] and node.get("valid_position", True)
    ]
    refs_by_key = defaultdict(list)
    for ref in base_refs:
        refs_by_key[coord_key(ref["coord"])].append(ref)

    def generator_family(node):
        name = generators.get(node.get("gen"), {}).get("name", "")
        # SpeedTree commonly pairs Base "End 2" with BaseRef "End_01 2".
        # Prefer that semantic pair when multiple references share (0, 0, 0).
        return re.sub(r"(?:_01)+", "", re.sub(r"\s+", " ", name.lower())).strip()

    records = []
    issues = []
    for guid in tree["node_order"]:
        node = nodes[guid]
        if node["type"] != "Branch":
            continue
        generator = generators.get(node.get("gen"), {})
        if generator.get("bone_enabled") is False:
            continue
        base = nodes.get(node["parent"])
        if (
            not base
            or base["type"] != "Base"
            or not base["coord"]
            or not node["coord"]
            or not base.get("valid_position", True)
            or not node.get("valid_position", True)
        ):
            continue

        candidates = refs_by_key.get(coord_key(base["coord"]), [])
        if not candidates:
            candidates = [ref for ref in base_refs if dist(ref["coord"], base["coord"]) <= raw_tolerance]
        candidates = [ref for ref in candidates if nodes.get(ref["parent"], {}).get("type") == "Branch"]
        if not candidates:
            issues.append({"child_branch": guid, "base": base["guid"], "error": "no matching BaseRef"})
            continue

        family = generator_family(base)
        family_candidates = [ref for ref in candidates if generator_family(ref) == family]
        if family_candidates:
            candidates = family_candidates

        ref = min(candidates, key=lambda item: dist(item["coord"], base["coord"]))
        parent_branch = nodes[ref["parent"]]
        records.append(
            {
                "child_branch_guid": guid,
                "child_branch_name": node["name"],
                "child_coord_raw": node["coord"],
                "base_guid": base["guid"],
                "base_name": base["name"],
                "attach_coord_raw": base["coord"],
                "base_ref_guid": ref["guid"],
                "base_ref_name": ref["name"],
                "parent_branch_guid": parent_branch["guid"],
                "parent_branch_name": parent_branch["name"],
                "parent_branch_coord_raw": parent_branch["coord"],
            }
        )
    return records, issues


def collect_bones(armature):
    bones = {}
    children = defaultdict(list)
    matrix = armature.matrix_world
    for bone in armature.data.bones:
        head = matrix @ bone.head_local
        tail = matrix @ bone.tail_local
        parent = bone.parent.name if bone.parent else None
        bones[bone.name] = {
            "name": bone.name,
            "parent": parent,
            "head": (head.x, head.y, head.z),
            "tail": (tail.x, tail.y, tail.z),
        }
        if parent:
            children[parent].append(bone.name)
    return bones, children


def unique_scale_candidates(candidates, tolerance=1e-5):
    result = []
    for candidate in candidates:
        try:
            value = float(candidate)
        except (TypeError, ValueError):
            continue
        if not math.isfinite(value) or value <= EPSILON:
            continue
        if any(abs(value - existing) <= tolerance * max(1.0, abs(existing)) for existing in result):
            continue
        result.append(value)
    return result


def point_array(points):
    rows = []
    for point in points:
        if point is None:
            continue
        try:
            row = [float(point[0]), float(point[1]), float(point[2])]
        except (TypeError, ValueError, IndexError):
            continue
        if all(math.isfinite(value) for value in row):
            rows.append(row)
    if not rows:
        return np.empty((0, 3), dtype=np.float64)
    return np.array(rows, dtype=np.float64)


def robust_span_candidates(source_points, target_points):
    source = point_array(source_points)
    target = point_array(target_points)
    if len(source) < 2 or len(target) < 2:
        return []

    source_low, source_high = np.percentile(source, [5.0, 95.0], axis=0)
    target_low, target_high = np.percentile(target, [5.0, 95.0], axis=0)
    source_span = source_high - source_low
    target_span = target_high - target_low

    candidates = []
    source_diag = float(np.linalg.norm(source_span))
    target_diag = float(np.linalg.norm(target_span))
    if source_diag > EPSILON and target_diag > EPSILON:
        candidates.append(source_diag / target_diag)

    for src, dst in zip(source_span, target_span):
        src = float(abs(src))
        dst = float(abs(dst))
        if src > EPSILON and dst > EPSILON:
            candidates.append(src / dst)

    source_radius = np.percentile(np.linalg.norm(source, axis=1), 95.0)
    target_radius = np.percentile(np.linalg.norm(target, axis=1), 95.0)
    if source_radius > EPSILON and target_radius > EPSILON:
        candidates.append(float(source_radius / target_radius))

    return unique_scale_candidates(candidates)


def build_scale_candidates(base_candidates, source_points, target_points):
    return unique_scale_candidates(list(base_candidates) + robust_span_candidates(source_points, target_points))


def choose_scale(records, orphan_roots, bones, candidates):
    if not records or not orphan_roots:
        return candidates[0], []
    candidates = build_scale_candidates(
        candidates,
        [rec["child_coord_raw"] for rec in records],
        [bones[root]["head"] for root in orphan_roots],
    )
    sample_records = records[: min(len(records), 2000)]
    scores = []
    for scale in candidates:
        record_points = [vec_div(rec["child_coord_raw"], scale) for rec in sample_records]
        nearest = []
        # Only a subset of SPM Branch nodes receives bones after Relative
        # rounding. Score from each actual FBX orphan root toward all SPM
        # records, not the other way around; otherwise hundreds of unboned
        # Base children dominate the median and invent scales such as 100-400.
        for root in orphan_roots:
            nearest.append(min(dist(bones[root]["head"], point) for point in record_points))
        nearest.sort()
        scores.append(
            {
                "scale": scale,
                "median_nearest_child_root": nearest[len(nearest) // 2],
                "max_nearest_child_root": nearest[-1],
            }
        )
    scores.sort(key=lambda item: (item["median_nearest_child_root"], item["max_nearest_child_root"]))
    return scores[0]["scale"], scores


def nearest_unused_start(coord, candidates, bones, used):
    best = None
    best_distance = None
    for name in candidates:
        if name in used:
            continue
        distance = dist(coord, bones[name]["head"])
        if best_distance is None or distance < best_distance:
            best = name
            best_distance = distance
    return best, best_distance


def nearest_start(coord, candidates, bones):
    best = None
    best_distance = None
    for name in candidates:
        distance = dist(coord, bones[name]["head"])
        if best_distance is None or distance < best_distance:
            best = name
            best_distance = distance
    return best, best_distance


def branch_chain(start_bone, children):
    chain = []
    stack = [start_bone]
    seen = set()
    while stack:
        name = stack.pop()
        if name in seen:
            continue
        seen.add(name)
        chain.append(name)
        for child in children.get(name, []):
            if child.endswith("_End"):
                stack.append(child)
    return chain


def nearest_bone_on_chain(point, chain, bones):
    best = None
    best_distance = None
    for name in chain:
        bone = bones[name]
        distance = tuple_point_segment_distance(point, bone["head"], bone["tail"])
        if best_distance is None or distance < best_distance:
            best = name
            best_distance = distance
    return best, best_distance


def build_reparent_map(records, bones, children, true_root, scale, tolerance):
    roots = [name for name, bone in bones.items() if bone["parent"] is None]
    orphan_roots = [root for root in roots if root != true_root and root.endswith("_Start")]
    all_starts = [name for name in bones if name.endswith("_Start")]

    mapping = {}
    details = []
    problems = []

    # Match the smaller, authoritative set (actual FBX orphan roots) onto the
    # larger SPM Base/BaseRef record set. A global nearest-pair greedy pass is
    # deterministic and prevents irrelevant unboned SPM branches from
    # consuming roots before their real record is reached.
    pairs = []
    record_points = [vec_div(rec["child_coord_raw"], scale) for rec in records]
    for child_bone in orphan_roots:
        child_head = bones[child_bone]["head"]
        for record_index, child_point in enumerate(record_points):
            pairs.append((dist(child_head, child_point), child_bone, record_index))
    pairs.sort(key=lambda item: (item[0], item[1], item[2]))
    matches = {}
    used_records = set()
    for child_distance, child_bone, record_index in pairs:
        if child_bone in matches or record_index in used_records:
            continue
        matches[child_bone] = (record_index, child_distance)
        used_records.add(record_index)
        if len(matches) == len(orphan_roots):
            break

    for child_bone in orphan_roots:
        match = matches.get(child_bone)
        if not match:
            problems.append({"child_bone": child_bone, "error": "no SPM Base/BaseRef record for orphan root"})
            continue
        record_index, child_distance = match
        rec = records[record_index]
        if child_distance > tolerance:
            problems.append(
                {**rec, "child_bone": child_bone, "error": "child root distance over tolerance", "distance": child_distance}
            )
            continue

        parent_branch_point = vec_div(rec["parent_branch_coord_raw"], scale)
        parent_start, parent_start_distance = nearest_start(parent_branch_point, all_starts, bones)
        if not parent_start:
            problems.append({**rec, "child_bone": child_bone, "error": "no parent branch start"})
            continue

        attach_point = vec_div(rec["attach_coord_raw"], scale)
        chain = branch_chain(parent_start, children)
        parent_bone, parent_distance = nearest_bone_on_chain(attach_point, chain, bones)
        if not parent_bone:
            problems.append({**rec, "child_bone": child_bone, "error": "empty parent branch chain"})
            continue

        mapping[child_bone] = parent_bone
        details.append(
            {
                **rec,
                "child_bone": child_bone,
                "parent_branch_start_bone": parent_start,
                "parent_bone": parent_bone,
                "child_distance": child_distance,
                "parent_branch_start_distance": parent_start_distance,
                "parent_attach_distance": parent_distance,
                "parent_chain_bones": len(chain),
            }
        )

    return mapping, details, problems, roots, orphan_roots


def resolve_true_root_bone(bones, requested):
    roots = [name for name, bone in bones.items() if bone["parent"] is None]
    if requested in bones and requested in roots:
        return requested, "requested"

    def bone_number(name):
        match = re.search(r"(?:^|_)(\d+)(?:_|$)", name)
        return int(match.group(1)) if match else 10**9

    non_start_roots = [name for name in roots if not name.endswith("_Start")]
    candidates = non_start_roots or roots
    if not candidates:
        return requested, "missing"
    return min(candidates, key=lambda name: (bone_number(name), name)), "inferred"


def build_independent_root_preservation_details(root_names, reason):
    return [
        {
            "child_bone": root,
            "parent_bone": None,
            "method": "preserve_independent_root_without_baseref",
            "reason": reason,
        }
        for root in root_names
    ]


def build_root_fallback_reparent_map(bones, true_root):
    """Preserve independent FBX roots when the SPM has no BaseRef contract.

    A Base/BaseRef pair is positive evidence that one SpeedTree branch is
    attached to another.  The absence of every pair is not evidence that all
    roots belong under the first deforming stem.  Ground-cover exports in
    particular intentionally contain many sibling roots, so keep them as
    independent armature bones; the FBX armature-object root will parent them
    in Unreal's single-root reference skeleton.

    The historical function name is retained for add-on/API compatibility.
    """
    roots = [name for name, bone in bones.items() if bone["parent"] is None]
    orphan_roots = [root for root in roots if root != true_root and root.endswith("_Start")]
    details = build_independent_root_preservation_details(
        orphan_roots,
        (
            "SPM contains no Base/BaseRef evidence for a parent-child "
            "relationship"
        ),
    )
    return {}, details, [], roots, orphan_roots


def apply_reparent_mapping(armature, mapping):
    ensure_object_mode()
    require_in_view_layer(armature, "to edit its bones")
    deselect_all()

    # Edit mode requires the armature to be visible.
    previous_hide_viewport = armature.hide_viewport
    armature.hide_viewport = False
    try:
        previous_hidden = armature.hide_get()
        armature.hide_set(False)
    except RuntimeError:
        previous_hidden = False

    bpy.context.view_layer.objects.active = armature
    armature.select_set(True)
    try:
        bpy.ops.object.mode_set(mode="EDIT")
        edit_bones = armature.data.edit_bones
        applied = 0
        for child, parent in mapping.items():
            if child not in edit_bones or parent not in edit_bones:
                continue
            edit_bones[child].use_connect = False
            edit_bones[child].parent = edit_bones[parent]
            applied += 1
        bpy.ops.object.mode_set(mode="OBJECT")
    finally:
        try:
            armature.hide_set(previous_hidden)
        except RuntimeError:
            pass
        armature.hide_viewport = previous_hide_viewport
    return applied


def run_reparent_from_spm(spm_path, armature_name, true_root, scale_value="auto", tolerance=0.08, apply=True, strict=True, report_path=""):
    tree = parse_speedtree(spm_path)
    records, spm_issues = find_base_ref_pairs(tree)
    armature = get_armature(armature_name)
    bones, children = collect_bones(armature)
    requested_true_root = true_root
    true_root, true_root_source = resolve_true_root_bone(bones, requested_true_root)
    roots_before = [name for name, bone in bones.items() if bone["parent"] is None]
    orphan_roots = [root for root in roots_before if root != true_root and root.endswith("_Start")]

    if scale_value == "auto":
        if records and orphan_roots:
            scale, scale_scores = choose_scale(records, orphan_roots, bones, [1.0, 3.28084, 100.0, 0.01])
        else:
            scale, scale_scores = resolve_spm_scale(tree, bones, true_root, scale_value)
    else:
        scale = float(scale_value)
        scale_scores = [{"scale": scale, "manual": True}]

    fallback_reason = ""
    if not records:
        mapping, details, problems, roots_before, orphan_roots = build_root_fallback_reparent_map(bones, true_root)
        fallback_reason = "no_base_ref_records_preserved_independent_roots"
    else:
        mapping, details, problems, roots_before, orphan_roots = build_reparent_map(
            records, bones, children, true_root, scale, tolerance
        )
        unmatched_orphans = [root for root in orphan_roots if root not in mapping]
        if unmatched_orphans:
            # Some files mix BaseRef-attached branches with independent
            # top-level stems.  An unmatched root has no positive attachment
            # evidence, so preserve it instead of inventing a relationship to
            # the first deforming root.
            details.extend(
                build_independent_root_preservation_details(
                    unmatched_orphans,
                    "no usable Base/BaseRef match for this FBX root",
                )
            )
            fallback_reason = (
                "unmatched_orphan_roots_preserved_without_baseref_match"
            )

    preserved_independent_roots = [
        detail["child_bone"]
        for detail in details
        if str(detail.get("method", "")).startswith(
            "preserve_independent_root_"
        )
    ]

    report = {
        "blend": bpy.data.filepath,
        "spm": spm_path,
        "spm_version": tree["version"],
        "armature": armature.name,
        "bones": len(bones),
        "roots_before": len(roots_before),
        "root_names_before": roots_before[:100],
        "true_root": true_root,
        "requested_true_root": requested_true_root,
        "true_root_source": true_root_source,
        "orphan_start_roots": len(orphan_roots),
        "base_ref_records": len(records),
        "mapping_count": len(mapping),
        "scale": scale,
        "scale_scores": scale_scores,
        "spm_issues": spm_issues[:80],
        "problem_count": len(problems),
        "problems": problems[:80],
        "mapping": mapping,
        "details_sample": details[:30],
        "fallback_reason": fallback_reason,
        "fallback_mapping_count": sum(
            1 for detail in details if str(detail.get("method", "")).startswith("fallback_")
        ),
        "preserved_independent_root_count": sum(
            1
            for detail in details
            if str(detail.get("method", "")).startswith(
                "preserve_independent_root_"
            )
        ),
        "preserved_independent_roots": preserved_independent_roots[:100],
        "root_parent_contract": (
            "base_ref_evidence_only"
            if records
            else "independent_roots_without_base_ref"
        ),
        "applied": False,
    }

    # Every root must be accounted for either by a BaseRef-backed mapping or by
    # explicit independent-root preservation. Strict mode must not reinterpret
    # the absence of a relationship as an incomplete map.
    processed_roots = set(mapping) | set(preserved_independent_roots)
    blocked = strict and processed_roots != set(orphan_roots)
    if blocked:
        report["status"] = "blocked"
        report["error"] = "Reparent mapping was not complete; no changes applied."
    elif apply:
        applied = apply_reparent_mapping(armature, mapping)
        roots_after = [bone.name for bone in armature.data.bones if bone.parent is None]
        report.update(
            {
                "status": (
                    "applied_with_independent_roots_preserved"
                    if mapping and preserved_independent_roots
                    else "applied"
                    if mapping
                    else "preserved_independent_roots"
                ),
                "applied": True,
                "applied_count": applied,
                "roots_after": len(roots_after),
                "root_names_after": roots_after,
            }
        )
    else:
        report["status"] = "dry-run-ok"

    write_report(report_path, report)
    return report


# ---------------------------------------------------------------------------
# Bone segment helpers (numpy accelerated nearest-bone queries)
# ---------------------------------------------------------------------------


def bone_world_segments(armature):
    matrix = armature.matrix_world
    segments = {}
    for bone in armature.data.bones:
        segments[bone.name] = (matrix @ bone.head_local, matrix @ bone.tail_local)
    return segments


def segments_to_arrays(segments, names):
    heads = np.empty((len(names), 3), dtype=np.float64)
    tails = np.empty((len(names), 3), dtype=np.float64)
    for index, name in enumerate(names):
        head, tail = segments[name]
        heads[index] = (head.x, head.y, head.z)
        tails[index] = (tail.x, tail.y, tail.z)
    return heads, tails


def nearest_segment(point, names, heads, tails):
    p = np.array((point[0], point[1], point[2]), dtype=np.float64)
    d = tails - heads
    l2 = np.einsum("ij,ij->i", d, d)
    t = np.clip(np.einsum("ij,ij->i", p - heads, d) / np.maximum(l2, 1e-12), 0.0, 1.0)
    projected = heads + t[:, None] * d
    delta = projected - p
    dist2 = np.einsum("ij,ij->i", delta, delta)
    index = int(np.argmin(dist2))
    return names[index], float(math.sqrt(dist2[index]))


# ---------------------------------------------------------------------------
# SPM leaf node -> armature bone mapping
# ---------------------------------------------------------------------------


def resolve_spm_scale(tree, bones, true_root, scale_value="auto"):
    if str(scale_value).lower() != "auto":
        return float(scale_value), []

    records, _issues = find_base_ref_pairs(tree)
    roots = [name for name, bone in bones.items() if bone["parent"] is None]
    orphan_roots = [root for root in roots if root != true_root and root.endswith("_Start")]
    if records and orphan_roots:
        return choose_scale(records, orphan_roots, bones, [1.0, 3.28084, 100.0, 0.01])

    branch_nodes = [node for node in tree["nodes"].values() if node["type"] == "Branch" and node["coord"]]
    start_bones = [name for name in bones if name.endswith("_Start")]
    if not branch_nodes or not start_bones:
        return 1.0, [{"scale": 1.0, "fallback": "no branch nodes or start bones"}]
    scores = []
    sample = branch_nodes[: min(len(branch_nodes), 500)]
    candidates = build_scale_candidates(
        [1.0, 3.28084, 100.0, 0.01],
        [node["coord"] for node in branch_nodes],
        [bones[name]["head"] for name in start_bones],
    )
    for scale in candidates:
        nearest = []
        for node in sample:
            point = vec_div(node["coord"], scale)
            nearest.append(min(dist(point, bones[name]["head"]) for name in start_bones))
        nearest.sort()
        scores.append({"scale": scale, "median_nearest_branch_start": nearest[len(nearest) // 2] if nearest else None})
    scores.sort(key=lambda item: item["median_nearest_branch_start"] if item["median_nearest_branch_start"] is not None else float("inf"))
    return scores[0]["scale"], scores


def branch_node_bone_map(tree, bones, scale):
    all_starts = [name for name in bones if name.endswith("_Start")]
    mapping = {}
    for guid, node in tree["nodes"].items():
        if node["type"] != "Branch" or not node["coord"]:
            continue
        bone_name, distance = nearest_start(vec_div(node["coord"], scale), all_starts, bones)
        if bone_name:
            mapping[guid] = {"bone": bone_name, "distance": distance}
    return mapping


def parent_branch_node(tree, node):
    nodes = tree["nodes"]
    parent_guid = node.get("parent")
    seen = set()
    while parent_guid and parent_guid not in seen:
        seen.add(parent_guid)
        parent = nodes.get(parent_guid)
        if not parent:
            return None
        if parent["type"] == "Branch":
            return parent
        parent_guid = parent.get("parent")
    return None


def mesh_component_centroid_tree(mesh_obj):
    components = mesh_connected_components(mesh_obj.data)
    tree = kdtree.KDTree(len(components))
    matrix = mesh_obj.matrix_world
    for index, component in enumerate(components):
        centroid = matrix @ (component["sum"] / max(len(component["vertices"]), 1))
        component["centroid_world"] = centroid
        tree.insert(centroid, index)
    tree.balance()
    return components, tree


def leaf_nodes_for_targets(tree, include_base_leaf_nodes=False):
    generators = tree.get("generators", {})
    nodes = []
    for node in tree["nodes"].values():
        if node["type"] == "Leaf Mesh":
            generator = generators.get(node.get("gen"), {})
            if generator.get("hidden"):
                continue
        elif include_base_leaf_nodes and node["type"] in {"Base", "BaseRef"}:
            pass
        else:
            continue
        if "leaf" not in (node["name"] or "").lower():
            continue
        if not node.get("coord"):
            continue
        if not node.get("valid_position", True):
            continue
        nodes.append(node)
    return nodes


def choose_spm_leaf_scale(leaf_nodes, component_tree=None, component_points=None, base_candidates=None):
    candidates = build_scale_candidates(
        base_candidates or [3.28084, 1.0, 30.48, 100.0, 0.01],
        [node["coord"] for node in leaf_nodes],
        component_points or [],
    )
    scores = []
    sample_count = min(len(leaf_nodes), 5000)
    sample_step = max(1, len(leaf_nodes) // sample_count) if sample_count else 1
    sample = leaf_nodes[::sample_step][:sample_count]
    if component_tree and sample:
        for scale in candidates:
            distances = []
            for node in sample:
                point = Vector(vec_div(node["coord"], scale))
                _co, _index, distance = component_tree.find(point)
                distances.append(distance)
            distances.sort()
            scores.append(
                {
                    "scale": scale,
                    "median_leaf_component_distance": distances[len(distances) // 2],
                    "p90_leaf_component_distance": distances[int(len(distances) * 0.9)],
                }
            )
        scores.sort(key=lambda item: item["median_leaf_component_distance"])
        return scores[0]["scale"], scores
    return candidates[0], []


def collect_spm_leaf_targets(
    spm_path,
    armature,
    true_root="Bone_1_Start",
    scale_value="auto",
    leaf_mesh_obj=None,
    include_base_leaf_nodes=False,
):
    if not spm_path or not Path(spm_path).exists():
        return {"status": "missing-spm", "targets": []}

    tree = parse_speedtree(spm_path)
    bones, children = collect_bones(armature)
    reparent_scale, reparent_scale_scores = resolve_spm_scale(tree, bones, true_root, scale_value)
    leaf_nodes = leaf_nodes_for_targets(tree, include_base_leaf_nodes=include_base_leaf_nodes)
    component_count = None
    leaf_scale = reparent_scale
    leaf_scale_scores = []
    if str(scale_value).lower() == "auto":
        component_tree = None
        if leaf_mesh_obj is not None:
            components, component_tree = mesh_component_centroid_tree(leaf_mesh_obj)
            component_count = len(components)
            component_points = [component["centroid_world"] for component in components]
            leaf_scale, leaf_scale_scores = choose_spm_leaf_scale(
                leaf_nodes,
                component_tree=component_tree,
                component_points=component_points,
                base_candidates=[reparent_scale, 3.28084, 1.0, 30.48, 100.0, 0.01],
            )

    scale = leaf_scale
    branch_map = branch_node_bone_map(tree, bones, scale)
    targets = []
    skipped = Counter()

    for node in leaf_nodes:
        branch = parent_branch_node(tree, node)
        if not branch:
            skipped["missing_parent_branch"] += 1
            continue
        branch_match = branch_map.get(branch["guid"])
        if not branch_match:
            skipped["unmatched_parent_branch"] += 1
            continue

        branch_bone = branch_match["bone"]
        chain = branch_chain(branch_bone, children)
        point = vec_div(node["coord"], scale)
        bone_name, bone_distance = nearest_bone_on_chain(point, chain, bones)
        if not bone_name:
            skipped["empty_parent_chain"] += 1
            continue

        targets.append(
            {
                "guid": node["guid"],
                "name": node["name"],
                "type": node["type"],
                "generator": tree.get("generators", {}).get(node.get("gen"), {}).get("name", ""),
                "point": point,
                "bone": bone_name,
                "parent_branch_guid": branch["guid"],
                "parent_branch_name": branch["name"],
                "parent_branch_bone": branch_bone,
                "parent_branch_distance": branch_match["distance"],
                "bone_distance": bone_distance,
            }
        )

    return {
        "status": "ok" if targets else "no-targets",
        "spm": str(spm_path),
        "spm_version": tree.get("version"),
        "scale": scale,
        "leaf_scale": leaf_scale,
        "leaf_scale_scores": leaf_scale_scores,
        "reparent_scale": reparent_scale,
        "reparent_scale_scores": reparent_scale_scores,
        "leaf_node_count": len(leaf_nodes),
        "leaf_component_count": component_count,
        "targets": targets,
        "target_count": len(targets),
        "skipped": dict(skipped),
    }


def mesh_connected_components(mesh):
    vertex_count = len(mesh.vertices)
    parent = list(range(vertex_count))
    rank = [0] * vertex_count

    def find(index):
        root = index
        while parent[root] != root:
            root = parent[root]
        while parent[index] != index:
            next_index = parent[index]
            parent[index] = root
            index = next_index
        return root

    def union(a, b):
        ra = find(a)
        rb = find(b)
        if ra == rb:
            return
        if rank[ra] < rank[rb]:
            parent[ra] = rb
        elif rank[ra] > rank[rb]:
            parent[rb] = ra
        else:
            parent[rb] = ra
            rank[ra] += 1

    for polygon in mesh.polygons:
        vertices = polygon.vertices
        if len(vertices) < 2:
            continue
        first = vertices[0]
        for vertex_index in vertices[1:]:
            union(first, vertex_index)

    components = {}
    for vertex in mesh.vertices:
        root = find(vertex.index)
        entry = components.get(root)
        if entry is None:
            entry = {"vertices": [], "sum": Vector((0.0, 0.0, 0.0))}
            components[root] = entry
        entry["vertices"].append(vertex.index)
        entry["sum"] += vertex.co
    return list(components.values())


def assign_mesh_components_from_spm_leaf_targets(obj, duplicate, targets):
    if not targets:
        return None
    target_bones = {target["bone"] for target in targets if target.get("bone")}
    if len(target_bones) <= 1 and len(targets) > 16:
        return None

    tree = kdtree.KDTree(len(targets))
    for index, target in enumerate(targets):
        tree.insert(Vector(target["point"]), index)
    tree.balance()

    components = mesh_connected_components(duplicate.data)
    groups_by_bone = defaultdict(list)
    distances = []
    sample_assignments = []
    matrix = obj.matrix_world

    for component_index, component in enumerate(components):
        centroid_local = component["sum"] / max(len(component["vertices"]), 1)
        centroid_world = matrix @ centroid_local
        _co, target_index, distance = tree.find(centroid_world)
        target = targets[target_index]
        groups_by_bone[target["bone"]].extend(component["vertices"])
        distances.append(distance)
        if len(sample_assignments) < 50:
            sample_assignments.append(
                {
                    "component": component_index,
                    "vertices": len(component["vertices"]),
                    "bone": target["bone"],
                    "leaf_node": target["name"],
                    "distance": distance,
                }
            )

    for bone_name, vertex_indices in groups_by_bone.items():
        duplicate.vertex_groups.new(name=bone_name).add(vertex_indices, 1.0, "REPLACE")

    distances.sort()
    return {
        "method": "spm_leaf_node_parent_branch",
        "weighting": "spm_leaf_node_parent_branch",
        "components": len(components),
        "targets": len(targets),
        "assigned_bones": len(groups_by_bone),
        "mean_match_distance": float(sum(distances) / len(distances)) if distances else 0.0,
        "median_match_distance": float(distances[len(distances) // 2]) if distances else 0.0,
        "max_match_distance": float(distances[-1]) if distances else 0.0,
        "sample_assignments": sample_assignments,
    }


def collect_skinned_surface_targets(armature, exclude_names=None):
    exclude_names = set(exclude_names or [])
    valid_bones = {bone.name for bone in armature.data.bones}
    points = []
    for obj in bpy.context.scene.objects:
        if obj.name in exclude_names or obj.type != "MESH":
            continue
        if "mergedskinned" in obj.name.lower():
            continue
        if any(slot.material and "leaf" in slot.material.name.lower() for slot in obj.material_slots):
            continue
        if not obj.vertex_groups or not armature_modifier(obj, armature):
            continue
        names_by_index = {group.index: group.name for group in obj.vertex_groups}
        matrix = obj.matrix_world
        for vertex in obj.data.vertices:
            best_name = None
            best_weight = 0.0
            for group_ref in vertex.groups:
                if group_ref.weight <= best_weight:
                    continue
                name = names_by_index.get(group_ref.group)
                if name in valid_bones:
                    best_name = name
                    best_weight = group_ref.weight
            if best_name:
                co = matrix @ vertex.co
                points.append(((co.x, co.y, co.z), best_name, obj.name))

    if not points:
        return {"status": "no-skinned-surface-targets", "points": [], "tree": None}

    tree = kdtree.KDTree(len(points))
    for index, (co, _bone, _object_name) in enumerate(points):
        tree.insert(Vector(co), index)
    tree.balance()
    return {"status": "ok", "points": points, "tree": tree}


def assign_mesh_components_from_skinned_surface(obj, duplicate, surface_targets):
    if not surface_targets or surface_targets.get("status") != "ok":
        return None

    components = mesh_connected_components(duplicate.data)
    groups_by_bone = defaultdict(list)
    distances = []
    sample_assignments = []
    matrix = obj.matrix_world
    points = surface_targets["points"]
    tree = surface_targets["tree"]

    for component_index, component in enumerate(components):
        centroid_local = component["sum"] / max(len(component["vertices"]), 1)
        centroid_world = matrix @ centroid_local
        _co, target_index, distance = tree.find(centroid_world)
        _point, bone_name, source_object = points[target_index]
        groups_by_bone[bone_name].extend(component["vertices"])
        distances.append(distance)
        if len(sample_assignments) < 50:
            sample_assignments.append(
                {
                    "component": component_index,
                    "vertices": len(component["vertices"]),
                    "bone": bone_name,
                    "source_object": source_object,
                    "distance": distance,
                }
            )

    for bone_name, vertex_indices in groups_by_bone.items():
        duplicate.vertex_groups.new(name=bone_name).add(vertex_indices, 1.0, "REPLACE")

    distances.sort()
    return {
        "method": "skinned_surface_component_match",
        "weighting": "skinned_surface_component_match",
        "components": len(components),
        "surface_points": len(points),
        "assigned_bones": len(groups_by_bone),
        "mean_match_distance": float(sum(distances) / len(distances)) if distances else 0.0,
        "median_match_distance": float(distances[len(distances) // 2]) if distances else 0.0,
        "max_match_distance": float(distances[-1]) if distances else 0.0,
        "sample_assignments": sample_assignments,
    }


# ---------------------------------------------------------------------------
# SpeedTree XML bone metadata (Generator/Mass/Radius -> wind simulation groups)
# ---------------------------------------------------------------------------
#
# The SpeedTreeRaw XML export carries per-bone semantic data the FBX lacks:
# Generator names (Trunk / Big N / Branch N / Root Twigs...), Mass, and Radius.
# The Unreal-side DynamicWind import consumes "JointName -> SimulationGroupIndex",
# so we match XML bones to armature bones by segment position (same idea as the
# SPM reparent matching) and emit per-bone group suggestions into the JSON.
# Note the XML bone hierarchy (ParentID) is broken the same way as the FBX
# (orphan branch roots), so the SPM reparent step stays authoritative.

XML_ATTR_RE = re.compile(r'([A-Za-z][A-Za-z0-9]*)="([^"]*)"')


def _speedtree_xml_number(value):
    """Parse the invariant or locale-comma decimal emitted by SpeedTree Raw XML."""
    text = str("" if value is None else value).strip()
    if not text:
        return 0.0
    if "," in text:
        if "." in text or text.count(",") != 1:
            raise ValueError(f"ambiguous SpeedTree XML decimal: {text!r}")
        text = text.replace(",", ".")
    number = float(text)
    if not math.isfinite(number):
        raise ValueError(f"non-finite SpeedTree XML decimal: {text!r}")
    return number


def parse_speedtree_xml_bones(xml_path):
    path = Path(xml_path)
    if not xml_path or not path.exists():
        raise RuntimeError(f"SpeedTree XML does not exist: {xml_path}")
    bones = []
    # The raw XML can be huge (vertex arrays); scan for <Bone .../> lines only
    # instead of parsing the whole document.
    with open(path, "r", encoding="utf-8", errors="ignore") as handle:
        for line in handle:
            if "<Bone " not in line:
                continue
            for chunk in line.split("<Bone ")[1:]:
                close = chunk.find(">")
                attrs = dict(XML_ATTR_RE.findall(chunk[: close if close >= 0 else None]))
                try:
                    bones.append(
                        {
                            "id": int(attrs["ID"]),
                            "parent_id": int(attrs.get("ParentID", "-1")),
                            "radius": _speedtree_xml_number(attrs.get("Radius", "0")),
                            "start": (
                                _speedtree_xml_number(attrs["StartX"]),
                                _speedtree_xml_number(attrs["StartY"]),
                                _speedtree_xml_number(attrs["StartZ"]),
                            ),
                            "end": (
                                _speedtree_xml_number(attrs["EndX"]),
                                _speedtree_xml_number(attrs["EndY"]),
                                _speedtree_xml_number(attrs["EndZ"]),
                            ),
                            "mass": _speedtree_xml_number(attrs.get("Mass", "0")),
                            "generator": attrs.get("Generator", ""),
                        }
                    )
                except (KeyError, ValueError):
                    continue
    if not bones:
        raise RuntimeError(f"No <Bone> entries found in SpeedTree XML: {xml_path}")
    return bones


def choose_xml_scale(xml_bones, head_array, candidates=(100.0, 1.0, 3.28084, 30.48, 0.01)):
    sample = xml_bones[: min(len(xml_bones), 300)]
    candidates = build_scale_candidates(
        candidates,
        [bone["start"] for bone in xml_bones],
        head_array.tolist() if hasattr(head_array, "tolist") else head_array,
    )
    scores = []
    for scale in candidates:
        nearest = []
        for bone in sample:
            point = np.array(bone["start"], dtype=np.float64) / scale
            nearest.append(float(np.sqrt(((head_array - point) ** 2).sum(axis=1)).min()))
        nearest.sort()
        scores.append({"scale": scale, "median_nearest": nearest[len(nearest) // 2]})
    scores.sort(key=lambda item: item["median_nearest"])
    return scores[0]["scale"], scores


def build_simulation_groups(xml_bones, trunk_generator_regex="trunk"):
    # Group bones by SpeedTree Generator. Trunk generators become simulation
    # group 0 (bIsTrunkGroup on the Unreal side); the rest are ordered by mean
    # bone mass descending, so heavier/stiffer levels get lower indices.
    stats = {}
    for bone in xml_bones:
        entry = stats.setdefault(bone["generator"], {"count": 0, "mass_total": 0.0, "radius_total": 0.0})
        entry["count"] += 1
        entry["mass_total"] += bone["mass"]
        entry["radius_total"] += bone["radius"]
    trunk_pattern = compile_optional_regex(trunk_generator_regex)
    trunk_generators = sorted(name for name in stats if trunk_pattern and trunk_pattern.search(name))
    others = sorted(
        (name for name in stats if name not in trunk_generators),
        key=lambda name: stats[name]["mass_total"] / max(stats[name]["count"], 1),
        reverse=True,
    )

    groups = []
    group_of_generator = {}
    if trunk_generators:
        trunk_count = sum(stats[name]["count"] for name in trunk_generators)
        groups.append(
            {
                "index": 0,
                "generators": trunk_generators,
                "is_trunk_group": True,
                "bone_count": trunk_count,
                "mean_mass": round(
                    sum(stats[name]["mass_total"] for name in trunk_generators) / max(trunk_count, 1), 4
                ),
                "mean_radius": round(
                    sum(stats[name]["radius_total"] for name in trunk_generators) / max(trunk_count, 1), 4
                ),
            }
        )
        for name in trunk_generators:
            group_of_generator[name] = 0
    for name in others:
        index = len(groups)
        groups.append(
            {
                "index": index,
                "generators": [name],
                "is_trunk_group": False,
                "bone_count": stats[name]["count"],
                "mean_mass": round(stats[name]["mass_total"] / max(stats[name]["count"], 1), 4),
                "mean_radius": round(stats[name]["radius_total"] / max(stats[name]["count"], 1), 4),
            }
        )
        group_of_generator[name] = index
    return groups, group_of_generator


def build_xml_bone_metadata(xml_path, armature, trunk_generator_regex="trunk"):
    xml_bones = parse_speedtree_xml_bones(xml_path)
    matrix = armature.matrix_world
    names = []
    heads = np.empty((len(armature.data.bones), 3), dtype=np.float64)
    tails = np.empty((len(armature.data.bones), 3), dtype=np.float64)
    for index, bone in enumerate(armature.data.bones):
        names.append(bone.name)
        head = matrix @ bone.head_local
        tail = matrix @ bone.tail_local
        heads[index] = (head.x, head.y, head.z)
        tails[index] = (tail.x, tail.y, tail.z)

    scale, scale_scores = choose_xml_scale(xml_bones, heads)
    starts = np.array([bone["start"] for bone in xml_bones], dtype=np.float64) / scale
    ends = np.array([bone["end"] for bone in xml_bones], dtype=np.float64) / scale

    groups, group_of_generator = build_simulation_groups(xml_bones, trunk_generator_regex)

    bone_records = []
    distances = []
    for index, name in enumerate(names):
        # FBX bones are joints (Blender synthesizes their tails on import), so
        # match each joint head against XML segment endpoints: a _Start joint
        # sits on its segment's start, chain/tip _End joints sit on segment
        # boundaries/ends. On exact ties (a child branch root on its parent's
        # segment end) prefer the start-endpoint match — the joint owns the
        # segment it starts.
        d_start = np.linalg.norm(starts - heads[index], axis=1)
        d_end = np.linalg.norm(ends - heads[index], axis=1)
        score = np.where(d_start <= d_end, d_start, d_end + 1e-9)
        best = int(np.argmin(score))
        xml_bone = xml_bones[best]
        distance = float(min(d_start[best], d_end[best]))
        distances.append(distance)
        bone_records.append(
            {
                "name": name,
                "bone_index": index,
                "parent_index": (
                    armature.data.bones.find(armature.data.bones[index].parent.name)
                    if armature.data.bones[index].parent is not None
                    else -1
                ),
                "xml_id": xml_bone["id"],
                "generator": xml_bone["generator"],
                "mass": xml_bone["mass"],
                "radius": xml_bone["radius"],
                "group": group_of_generator.get(xml_bone["generator"], 0),
                "match_distance": round(distance, 4),
            }
        )

    distances.sort()
    info = {
        "source": str(xml_path),
        "xml_bone_count": len(xml_bones),
        "armature_bone_count": len(names),
        "scale": scale,
        "scale_scores": scale_scores,
        "trunk_generator_regex": trunk_generator_regex,
        "generators": {name: index for name, index in sorted(group_of_generator.items())},
        "simulation_groups": groups,
        "match": {
            "median_distance": round(distances[len(distances) // 2], 4) if distances else None,
            "max_distance": round(distances[-1], 4) if distances else None,
        },
    }
    return bone_records, info


# ---------------------------------------------------------------------------
# Loose instance skinning
# ---------------------------------------------------------------------------


def find_skinned_parent(obj, armature):
    current = obj.parent
    while current:
        if current.type == "MESH":
            for modifier in current.modifiers:
                if modifier.type == "ARMATURE" and modifier.object == armature:
                    return current
        current = current.parent
    return None


def choose_bone_for_instance(obj, skinned_parent, armature, segments, candidate_cache, fallback_all_bones=False):
    key = skinned_parent.name
    cached = candidate_cache.get(key)
    if cached is None:
        names = [group.name for group in skinned_parent.vertex_groups if group.name in segments and group.name != "Root"]
        if not names and fallback_all_bones:
            names = [name for name in segments if name != "Root"]
        if names:
            heads, tails = segments_to_arrays(segments, names)
            cached = (names, heads, tails)
        else:
            cached = ((), None, None)
        candidate_cache[key] = cached
    names, heads, tails = cached
    if not names:
        return None, None

    corners = obj.bound_box
    center_local = Vector(
        (
            sum(corner[0] for corner in corners) / 8.0,
            sum(corner[1] for corner in corners) / 8.0,
            sum(corner[2] for corner in corners) / 8.0,
        )
    )
    center = obj.matrix_world @ center_local
    return nearest_segment(center, names, heads, tails)


def collect_loose_instances(
    armature,
    name_contains,
    source_object_names=None,
    exclude_object_names=None,
):
    instances = []
    needle = name_contains.lower()
    source_names = (
        {str(name) for name in source_object_names}
        if source_object_names is not None
        else None
    )
    excluded_names = {str(name) for name in (exclude_object_names or [])}
    for obj in bpy.context.scene.objects:
        if obj.type != "MESH":
            continue
        if source_names is not None and obj.name not in source_names:
            continue
        if obj.name in excluded_names:
            continue
        if is_cluster_normalizer_generated(obj):
            continue
        if needle and needle not in obj.name.lower():
            continue
        if len(obj.data.vertices) == 0:
            continue
        if obj.vertex_groups:
            continue
        if any(modifier.type == "ARMATURE" for modifier in obj.modifiers):
            continue
        skinned_parent = find_skinned_parent(obj, armature)
        if skinned_parent:
            instances.append((obj, skinned_parent))
            continue
        # SpeedTree 10.1 material-grouped exports can place all leaf geometry
        # under a zero-face source container rather than under a skinned parent
        # mesh. Still create a skinned replacement object for it; the following
        # weight-repair stage will fill zero-weight vertices by nearest bone.
        instances.append((obj, None))
    return instances


def normalize_loose_speedtree_uv_contract(obj):
    """Promote authored loose-plane UVs into the final SpeedTree contract.

    Blender-authored Cluster planes commonly carry one default ``UVMap``.
    Joining that mesh ahead of imported SpeedTree geometry makes Blender keep
    ``UVMap`` at index 0 and zero-fill the later ``uv0``/``blend_ao`` layers for
    the plane faces.  Preserve the authored coordinates by renaming that sole
    layer to ``uv0`` before the join, then append neutral blend/AO values.

    Ambiguous layouts fail closed; this function never deletes or guesses
    between multiple authored UV sets.
    """
    if obj is None or obj.type != "MESH" or not obj.data:
        raise RuntimeError("Loose SpeedTree UV normalization requires a mesh")
    uv_layers = obj.data.uv_layers
    names_before = [layer.name for layer in uv_layers]
    uv0 = uv_layers.get("uv0")
    legacy = uv_layers.get("UVMap")
    renamed = False
    if uv0 is None:
        if legacy is None:
            raise RuntimeError(
                f"Loose SpeedTree plane has no uv0/UVMap layer: {obj.name}"
            )
        legacy.name = "uv0"
        uv0 = legacy
        renamed = True
    if len(uv_layers) == 0 or uv_layers[0] != uv0:
        raise RuntimeError(
            f"Loose SpeedTree plane uv0 is not index 0: {obj.name}: "
            + ", ".join(layer.name for layer in uv_layers)
        )

    blend_ao = uv_layers.get("blend_ao")
    created_blend_ao = False
    if blend_ao is None:
        if len(uv_layers) != 1:
            raise RuntimeError(
                f"Loose SpeedTree plane has ambiguous UV layers: {obj.name}: "
                + ", ".join(layer.name for layer in uv_layers)
            )
        blend_ao = uv_layers.new(name="blend_ao")
        neutral_values = [1.0] * (len(blend_ao.data) * 2)
        try:
            blend_ao.data.foreach_set("uv", neutral_values)
        except (AttributeError, TypeError, ValueError):
            for item in blend_ao.data:
                item.uv = (1.0, 1.0)
        created_blend_ao = True

    names_after = [layer.name for layer in uv_layers]
    if names_after != ["uv0", "blend_ao"]:
        raise RuntimeError(
            f"Loose SpeedTree plane UV contract is not uv0/blend_ao: "
            f"{obj.name}: {', '.join(names_after)}"
        )
    return {
        "status": "normalized" if renamed or created_blend_ao else "preserved",
        "layers_before": names_before,
        "layers_after": names_after,
        "renamed_uvmap_to_uv0": renamed,
        "created_neutral_blend_ao": created_blend_ao,
    }


def build_skinned_instance_mesh(armature, instances, out_name, hide_originals, fallback_all_bones, leaf_targets=None):
    # Duplicate each loose instance, weight it 1.0 to its nearest branch bone,
    # then join. Join preserves UV layers, color attributes, custom split
    # normals, and materials — the previous from_pydata rebuild lost all of
    # them.
    segments = bone_world_segments(armature)
    candidate_cache = {}
    copies = []
    assignments = []
    skipped = []
    source_names = {obj.name for obj, _parent in instances}
    surface_targets = collect_skinned_surface_targets(armature, exclude_names=source_names)
    ensure_object_mode()

    for obj, skinned_parent in instances:
        duplicate = obj.copy()
        duplicate.data = obj.data.copy()
        duplicate.modifiers.clear()
        duplicate.vertex_groups.clear()
        duplicate.hide_viewport = False
        duplicate.hide_render = False
        uv_contract = normalize_loose_speedtree_uv_contract(duplicate)
        bpy.context.scene.collection.objects.link(duplicate)
        if skinned_parent:
            bone_name, bone_distance = choose_bone_for_instance(
                obj, skinned_parent, armature, segments, candidate_cache, fallback_all_bones
            )
            if not bone_name:
                remove_object_and_orphan_mesh(duplicate)
                skipped.append({"object": obj.name, "reason": "no_candidate_bone", "parent": skinned_parent.name})
                continue
            group = duplicate.vertex_groups.new(name=bone_name)
            group.add(list(range(len(duplicate.data.vertices))), 1.0, "REPLACE")
            assignment = {
                "object": obj.name,
                "parent_mesh": skinned_parent.name,
                "bone": bone_name,
                "vertex_count": len(obj.data.vertices),
                "distance": bone_distance,
                "uv_contract": uv_contract,
            }
        else:
            spm_assignment = assign_mesh_components_from_spm_leaf_targets(obj, duplicate, leaf_targets or [])
            surface_assignment = None
            if not spm_assignment:
                surface_assignment = assign_mesh_components_from_skinned_surface(obj, duplicate, surface_targets)
            assignment = {
                "object": obj.name,
                "parent_mesh": "",
                "bone": "",
                "vertex_count": len(obj.data.vertices),
                "distance": None,
                "weighting": "deferred_to_weight_repair_nearest_bone",
                "uv_contract": uv_contract,
            }
            if spm_assignment:
                assignment.update(spm_assignment)
            elif surface_assignment:
                assignment.update(surface_assignment)
        copies.append(duplicate)
        assignments.append(assignment)

        if hide_originals:
            obj.hide_viewport = True
            obj.hide_render = True

    if not copies:
        return None, assignments, skipped

    leftover_meshes = [duplicate.data for duplicate in copies[1:]]
    out_obj = join_objects(copies)
    out_obj.name = out_name
    out_obj.data.name = out_name + "Mesh"
    for mesh in leftover_meshes:
        if mesh.users == 0:
            bpy.data.meshes.remove(mesh)

    parent_keep_world(out_obj, armature)
    modifier = out_obj.modifiers.new(name="Armature", type="ARMATURE")
    modifier.object = armature
    return out_obj, assignments, skipped


def run_skin_loose_instances(
    armature_name,
    name_contains,
    out_name,
    hide_originals=True,
    fallback_all_bones=False,
    apply=True,
    report_path="",
    spm_path="",
    true_root="Bone_1_Start",
    scale_value="auto",
    source_object_names=None,
    exclude_object_names=None,
):
    armature = get_armature(armature_name)
    instances = collect_loose_instances(
        armature,
        name_contains,
        source_object_names=source_object_names,
        exclude_object_names=exclude_object_names,
    )
    leaf_target_info = {"status": "not-requested", "targets": []}
    if spm_path:
        leaf_mesh_obj = next((obj for obj, parent in instances if parent is None), None)
        leaf_target_info = collect_spm_leaf_targets(
            spm_path,
            armature,
            true_root=true_root,
            scale_value=scale_value,
            leaf_mesh_obj=leaf_mesh_obj,
        )
    report = {
        "file": bpy.data.filepath,
        "armature": armature.name,
        "name_contains": name_contains,
        "candidate_instances": len(instances),
        "source_objects": [obj.name for obj, _parent in instances],
        "source_scope_restricted": source_object_names is not None,
        "excluded_source_objects": sorted(
            {str(name) for name in (exclude_object_names or [])}
        ),
        "spm_leaf_targets": {
            key: value
            for key, value in leaf_target_info.items()
            if key not in {"targets", "scale_scores"}
        },
        "apply": apply,
    }
    if not apply:
        report["status"] = "dry-run-ok"
    elif not instances:
        report["status"] = "skipped-no-instances"
    else:
        remove_object_and_orphan_mesh(bpy.data.objects.get(out_name))
        out_obj, assignments, skipped = build_skinned_instance_mesh(
            armature,
            instances,
            out_name,
            hide_originals,
            fallback_all_bones,
            leaf_targets=leaf_target_info.get("targets", []),
        )
        if out_obj is None:
            report.update(
                {
                    "status": "skipped-no-assignable-instances",
                    "skipped_instances": len(skipped),
                    "skipped": skipped[:50],
                }
            )
        else:
            report.update(
                {
                    "status": "applied",
                    "created_object": out_obj.name,
                    "created_vertices": len(out_obj.data.vertices),
                    "created_faces": len(out_obj.data.polygons),
                    "created_uv_layers": [layer.name for layer in out_obj.data.uv_layers],
                    "created_color_attributes": [attr.name for attr in out_obj.data.color_attributes],
                    "assigned_instances": len(assignments),
                    "skipped_instances": len(skipped),
                    "sample_assignments": assignments[:50],
                    "skipped": skipped[:50],
                }
            )
    write_report(report_path, report)
    return report


# ---------------------------------------------------------------------------
# Weight repair
# ---------------------------------------------------------------------------


def armature_modifier(obj, armature):
    for modifier in obj.modifiers:
        if modifier.type == "ARMATURE" and modifier.object == armature:
            return modifier
    return None


def group_name_by_index(obj):
    return {group.index: group.name for group in obj.vertex_groups}


def candidate_bones_for_object(obj, armature, valid_bones, fallback_all=True):
    candidates = set()
    for group in obj.vertex_groups:
        if group.name in valid_bones:
            candidates.add(group.name)
    for name in list(candidates):
        bone = armature.data.bones.get(name)
        if not bone:
            continue
        if bone.parent:
            candidates.add(bone.parent.name)
        for child in bone.children:
            candidates.add(child.name)
    if not candidates and fallback_all:
        candidates = set(valid_bones)
    return candidates


def ensure_group(obj, name):
    group = obj.vertex_groups.get(name)
    if group is None:
        group = obj.vertex_groups.new(name=name)
    return group


def remove_non_bone_groups(obj, valid_bones):
    # After repair_object_weights every non-bone influence has been stripped,
    # so all non-bone groups are empty and can be removed outright.
    removed = []
    for group in list(obj.vertex_groups):
        if group.name not in valid_bones:
            removed.append(group.name)
            obj.vertex_groups.remove(group)
    return removed


def repair_object_weights(obj, armature, valid_bones, segments, fill_zero_weight=True, max_samples_per_object=20):
    # Single read pass over the vertices, then batched group.remove()/add()
    # calls. The previous implementation rebuilt the group-index map and
    # touched every vertex group per vertex, which made big trees look frozen.
    groups_by_index = {group.index: group for group in obj.vertex_groups}
    names_by_index = {index: group.name for index, group in groups_by_index.items()}
    valid_indices = {index for index, name in names_by_index.items() if name in valid_bones}
    epsilon = EPSILON

    remove_lists = defaultdict(list)
    fill_vertices = []
    invalid_counter = Counter()
    repaired = 0
    samples = []

    for vertex in obj.data.vertices:
        has_valid = False
        removed_names = None
        for group_ref in vertex.groups:
            if group_ref.weight <= epsilon:
                continue
            index = group_ref.group
            if index in valid_indices:
                has_valid = True
            else:
                name = names_by_index.get(index, str(index))
                remove_lists[index].append(vertex.index)
                invalid_counter[name] += 1
                if removed_names is None:
                    removed_names = []
                removed_names.append(name)
        had_invalid = removed_names is not None
        if had_invalid:
            repaired += 1
            if len(samples) < max_samples_per_object:
                samples.append({"vertex": vertex.index, "removed_groups": removed_names})
        if not has_valid and (had_invalid or fill_zero_weight):
            fill_vertices.append((vertex.index, had_invalid))

    for index, vertex_indices in remove_lists.items():
        group = groups_by_index.get(index)
        if group is not None:
            group.remove(vertex_indices)

    filled = 0
    if fill_vertices:
        candidates = [name for name in candidate_bones_for_object(obj, armature, valid_bones) if name in segments]
        if candidates:
            heads, tails = segments_to_arrays(segments, candidates)
            matrix = obj.matrix_world
            vertices = obj.data.vertices
            fills_by_bone = defaultdict(list)
            for vertex_index, had_invalid in fill_vertices:
                world_point = matrix @ vertices[vertex_index].co
                bone_name, distance = nearest_segment(world_point, candidates, heads, tails)
                fills_by_bone[bone_name].append(vertex_index)
                if not had_invalid:
                    filled += 1
                if len(samples) < max_samples_per_object:
                    samples.append({"vertex": vertex_index, "filled_with": bone_name, "distance": distance})
            for bone_name, vertex_indices in fills_by_bone.items():
                ensure_group(obj, bone_name).add(vertex_indices, 1.0, "REPLACE")

    return {
        "object": obj.name,
        "vertex_count": len(obj.data.vertices),
        "invalid_groups": dict(invalid_counter.most_common()),
        "repaired_invalid_vertices": repaired,
        "filled_zero_weight_vertices": filled,
        "samples": samples,
    }


def count_remaining_issues(objects, valid_bones):
    invalid_weight_vertices = 0
    zero_weight_vertices = 0
    invalid_group_counts = Counter()
    epsilon = EPSILON
    for obj in objects:
        names = group_name_by_index(obj)
        valid_indices = {index for index, name in names.items() if name in valid_bones}
        for vertex in obj.data.vertices:
            total = 0.0
            has_invalid = False
            for group_ref in vertex.groups:
                weight = group_ref.weight
                if weight <= epsilon:
                    continue
                total += weight
                if group_ref.group not in valid_indices:
                    has_invalid = True
                    invalid_group_counts[names.get(group_ref.group)] += 1
            if total <= epsilon:
                zero_weight_vertices += 1
            if has_invalid:
                invalid_weight_vertices += 1
    return {
        "remaining_invalid_weight_vertices": invalid_weight_vertices,
        "remaining_zero_weight_vertices": zero_weight_vertices,
        "remaining_invalid_group_counts": dict(invalid_group_counts.most_common()),
    }


def run_repair_invalid_weights(armature_name, mesh_regex="", fill_zero_weight=True, remove_empty_invalid_groups=True, max_samples_per_object=20, report_path=""):
    armature = get_armature(armature_name)
    name_filter = compile_optional_regex(mesh_regex)
    valid_bones = {bone.name for bone in armature.data.bones}
    segments = bone_world_segments(armature)
    objects = []
    object_reports = []
    removed_groups = {}

    for obj in bpy.context.scene.objects:
        if obj.type != "MESH":
            continue
        if name_filter and not name_filter.search(obj.name):
            continue
        if not armature_modifier(obj, armature):
            continue
        objects.append(obj)

    for obj in objects:
        report = repair_object_weights(obj, armature, valid_bones, segments, fill_zero_weight, max_samples_per_object)
        if report["repaired_invalid_vertices"] or report["filled_zero_weight_vertices"]:
            object_reports.append(report)
        if remove_empty_invalid_groups:
            removed = remove_non_bone_groups(obj, valid_bones)
            if removed:
                removed_groups[obj.name] = removed

    integrity = count_remaining_issues(objects, valid_bones)
    roots = [bone.name for bone in armature.data.bones if bone.parent is None]
    total_invalid_counts = Counter()
    for item in object_reports:
        total_invalid_counts.update(item["invalid_groups"])
    final_report = {
        "source_file": bpy.data.filepath,
        "armature": armature.name,
        "root_bones": roots,
        "checked_meshes": len(objects),
        "objects_repaired": len(object_reports),
        "total_repaired_invalid_vertices": sum(item["repaired_invalid_vertices"] for item in object_reports),
        "total_filled_zero_weight_vertices": sum(item["filled_zero_weight_vertices"] for item in object_reports),
        "invalid_group_vertex_counts": dict(total_invalid_counts.most_common()),
        "removed_empty_invalid_groups": removed_groups,
        "integrity_after_repair": integrity,
        "object_reports_sample": object_reports[:80],
    }
    write_report(report_path, final_report)
    return final_report


# ---------------------------------------------------------------------------
# Merge / export
# ---------------------------------------------------------------------------


def armature_modifier_uses(obj, armature):
    return obj.type == "MESH" and any(modifier.type == "ARMATURE" and modifier.object == armature for modifier in obj.modifiers)


def merge_skinned_meshes(armature, source_objects, merged_name):
    # Duplicate sources and join them. Join runs in C and preserves UV layers,
    # color attributes, custom split normals, materials, and vertex groups —
    # the previous Python from_pydata rebuild was slow and dropped all of them.
    valid_bones = {bone.name for bone in armature.data.bones}
    remove_object_and_orphan_mesh(bpy.data.objects.get(merged_name))
    ensure_object_mode()

    copies = []
    source_ranges = []
    for obj in source_objects:
        duplicate = obj.copy()
        duplicate.data = obj.data.copy()
        duplicate.modifiers.clear()
        bpy.context.scene.collection.objects.link(duplicate)
        copies.append(duplicate)
        source_ranges.append(
            {
                "object": obj.name,
                "vertex_count": len(obj.data.vertices),
                "face_count": len(obj.data.polygons),
            }
        )

    leftover_meshes = [duplicate.data for duplicate in copies[1:]]
    merged_obj = join_objects(copies)
    merged_obj.name = merged_name
    merged_obj.data.name = merged_name + "Mesh"
    for mesh in leftover_meshes:
        if mesh.users == 0:
            bpy.data.meshes.remove(mesh)

    removed_invalid_groups = remove_non_bone_groups(merged_obj, valid_bones)
    parent_keep_world(merged_obj, armature)
    modifier = merged_obj.modifiers.new(name="Armature", type="ARMATURE")
    modifier.object = armature

    uv_names = [layer.name for layer in merged_obj.data.uv_layers]
    return merged_obj, source_ranges, len(merged_obj.data.materials), uv_names, removed_invalid_groups


def _strict_material_intents_for_name(material_name, envelope):
    """Return the authoritative intent rows matching one final slot name."""
    api = handoff_contract.central_contract_api()
    intents = list(envelope.get("material_intents") or [])
    material_key = api.normalize_material_key(material_name)
    exact = [
        row for row in intents
        if api.normalize_material_key(row.get("material_key")) == material_key
    ]
    if exact:
        return exact
    base_key = api.normalize_material_key(
        api.production_group_base_name(material_name)
    )
    if not base_key:
        return []
    return [
        row for row in intents
        if api.normalize_material_key(row.get("production_group_base"))
        == base_key
    ]


def _is_unmanaged_empty_default_intent(intent):
    api = handoff_contract.central_contract_api()
    binding = intent.get("texture_binding")
    files = binding.get("files") if isinstance(binding, dict) else None
    return bool(
        api.normalize_material_key(intent.get("material_key")) == "default"
        and str(intent.get("texture_source_mode") or "")
        != "managed_texture_set"
        and not files
    )


def _is_ready_managed_bark_intent(intent):
    binding = intent.get("texture_binding")
    if not isinstance(binding, dict):
        return False
    if (
        str(intent.get("tree_part") or "") != "bark"
        or str(intent.get("texture_source_mode") or "")
        != "managed_texture_set"
        or str(binding.get("status") or "") != "ok"
    ):
        return False
    files = binding.get("files")
    if not isinstance(files, dict):
        return False
    for role in SPEEDTREE_TEXTURE_ROLES:
        path = Path(str(files.get(role) or ""))
        try:
            if not path.is_file() or path.stat().st_size <= 0:
                return False
        except OSError:
            return False
    return True


def _is_semantic_bark_intent(intent):
    return bool(
        isinstance(intent, dict)
        and str(intent.get("tree_part") or "") == "bark"
    )


def normalize_merged_speedtree_placeholder_material(
    merged_obj, texture_contract=None
):
    """Replace a proven Default/None slot with a deterministic bark slot.

    This is deliberately a post-merge operation.  The final mesh's real
    material slots are the mutation boundary, while the strict preflight
    envelope supplies the only accepted semantic evidence.  No material name
    other than the central exact ``default`` key is treated as a placeholder,
    and a ``None`` slot is accepted only at the one STMAT Default ordinal.
    """
    if not isinstance(texture_contract, dict) or not texture_contract.get(
        "strict_speedtree_pipeline_contract"
    ):
        return {
            "status": "not_applicable",
            "reason": "strict_speedtree_pipeline_contract_not_present",
        }
    envelope = texture_contract.get("speedtree_pipeline_contract")
    if not isinstance(envelope, dict):
        raise RuntimeError(
            "Strict SpeedTree placeholder normalization has no envelope"
        )
    if merged_obj is None or merged_obj.type != "MESH" or merged_obj.data is None:
        raise RuntimeError(
            "Strict SpeedTree placeholder normalization requires a merged mesh"
        )

    api = handoff_contract.central_contract_api()
    runtime_tolerant = _runtime_tolerant_texture_contract(
        texture_contract
    )
    intents = list(envelope.get("material_intents") or [])
    default_intents = [
        row for row in intents if _is_unmanaged_empty_default_intent(row)
    ]
    all_default_intents = [
        row for row in intents
        if api.normalize_material_key(row.get("material_key")) == "default"
    ]
    old_materials = list(merged_obj.data.materials)
    placeholder_slots = []

    default_stmat_index = None
    if len(default_intents) == 1:
        try:
            default_stmat_index = int(default_intents[0]["stmat_material_index"])
        except (KeyError, TypeError, ValueError):
            default_stmat_index = None

    for slot_index, material in enumerate(old_materials):
        if material is None:
            if (
                len(default_intents) == 1
                and default_stmat_index is not None
                and slot_index == default_stmat_index
            ):
                placeholder_slots.append(slot_index)
            # A None slot at any other ordinal is not evidence of the STMAT
            # Default placeholder. Leave it unchanged for the ordinary
            # face-assigned empty-slot validator below the merge/export
            # boundary; that validator can distinguish an unused join artifact
            # from an actual authored material omission.
            continue
        if api.normalize_material_key(material.name) != "default":
            continue
        matching = _strict_material_intents_for_name(material.name, envelope)
        if len(matching) == 1 and _is_unmanaged_empty_default_intent(
            matching[0]
        ):
            placeholder_slots.append(slot_index)
        else:
            message = (
                "SpeedTree merged Default material is not uniquely proven by "
                "the strict STMAT Default intent; slot: "
                + str(slot_index)
            )
            raise RuntimeError(message)
    if not placeholder_slots:
        return {
            "status": "not_applicable",
            "reason": "no_default_or_none_placeholder_slots",
        }
    if len(all_default_intents) != 1 or len(default_intents) != 1:
        message = (
            "SpeedTree merged Default placeholder requires exactly one "
            "unmanaged, source-empty STMAT Default intent"
        )
        raise RuntimeError(message)

    candidate_materials = []
    seen_candidates = set()
    for slot_index, material in enumerate(old_materials):
        if material is None or material.as_pointer() in seen_candidates:
            continue
        if str(material.get(UNREAL_TREE_PART_PROPERTY) or "") != "bark":
            continue
        matching = _strict_material_intents_for_name(material.name, envelope)
        if len(matching) != 1 or not (
            _is_semantic_bark_intent(matching[0])
            if runtime_tolerant
            else _is_ready_managed_bark_intent(matching[0])
        ):
            continue
        seen_candidates.add(material.as_pointer())
        candidate_materials.append((slot_index, material))
    if not candidate_materials:
        return {
            "status": "not_applicable",
            "reason": "no_semantic_bark_candidate_preserved_default",
            "placeholder_slots": placeholder_slots,
            "candidate_count": 0,
            "selection_policy": "preserve_default_without_semantic_bark",
        }
    target_slot_index, target_material = candidate_materials[0]
    candidate_count = len(candidate_materials)

    if merged_obj.data.users > 1:
        merged_obj.data = merged_obj.data.copy()
    mesh = merged_obj.data
    old_materials = list(mesh.materials)
    placeholder_set = set(placeholder_slots)
    new_materials = []
    slot_map = {}
    target_new_index = None
    for old_index, material in enumerate(old_materials):
        if old_index in placeholder_set:
            continue
        new_index = len(new_materials)
        new_materials.append(material)
        slot_map[old_index] = new_index
        if material is target_material and target_new_index is None:
            target_new_index = new_index
    if target_new_index is None:
        raise RuntimeError(
            "Managed bark candidate disappeared before placeholder remap"
        )
    for slot_index in placeholder_slots:
        slot_map[slot_index] = target_new_index
    changed_faces = sum(
        1 for polygon in mesh.polygons
        if polygon.material_index in placeholder_set
    )
    remap_mesh_materials(mesh, slot_map, new_materials)
    return {
        "status": "applied",
        "proof": (
            (
                "runtime_stmat_default_to_unique_semantic_bark"
                if candidate_count == 1
                else "runtime_stmat_default_to_first_semantic_bark"
            )
            if runtime_tolerant
            else (
                "strict_stmat_default_to_unique_managed_bark"
                if candidate_count == 1
                else "strict_stmat_default_to_first_managed_bark"
            )
        ),
        "placeholder_slots": placeholder_slots,
        "target_material": target_material.name,
        "target_material_slot": target_slot_index,
        "candidate_count": candidate_count,
        "candidate_material_slots": [
            slot_index for slot_index, _material in candidate_materials
        ],
        "candidate_materials": [
            material.name for _slot_index, material in candidate_materials
        ],
        "selection_policy": "first_semantic_bark_in_material_slot_order",
        "changed_face_count": changed_faces,
        "material_count_before": len(old_materials),
        "material_count_after": len(mesh.materials),
    }


def validate_face_assigned_material_slots(mesh_obj):
    """Reject structural material omissions at the final export boundary.

    An unused empty Blender slot is harmless metadata and may survive a join.
    A polygon assigned to an empty or out-of-range slot, however, would publish
    a mesh section with no material identity. That is structural corruption,
    not texture availability, and remains a hard failure.
    """
    if mesh_obj is None or mesh_obj.type != "MESH" or mesh_obj.data is None:
        raise RuntimeError(
            "Material-slot validation requires a final merged mesh"
        )
    materials = list(mesh_obj.data.materials)
    invalid_faces = []
    invalid_slots = set()
    for polygon in mesh_obj.data.polygons:
        slot_index = int(polygon.material_index)
        if (
            slot_index < 0
            or slot_index >= len(materials)
            or materials[slot_index] is None
        ):
            invalid_faces.append(int(polygon.index))
            invalid_slots.add(slot_index)
    if invalid_faces:
        raise RuntimeError(
            "Final merged mesh has polygon-assigned empty material slots: "
            f"object={mesh_obj.name}, slots={sorted(invalid_slots)}, "
            f"faces={invalid_faces[:40]}"
        )
    return {
        "status": "ok",
        "material_count": len(materials),
        "assigned_slot_indices": sorted(
            {int(polygon.material_index) for polygon in mesh_obj.data.polygons}
        ),
        "unused_empty_slot_indices": [
            index
            for index, material in enumerate(materials)
            if material is None
        ],
    }


def count_zero_weight_vertices(obj):
    zero = 0
    for vertex in obj.data.vertices:
        if not any(group.weight > EPSILON for group in vertex.groups):
            zero += 1
    return zero


MERGED_SUFFIX = "_Codex_MergedSkinned_WeightsFixed"


def strip_blender_duplicate_suffix(name):
    return re.sub(r"\.\d{3}$", "", name)


def is_codex_merged_output_name(name):
    return MERGED_SUFFIX.lower() in strip_blender_duplicate_suffix(name).lower()


def strip_merged_suffix(name):
    base = strip_blender_duplicate_suffix(name)
    if base.endswith(MERGED_SUFFIX):
        return base[: -len(MERGED_SUFFIX)]
    return base


def shared_source_mesh_prefix(names):
    sk_names = [name for name in names if name.startswith("SK_") and "_Codex" not in name]
    if len(sk_names) < 2:
        return ""
    prefix = os.path.commonprefix(sk_names).rstrip("_.- ")
    parts = prefix.split("_")
    if len(parts) > 3:
        prefix = "_".join(parts[:-1]) if parts[-1].lower() in {"bark", "trunk", "branch", "leaf", "leaves", "frond", "card", "cap"} else prefix
    return prefix if prefix.startswith("SK_") and len(prefix) > len("SK_") else ""


def choose_mesh_unit_name(merged_name, source_objects, unit_name):
    # The mesh-unit Empty must carry the source FBX name (e.g. SK_Tree_elm_01):
    # material-split exports name their meshes after materials, so deriving the
    # unit name from source meshes picks the wrong name there.
    if unit_name:
        return unit_name
    merged_base = strip_merged_suffix(merged_name)
    source_bases = [strip_blender_duplicate_suffix(obj.name) for obj in source_objects if obj.name]
    shared_prefix = shared_source_mesh_prefix(source_bases)
    if shared_prefix:
        return shared_prefix

    candidates = []
    for obj in source_objects:
        base = strip_blender_duplicate_suffix(obj.name)
        if not base:
            continue
        score = len(obj.data.vertices)
        if base.startswith("SK_"):
            score += 1_000_000
        if base == merged_base:
            score += 500_000
        if "_Codex" in base:
            score -= 750_000
        candidates.append((score, base))

    if candidates:
        candidates.sort(reverse=True)
        return candidates[0][1]
    return merged_base or unit_name or merged_name


def ensure_only_collection(obj, collection):
    if collection.objects.get(obj.name) is None:
        collection.objects.link(obj)
    for other in list(obj.users_collection):
        if other != collection:
            other.objects.unlink(obj)


def get_or_create_export_empty(name, collection, excluded=()):
    existing = bpy.data.objects.get(name)
    if existing is not None and existing not in excluded and existing.type == "EMPTY":
        empty = existing
        renamed_conflict = None
    else:
        renamed_conflict = None
        if existing is not None and existing not in excluded:
            existing.name = f"{name}_source"
            renamed_conflict = existing.name
        empty = bpy.data.objects.new(name, None)
        collection.objects.link(empty)
    empty.name = name
    empty.empty_display_type = "PLAIN_AXES"
    return empty, renamed_conflict


def structure_export_unit(
    armature,
    merged_obj,
    unit_name,
    mesh_unit_name,
    collection_name="Export",
    source_collection_name="SpeedTree_Source",
    source_fbx_path="",
):
    # Build the final Blender export unit:
    #   Export collection > Root armature > Empty (source mesh name,
    #   e.g. SK_Tree_elm_01) > merged mesh.
    # The source-mesh Empty is intentionally separate from the merged mesh so
    # the user does not need to manually rebuild this hierarchy after pipeline.
    coll = bpy.data.collections.get(collection_name)
    if coll is None:
        coll = bpy.data.collections.new(collection_name)
        bpy.context.scene.collection.children.link(coll)

    renamed_conflicts = []
    mesh_unit_name = mesh_unit_name or unit_name or f"{merged_obj.name}_Unit"
    if mesh_unit_name in {armature.name, merged_obj.name}:
        mesh_unit_name = f"{mesh_unit_name}_Mesh"
    mesh_unit_empty, renamed = get_or_create_export_empty(mesh_unit_name, coll, excluded=(armature, merged_obj))
    if renamed:
        renamed_conflicts.append({"requested": mesh_unit_name, "renamed_to": renamed})
    if source_fbx_path:
        mesh_unit_empty["codex_source_fbx"] = str(source_fbx_path)
    source_identity_path = str(
        merged_obj.get("codex_source_identity", "") or ""
    ).strip()
    if source_identity_path:
        mesh_unit_empty["codex_source_identity"] = source_identity_path

    if armature.parent is not None:
        parent_keep_world(armature, None)
    if mesh_unit_empty.parent != armature:
        parent_keep_world(mesh_unit_empty, armature)
    if merged_obj.parent != mesh_unit_empty:
        parent_keep_world(merged_obj, mesh_unit_empty)

    stale_unit_empty = bpy.data.objects.get(unit_name) if unit_name else None
    removed_stale_unit_empty = None
    if stale_unit_empty and stale_unit_empty.type == "EMPTY" and stale_unit_empty != mesh_unit_empty:
        if not stale_unit_empty.children:
            removed_stale_unit_empty = stale_unit_empty.name
            bpy.data.objects.remove(stale_unit_empty, do_unlink=True)

    for obj in (armature, mesh_unit_empty, merged_obj):
        ensure_only_collection(obj, coll)

    # Send2UE may intentionally contain several export units. Only stale items
    # owned by this source FBX are eligible for cleanup; unrelated/user-managed
    # Export contents must remain untouched.
    unit_members = {armature, mesh_unit_empty, merged_obj}
    swept_to_source = []
    deleted_stale_empties = []
    strays = [
        obj
        for obj in list(coll.objects)
        if obj not in unit_members
        and source_fbx_path
        and belongs_to_source_fbx(obj, source_fbx_path)
    ]
    for obj in strays:
        if obj.type == "EMPTY" and not obj.children:
            deleted_stale_empties.append(obj.name)
            bpy.data.objects.remove(obj, do_unlink=True)
            continue
        if obj.type == "MESH" and obj.data and len(obj.data.vertices) == 0 and not obj.children:
            # SpeedTree exports container nodes as zero-vertex meshes; they are
            # unusable junk and would become their own FBX unit in send2ue.
            deleted_stale_empties.append(obj.name)
            remove_object_and_orphan_mesh(obj)
            continue
        source_collection = ensure_scene_collection(source_collection_name)
        ensure_only_collection(obj, source_collection)
        swept_to_source.append(obj.name)

    return {
        "unit_empty": "",
        "mesh_unit_empty": mesh_unit_empty.name,
        "collection": coll.name,
        "hierarchy": [armature.name, mesh_unit_empty.name, merged_obj.name],
        "renamed_conflicts": renamed_conflicts,
        "removed_stale_unit_empty": removed_stale_unit_empty,
        "swept_to_source": swept_to_source,
        "source_collection": source_collection_name if swept_to_source else "",
        "deleted_stale_empties": deleted_stale_empties,
    }


def park_cluster_source_full_reference(
    armature,
    merged_obj,
    source_fbx_path,
    export_collection_name="Export",
    source_collection_name="SpeedTree_Source",
):
    """Keep the repaired Full SK reference out of Send2UE's Export collection.

    Cluster Normalizer owns the actual export pivots and always names them
    ``<source_stem>_01``, ``_02``, ... even when there is only one prototype.
    BWR still needs its merged source for inspection and downstream repair
    reports, but exposing that merge as the unsuffixed ``<source_stem>`` export
    unit creates an extra Unreal asset and violates the normalized naming
    contract.
    """
    source_collection = ensure_scene_collection(source_collection_name)
    parent_keep_world(merged_obj, armature)
    for obj in (armature, merged_obj):
        ensure_only_collection(obj, source_collection)

    removed_export_empties = []
    export_collection = bpy.data.collections.get(export_collection_name)
    source_stem = Path(source_fbx_path).stem if source_fbx_path else ""
    if export_collection:
        for obj in list(export_collection.objects):
            if is_cluster_normalizer_generated(obj):
                continue
            owned = (
                belongs_to_source_fbx_cleanup_lineage(obj, source_fbx_path)
                if source_fbx_path
                else False
            )
            canonical_empty = (
                bool(source_stem)
                and obj.type == "EMPTY"
                and obj.name == source_stem
            )
            if obj.type == "EMPTY" and (owned or canonical_empty):
                for child in list(obj.children):
                    if child != merged_obj:
                        parent_keep_world(child, armature)
                    ensure_only_collection(child, source_collection)
                if not obj.children:
                    removed_export_empties.append(obj.name)
                    bpy.data.objects.remove(obj, do_unlink=True)
                continue
            if owned:
                ensure_only_collection(obj, source_collection)

    return {
        "status": "parked_cluster_source_full_reference",
        "collection": source_collection.name,
        "hierarchy": [armature.name, merged_obj.name],
        "removed_export_empties": removed_export_empties,
        "reason": (
            "Cluster Normalizer-generated ordinal pivots are the only "
            "Send2UE export units for a Cluster source"
        ),
    }


def _mesh_face_material_counts(obj):
    materials = list(obj.data.materials)
    counts = Counter()
    for polygon in obj.data.polygons:
        slot = int(polygon.material_index)
        material = materials[slot] if 0 <= slot < len(materials) else None
        key = material.name if material is not None else f"<unassigned:{slot}>"
        counts[key] += 1
    return counts


def validate_source_geometry_coverage(source_objects, merged_obj):
    """Prove that every cleaned source face survives the final SK merge.

    SpeedTree material-grouped FBX files may contain authored scan trunks,
    stitches, caps, or bark surfaces whose names do not contain ``branch`` or
    ``leaf``.  Texture-contract validation cannot detect their omission when a
    material is absent from that contract, so geometry coverage is checked
    independently against the cleaned imported source objects. Material-name
    histograms are diagnostic because merge-time placeholder normalization can
    legitimately reassign faces without changing geometry coverage.
    """
    expected_objects = []
    seen = set()
    expected_materials = Counter()
    for obj in source_objects or []:
        if (
            obj is None
            or obj.name in seen
            or obj.type != "MESH"
            or obj.data is None
            or len(obj.data.polygons) == 0
        ):
            continue
        seen.add(obj.name)
        counts = _mesh_face_material_counts(obj)
        expected_materials.update(counts)
        expected_objects.append(
            {
                "object": obj.name,
                "faces": len(obj.data.polygons),
                "materials": dict(sorted(counts.items())),
            }
        )

    actual_materials = _mesh_face_material_counts(merged_obj)
    expected_faces = sum(row["faces"] for row in expected_objects)
    actual_faces = len(merged_obj.data.polygons)
    missing_material_faces = {
        name: count - actual_materials.get(name, 0)
        for name, count in expected_materials.items()
        if count > actual_materials.get(name, 0)
    }
    unexpected_material_faces = {
        name: count - expected_materials.get(name, 0)
        for name, count in actual_materials.items()
        if count > expected_materials.get(name, 0)
    }
    report = {
        "status": "ok",
        "expected_source_mesh_count": len(expected_objects),
        "expected_faces": expected_faces,
        "merged_faces": actual_faces,
        "face_delta": actual_faces - expected_faces,
        "expected_material_faces": dict(sorted(expected_materials.items())),
        "merged_material_faces": dict(sorted(actual_materials.items())),
        "missing_material_faces": dict(sorted(missing_material_faces.items())),
        "unexpected_material_faces": dict(
            sorted(unexpected_material_faces.items())
        ),
        "material_histogram_status": (
            "match"
            if expected_materials == actual_materials
            else "diagnostic_drift"
        ),
        "source_objects": expected_objects,
    }
    if actual_faces != expected_faces:
        report["status"] = "blocked"
        raise RuntimeError(
            "Final SpeedTree merge omitted or duplicated cleaned source geometry: "
            + json.dumps(report, sort_keys=True)
        )
    return report


def run_merge_export(
    armature_name,
    merged_name,
    fbx_path="",
    mesh_regex="",
    include_hidden=False,
    report_path="",
    settings=None,
    texture_contract=None,
    expected_source_objects=None,
):
    armature = get_armature(armature_name)
    name_filter = compile_optional_regex(mesh_regex)
    source_objects = []
    for obj in bpy.context.scene.objects:
        if obj.type != "MESH":
            continue
        if is_cluster_normalizer_generated(obj):
            continue
        if obj.name == merged_name:
            continue
        if is_codex_merged_output_name(obj.name):
            continue
        if not armature_modifier_uses(obj, armature):
            continue
        if name_filter and not name_filter.search(obj.name):
            continue
        if not include_hidden and object_hidden(obj):
            continue
        if len(obj.data.vertices) == 0:
            continue
        source_objects.append(obj)
    if not source_objects:
        raise RuntimeError("No skinned source meshes found.")

    source_object_names = [obj.name for obj in source_objects]

    merged_obj, ranges, _material_count, uv_names, removed_invalid_groups = merge_skinned_meshes(
        armature, source_objects, merged_name
    )
    placeholder_material_normalization = (
        normalize_merged_speedtree_placeholder_material(
            merged_obj, texture_contract=texture_contract
        )
    )
    material_slot_validation = validate_face_assigned_material_slots(
        merged_obj
    )
    api = handoff_contract.central_contract_api()
    material_slot_validation["placeholder_cleanup_authorized"] = bool(
        isinstance(texture_contract, dict)
        and texture_contract.get("strict_speedtree_pipeline_contract")
    )
    material_slot_validation["canonical_default_slot_indices"] = [
        index
        for index, material in enumerate(merged_obj.data.materials)
        if material is not None
        and api.normalize_material_key(
            api.production_group_base_name(material.name)
        ) == "default"
    ]
    source_geometry_coverage = (
        validate_source_geometry_coverage(expected_source_objects, merged_obj)
        if expected_source_objects is not None
        else {"status": "not_requested"}
    )
    source_fbx_candidates = {
        str(obj.get("codex_source_fbx", "") or "").strip()
        for obj in source_objects
        if str(obj.get("codex_source_fbx", "") or "").strip()
    }
    configured_source_fbx = str(
        (settings or {}).get("source_fbx_path", "") or ""
    ).strip()
    if configured_source_fbx:
        source_fbx_candidates.add(configured_source_fbx)
    if len(source_fbx_candidates) > 1:
        raise RuntimeError(
            "Merged sources disagree about their FBX provenance: "
            + ", ".join(sorted(source_fbx_candidates))
        )
    merged_source_fbx = (
        next(iter(source_fbx_candidates)) if source_fbx_candidates else ""
    )
    if merged_source_fbx:
        # Blender's object join does not reliably preserve custom properties
        # from the chosen active copy. The Cluster Normalizer validates this
        # exact source FBX against the matching Raw XML, so write it
        # explicitly on the final merged mesh.
        merged_obj["codex_source_fbx"] = merged_source_fbx
    source_identity_candidates = {
        str(obj.get("codex_source_identity", "") or "").strip()
        for obj in source_objects
        if str(obj.get("codex_source_identity", "") or "").strip()
    }
    configured_source_identity = str(
        (settings or {}).get("source_identity_path", "") or ""
    ).strip()
    if configured_source_identity:
        source_identity_candidates.add(configured_source_identity)
    if len(source_identity_candidates) > 1:
        raise RuntimeError(
            "Merged sources disagree about their stable source identity: "
            + ", ".join(sorted(source_identity_candidates))
        )
    merged_source_identity = (
        next(iter(source_identity_candidates))
        if source_identity_candidates
        else ""
    )
    if merged_source_identity:
        merged_obj["codex_source_identity"] = merged_source_identity
    zero_weight_vertices = count_zero_weight_vertices(merged_obj)
    roots = [bone.name for bone in armature.data.bones if bone.parent is None]

    report = {
        "source_file": bpy.data.filepath,
        "fbx": fbx_path,
        "armature": armature.name,
        "bone_count": len(armature.data.bones),
        "root_bones": roots,
        "source_mesh_count": len(source_objects),
        "source_objects": source_object_names,
        "merged_object": merged_obj.name,
        "merged_vertices": len(merged_obj.data.vertices),
        "merged_faces": len(merged_obj.data.polygons),
        "merged_vertex_groups": len(merged_obj.vertex_groups),
        "zero_weight_vertices": zero_weight_vertices,
        "removed_invalid_groups": removed_invalid_groups,
        "material_count": len(merged_obj.data.materials),
        "placeholder_material_normalization": (
            placeholder_material_normalization
        ),
        "material_slot_validation": material_slot_validation,
        "source_geometry_coverage": source_geometry_coverage,
        "uv_layers": uv_names,
        "color_attributes": [attr.name for attr in merged_obj.data.color_attributes],
        "merge_method": "join",
        "source_fbx_provenance": merged_source_fbx,
        "sample_sources": ranges[:80],
    }

    make_export_structure = settings.get("make_export_structure", settings.get("make_handoff_structure", True)) if settings else False
    if settings and make_export_structure:
        source_fbx = settings.get("source_fbx_path", "")
        if (
            settings.get("cluster_source_skin_contract", False)
            and settings.get(
                "defer_cluster_export_to_normalizer",
                False,
            )
        ):
            report["export_structure"] = park_cluster_source_full_reference(
                armature,
                merged_obj,
                source_fbx,
                settings.get("export_collection_name", "Export"),
                settings.get("source_collection_name", "SpeedTree_Source"),
            )
        else:
            unit_name = Path(source_fbx).stem if source_fbx else ""
            if not unit_name:
                unit_name = settings.get("name_stem", "") or (Path(bpy.data.filepath).stem if bpy.data.filepath else "")
            if not unit_name:
                unit_name = f"{armature.name}_ExportUnit"
            mesh_unit_name = choose_mesh_unit_name(merged_obj.name, source_objects, unit_name)
            report["export_structure"] = structure_export_unit(
                armature,
                merged_obj,
                unit_name,
                mesh_unit_name,
                settings.get("export_collection_name", "Export"),
                settings.get("source_collection_name", "SpeedTree_Source"),
                source_fbx,
            )

    if fbx_path:
        require_in_view_layer(armature, "for FBX export selection")
        previous_hide_viewport = armature.hide_viewport
        armature.hide_viewport = False
        try:
            previous_hidden = armature.hide_get()
            armature.hide_set(False)
        except RuntimeError:
            previous_hidden = False
        try:
            deselect_all()
            armature.select_set(True)
            merged_obj.select_set(True)
            bpy.context.view_layer.objects.active = armature
            Path(fbx_path).parent.mkdir(parents=True, exist_ok=True)
            bpy.ops.export_scene.fbx(
                filepath=fbx_path,
                use_selection=True,
                object_types={"ARMATURE", "MESH"},
                add_leaf_bones=False,
                bake_anim=False,
                use_mesh_modifiers=False,
                mesh_smooth_type="FACE",
                use_custom_props=False,
                primary_bone_axis="Y",
                secondary_bone_axis="X",
                armature_nodetype="NULL",
                path_mode="AUTO",
            )
        finally:
            try:
                armature.hide_set(previous_hidden)
            except RuntimeError:
                pass
            armature.hide_viewport = previous_hide_viewport

    if settings and settings.get("write_unreal_json", True):
        paths = default_paths(settings)
        json_result = write_unreal_json_from_scene(settings, paths, export_report=report)
        report["unreal_json"] = json_result["path"]
        report["dynamic_wind_json"] = json_result.get("dynamic_wind_path", "")
        report["unreal_json_warnings"] = json_result.get("warnings", [])
        report["grouping_health"] = json_result.get("grouping_health", {})
    write_report(report_path, report)
    return report


# Parked 3D Branch Cluster wrapper.
# This is intentionally not active because the current conversion direction does
# not use direct Frond_* card replacement.
#
# def run_replace_branch_frond_clusters(
#     tree_xml_path,
#     cluster_blend_path,
#     branch_material_id="8",
#     cluster_prefix="branch_elm_01",
#     collection_name="Codex_NameMatched_BranchCluster_Replacements",
#     hide_original_cards=False,
#     report_path="",
# ):
#     return branch_clusters.run_replace_branch_frond_clusters(
#         tree_xml_path,
#         cluster_blend_path,
#         branch_material_id=branch_material_id,
#         cluster_prefix=cluster_prefix,
#         collection_name=collection_name,
#         hide_original_cards=hide_original_cards,
#         report_path=report_path,
#     )


def default_paths(settings):
    blend_path = bpy.data.filepath
    source_fbx = settings.get("source_fbx_path", "")
    output_base_path = blend_path or source_fbx
    naming_base_path = source_fbx or blend_path
    if not output_base_path and not settings.get("out_dir"):
        raise RuntimeError("Save/open a .blend, choose a Source FBX, or set an Output Directory before running the SpeedTree repair pipeline.")
    out_dir = settings.get("out_dir") or os.path.dirname(output_base_path)
    name_stem = settings.get("name_stem") or (Path(naming_base_path).stem if naming_base_path else "speedtree_export")
    merged_name = settings.get("merged_name") or f"{name_stem}_Codex_MergedSkinned_WeightsFixed"
    blend_dir = os.path.dirname(blend_path) if blend_path else out_dir
    json_dir = os.path.join(blend_dir, "JSON")
    # Only the MegaPlant JSON (and the optional FBX) are deliverables; all
    # diagnostic reports go into a reports/ subfolder so the output directory
    # shows exactly one JSON to hand to Unreal.
    reports_dir = os.path.join(out_dir, "reports")
    return {
        "out_dir": out_dir,
        "json_dir": json_dir,
        "reports_dir": reports_dir,
        "name_stem": name_stem,
        "merged_name": merged_name,
        "fixed_blend": os.path.join(out_dir, f"{name_stem}_fixed_codex.blend"),
        "leaf_blend": os.path.join(out_dir, f"{name_stem}_fixed_codex_skinned_leaves.blend"),
        "weight_blend": os.path.join(out_dir, f"{name_stem}_fixed_codex_skinned_weights_fixed.blend"),
        "export_blend": os.path.join(out_dir, f"{name_stem}_codex_merged_skinned_weights_fixed_export.blend"),
        "fbx": os.path.join(out_dir, f"{name_stem}_codex_merged_skinned_weights_fixed.fbx"),
        "reparent_report": os.path.join(reports_dir, f"{name_stem}_reparent_report_codex.json"),
        "branch_plane_report": os.path.join(
            reports_dir,
            f"{name_stem}_branch_plane_skin_report_codex.json",
        ),
        "leaf_report": os.path.join(reports_dir, f"{name_stem}_leaf_skin_report_codex.json"),
        "residual_geometry_report": os.path.join(
            reports_dir,
            f"{name_stem}_residual_geometry_skin_report_codex.json",
        ),
        "weight_report": os.path.join(reports_dir, f"{name_stem}_weight_repair_report_codex.json"),
        "export_report": os.path.join(reports_dir, f"{name_stem}_codex_merged_skinned_weights_fixed_export_report.json"),
        "unreal_json": os.path.join(json_dir, f"{name_stem}_megaplant_tree_groups.json"),
        "dynamic_wind_json": os.path.join(json_dir, f"{name_stem}_dynamic_wind_import_from_megaplant_groups.json"),
        "pipeline_report": os.path.join(reports_dir, f"{name_stem}_speedtree_repair_pipeline_report_codex.json"),
    }


def save_blend(path):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    bpy.ops.wm.save_as_mainfile(filepath=path)


def run_full_pipeline(settings):
    paths = default_paths(settings)
    reports = {"paths": paths, "steps": [], "saved_blends": []}
    save_stage_blends = settings.get("save_intermediate_blends", False)

    # New-schema provenance/profile validation must finish before any Blender
    # mutation.  The operational BAT boundary still treats texture assignment
    # as runtime-tolerant; strict publication remains available to audit calls.
    texture_contract = load_speedtree_runtime_texture_contract(
        settings.get("texture_contract_path", ""),
        spm_path=settings.get("spm_path", ""),
        source_fbx_path=settings.get("source_fbx_path", ""),
    )
    if isinstance(texture_contract, dict) and texture_contract.get(
        "strict_speedtree_pipeline_contract"
    ):
        reports["speedtree_pipeline_contract"] = texture_contract.get(
            "speedtree_pipeline_contract"
        )
        reports["speedtree_live_source_identity"] = texture_contract.get(
            "live_source_identity"
        )

    source_fbx_path = settings.get("source_fbx_path", "")
    source_import_objects = [
        obj
        for obj in bpy.context.scene.objects
        if (
            belongs_to_source_fbx(obj, source_fbx_path)
            if source_fbx_path
            else "codex_source_fbx" in obj
        )
    ]
    texture_preflight = preflight_speedtree_material_texture_contracts(
        source_import_objects,
        texture_contract,
        source_fbx_override=source_fbx_path,
    )
    texture_contract = texture_preflight["texture_contract"]
    reports["speedtree_material_texture_preflight"] = texture_preflight
    applied_scales = apply_object_scales(source_import_objects)
    if applied_scales:
        reports["steps"].append({"name": "apply_import_scales", "status": "applied", "applied": applied_scales})

    source_import_meshes = [obj for obj in source_import_objects if obj.type == "MESH"]
    tag_existing_source_materials(source_import_meshes)
    renamed_materials = strip_speedtree_material_suffixes(source_import_meshes)
    if renamed_materials:
        reports["steps"].append(
            {"name": "normalize_material_names", "status": "applied", "renamed": renamed_materials}
        )

    material_consolidation = consolidate_speedtree_group_materials(
        source_import_meshes, texture_contract=texture_contract
    )
    reports["steps"].append(
        {
            "name": "consolidate_speedtree_group_materials",
            "status": material_consolidation.get("status", "skipped"),
            "changed_object_count": material_consolidation.get("changed_object_count", 0),
            "changed_face_count": material_consolidation.get("changed_face_count", 0),
            "groups": material_consolidation.get("groups", []),
        }
    )

    material_intents = apply_speedtree_material_intents(
        source_import_meshes, texture_contract=texture_contract
    )
    reports["speedtree_material_intents"] = material_intents
    reports["steps"].append(
        {
            "name": "apply_speedtree_material_intents",
            "status": material_intents.get("status", "legacy_fallback"),
            "changed_materials": material_intents.get("changed_materials", []),
        }
    )

    instance_profile = apply_spm_unreal_instance_profile(
        source_import_meshes, settings["spm_path"]
    )
    reports["unreal_instance_profile"] = instance_profile
    reports["steps"].append(
        {
            "name": "apply_spm_unreal_instance_profile",
            "status": instance_profile.get("status", "inspection_error"),
            "profile": instance_profile.get("profile", ""),
            "material_count": instance_profile.get("material_count", 0),
        }
    )

    texture_normalization = normalize_speedtree_material_textures(
        source_import_meshes, texture_contract=texture_contract
    )
    reports["texture_normalization"] = texture_normalization
    reports["steps"].append(
        {
            "name": "normalize_speedtree_material_textures",
            "status": texture_normalization.get("status", "missing"),
            "materials": texture_normalization.get("materials", []),
        }
    )
    group_variant_rebinding = rebind_blocked_speedtree_group_variants(
        bpy.context.scene.objects
    )
    reports["speedtree_group_variant_rebinding"] = (
        group_variant_rebinding
    )
    reports["steps"].append(
        {
            "name": "rebind_blocked_speedtree_group_variants",
            "status": group_variant_rebinding.get("status", "ok"),
            "rebound_count": group_variant_rebinding.get(
                "rebound_count", 0
            ),
            "unresolved": group_variant_rebinding.get("unresolved", []),
        }
    )

    removed_phantoms = remove_phantom_image_nodes(bpy.context.scene.objects)
    if removed_phantoms:
        reports["steps"].append(
            {"name": "remove_phantom_texture_nodes", "status": "applied", "removed": removed_phantoms}
        )

    def stage_save(path):
        if save_stage_blends:
            save_blend(path)
            reports["saved_blends"].append(path)

    if settings.get("cluster_source_skin_contract", False):
        cluster_armature = get_armature(
            settings.get("armature_name", "Root")
        )
        cluster_roots = [
            bone.name
            for bone in cluster_armature.data.bones
            if bone.parent is None
        ]
        if len(cluster_roots) != len(cluster_armature.data.bones):
            raise RuntimeError(
                "Cluster render-root axes must remain independent before "
                "normalization; found parented axis bones."
            )
        reparent = {
            "status": "skipped_cluster_independent_render_roots",
            "spm": settings["spm_path"],
            "armature": cluster_armature.name,
            "bone_count": len(cluster_armature.data.bones),
            "root_names_before": cluster_roots,
            "root_names_after": cluster_roots,
            "reason": (
                "Cluster axes represent independent render components, not a "
                "tree skeleton hierarchy"
            ),
        }
        write_report(paths["reparent_report"], reparent)
    else:
        reparent = run_reparent_from_spm(
            settings["spm_path"],
            settings.get("armature_name", "Root"),
            settings.get("true_root", "Bone_1_Start"),
            settings.get("scale_value", "auto"),
            settings.get("tolerance", 0.08),
            apply=True,
            strict=settings.get("strict_reparent", True),
            report_path=paths["reparent_report"],
        )
    reports["steps"].append({"name": "reparent", "status": reparent.get("status"), "report": paths["reparent_report"]})
    if reparent.get("status") == "blocked":
        write_report(paths["pipeline_report"], reports)
        raise RuntimeError(
            (
                reparent.get("error", "Reparent blocked.")
                + f" Export structure was not built. Reparent report: {paths['reparent_report']}"
            )
        )
    stage_save(paths["fixed_blend"])

    # Material-grouped SpeedTree exports leave branch/frond plane geometry as
    # an unskinned M_branch_* mesh.  Unlike loose leaves, it was never converted
    # to an armature-driven replacement and was therefore silently omitted by
    # run_merge_export(), which accepts skinned sources only.  Skin each
    # disconnected plane component rigidly to the nearest already-skinned bark
    # surface so the authored M_branch_* material/geometry pair survives the
    # final Full SK merge.
    branch_planes = run_skin_loose_instances(
        settings.get("armature_name", "Root"),
        "branch",
        "Branches_Skinned_Codex",
        hide_originals=settings.get("hide_originals", True),
        fallback_all_bones=settings.get("fallback_all_bones", False),
        apply=True,
        report_path=paths["branch_plane_report"],
        spm_path="",
        true_root=settings.get("true_root", "Bone_1_Start"),
        scale_value=settings.get("scale_value", "auto"),
        source_object_names=[obj.name for obj in source_import_meshes],
    )
    reports["steps"].append(
        {
            "name": "skin_branch_planes",
            "status": branch_planes.get("status"),
            "report": paths["branch_plane_report"],
            "created_object": branch_planes.get("created_object", ""),
            "created_faces": branch_planes.get("created_faces", 0),
        }
    )
    represented_source_objects = set(branch_planes.get("source_objects", []))

    if settings.get("skip_leaf_skin", False):
        reports["steps"].append({"name": "skin_loose_instances", "status": "skipped"})
    else:
        leaf = run_skin_loose_instances(
            settings.get("armature_name", "Root"),
            settings.get("leaf_name_contains", "leaf"),
            settings.get("leaf_out_name", "Leaves_Skinned_Codex"),
            hide_originals=settings.get("hide_originals", True),
            fallback_all_bones=settings.get("fallback_all_bones", False),
            apply=True,
            report_path=paths["leaf_report"],
            spm_path=settings.get("spm_path", ""),
            true_root=settings.get("true_root", "Bone_1_Start"),
            scale_value=settings.get("scale_value", "auto"),
            source_object_names=[obj.name for obj in source_import_meshes],
        )
        represented_source_objects.update(leaf.get("source_objects", []))
        reports["steps"].append({"name": "skin_loose_instances", "status": leaf.get("status"), "report": paths["leaf_report"]})
        stage_save(paths["leaf_blend"])

    # Preserve every remaining authored render mesh, regardless of naming.
    # Scan-derived trunks and their stitch surfaces commonly use M_tree_* or
    # M_bark_* names and therefore bypass the historical branch/leaf passes.
    # Restrict the catch-all to this exact FBX import and exclude source meshes
    # already represented by the semantic passes so each face is copied once.
    residual_geometry = run_skin_loose_instances(
        settings.get("armature_name", "Root"),
        "",
        "Residual_Geometry_Skinned_Codex",
        hide_originals=settings.get("hide_originals", True),
        fallback_all_bones=settings.get("fallback_all_bones", False),
        apply=True,
        report_path=paths["residual_geometry_report"],
        spm_path="",
        true_root=settings.get("true_root", "Bone_1_Start"),
        scale_value=settings.get("scale_value", "auto"),
        source_object_names=[obj.name for obj in source_import_meshes],
        exclude_object_names=represented_source_objects,
    )
    reports["steps"].append(
        {
            "name": "skin_residual_render_geometry",
            "status": residual_geometry.get("status"),
            "report": paths["residual_geometry_report"],
            "source_objects": residual_geometry.get("source_objects", []),
            "created_object": residual_geometry.get("created_object", ""),
            "created_faces": residual_geometry.get("created_faces", 0),
        }
    )

    weights = run_repair_invalid_weights(
        settings.get("armature_name", "Root"),
        mesh_regex=settings.get("mesh_regex", ""),
        fill_zero_weight=settings.get("fill_zero_weight", True),
        remove_empty_invalid_groups=settings.get("remove_empty_invalid_groups", True),
        max_samples_per_object=settings.get("max_samples_per_object", 20),
        report_path=paths["weight_report"],
    )
    reports["steps"].append(
        {
            "name": "repair_invalid_weights",
            "status": "applied",
            "report": paths["weight_report"],
            "remaining": weights.get("integrity_after_repair", {}),
        }
    )
    stage_save(paths["weight_blend"])

    export = run_merge_export(
        settings.get("armature_name", "Root"),
        paths["merged_name"],
        fbx_path=paths["fbx"] if settings.get("export_fbx", False) else "",
        mesh_regex=settings.get("mesh_regex", ""),
        include_hidden=settings.get("include_hidden", False),
        report_path=paths["export_report"],
        settings=settings,
        texture_contract=texture_contract,
        expected_source_objects=source_import_meshes,
    )
    reports["steps"].append(
        {
            "name": "merge_export",
            "status": "applied",
            "report": paths["export_report"],
            "zero_weight_vertices": export.get("zero_weight_vertices"),
            "removed_invalid_groups": export.get("removed_invalid_groups"),
            "placeholder_material_normalization": export.get(
                "placeholder_material_normalization", {}
            ),
            "source_geometry_coverage": export.get(
                "source_geometry_coverage", {}
            ),
            "export_structure": export.get("export_structure", {}),
        }
    )
    reports["export_structure"] = export.get("export_structure", {})
    reports["material_slot_validation"] = export.get(
        "material_slot_validation", {}
    )
    stage_save(paths["export_blend"])
    reports["status"] = "done"
    reports["spm_read_cache"] = spm_reader.cache_info()
    reports["grouping_health"] = export.get("grouping_health", {})
    reports["warnings"] = export.get("unreal_json_warnings", [])
    write_report(paths["pipeline_report"], reports)
    return reports


# ---------------------------------------------------------------------------
# One-shot SpeedTree -> import -> repair (idempotent re-run == update)
# ---------------------------------------------------------------------------


def remove_object_and_orphan_data(obj):
    # Like remove_object_and_orphan_mesh but also frees orphaned armature data.
    if not obj:
        return
    data = obj.data
    obj_type = obj.type
    bpy.data.objects.remove(obj, do_unlink=True)
    if data is None or getattr(data, "users", 1) != 0:
        return
    if obj_type == "MESH":
        bpy.data.meshes.remove(data)
    elif obj_type == "ARMATURE":
        bpy.data.armatures.remove(data)


def clear_previous_codex_build(settings):
    # Idempotent "update": drop everything a previous run of this add-on created
    # so re-running from SpeedTree does not stack duplicate imports/merges. Every
    # imported object carries a "codex_source_fbx" id property (copies inherit
    # it, so merged/leaf outputs are caught too).  Cleanup also accepts the
    # retired unprefixed sibling of a canonical SK_* FBX so an output-name
    # normalization does not leave the former Export unit behind.
    if bpy.context.scene.get(JSON_PREVIEW_SCENE_KEY):
        restore_json_group_preview()

    source_fbx_path = settings.get("source_fbx_path", "")
    source_identity_path = settings.get("source_identity_path", "")
    cleanup_source_fbx_paths = [source_fbx_path]
    cleanup_source_fbx_paths.extend(
        settings.get("source_fbx_cleanup_aliases", []) or []
    )
    cleanup_source_fbx_paths = [
        str(path)
        for path in cleanup_source_fbx_paths
        if str(path or "").strip()
    ]

    def owned_by_current_source(datablock):
        if belongs_to_source_identity(datablock, source_identity_path):
            return True
        return any(
            belongs_to_source_fbx_cleanup_lineage(datablock, path)
            for path in cleanup_source_fbx_paths
        )

    target_merged_name = default_paths(settings)["merged_name"]
    doomed = [
        obj
        for obj in list(bpy.data.objects)
        if (
            owned_by_current_source(obj)
            if cleanup_source_fbx_paths or source_identity_path
            else (
                "codex_source_fbx" in obj
                or is_codex_merged_output_name(obj.name)
            )
        )
        or obj.name == target_merged_name
    ]
    candidate_materials = collect_object_materials(doomed)
    removed_objects = [obj.name for obj in doomed]
    for obj in doomed:
        remove_object_and_orphan_data(obj)

    # Remove only childless unit empties owned by this source. The exact source
    # stem is included as a compatibility cleanup for units made before the
    # ownership tag was added.
    removed_empties = []
    export_collection = bpy.data.collections.get(settings.get("export_collection_name", "Export"))
    legacy_unit_names = {
        Path(path).stem for path in cleanup_source_fbx_paths
    }
    if export_collection:
        for obj in list(export_collection.objects):
            owned_empty = owned_by_current_source(obj)
            legacy_empty = obj.name in legacy_unit_names
            if obj.type == "EMPTY" and not obj.children and (owned_empty or legacy_empty):
                removed_empties.append(obj.name)
                bpy.data.objects.remove(obj, do_unlink=True)

    tagged_orphan_materials = [
        material
        for material in bpy.data.materials
        if material.users == 0
        and (
            owned_by_current_source(material)
            if cleanup_source_fbx_paths or source_identity_path
            else "codex_source_fbx" in material
        )
    ]
    removed_materials = []
    seen_material_names = set()
    for material in list(candidate_materials) + tagged_orphan_materials:
        if not material:
            continue
        try:
            material_name = material.name
            material_users = material.users
        except ReferenceError:
            continue
        if material_name in seen_material_names or material_users != 0:
            continue
        seen_material_names.add(material_name)
        removed_materials.append(material_name)
        bpy.data.materials.remove(material)

    return {
        "source_identity_path": str(source_identity_path or ""),
        "source_fbx_cleanup_paths": cleanup_source_fbx_paths,
        "removed_objects": removed_objects,
        "removed_empties": removed_empties,
        "removed_materials": removed_materials,
    }


def run_import_and_repair(settings):
    # settings must already carry source_fbx_path (and ideally xml_path) from the
    # SpeedTree export step. Wipes the previous build, re-imports, re-runs the
    # full repair pipeline. Pressing the button again is a clean update.
    if not settings.get("source_fbx_path"):
        raise RuntimeError("Source FBX path is required (run the SpeedTree export first).")
    texture_contract = load_speedtree_runtime_texture_contract(
        settings.get("texture_contract_path", ""),
        spm_path=settings.get("spm_path", ""),
        source_fbx_path=settings.get("source_fbx_path", ""),
    )
    cleanup = clear_previous_codex_build(settings)
    imported = run_import_source_fbx(
        settings["source_fbx_path"],
        settings.get("source_collection_name", "SpeedTree_Source"),
        rigid_fallback=True,
        cluster_source_skin_contract=settings.get(
            "cluster_source_skin_contract",
            False,
        ),
        armature_name=settings.get("armature_name", "Root"),
        true_root=settings.get("true_root", "Bone_1_Start"),
        spm_path=settings.get("spm_path", ""),
        texture_contract=texture_contract,
        cluster_source_xml_path=settings.get("xml_path", ""),
        source_identity_path=settings.get("source_identity_path", ""),
    )
    source_collection = bpy.data.collections.get(settings.get("source_collection_name", "SpeedTree_Source"))
    if source_collection:
        source_collection.hide_viewport = False
    reports = run_full_pipeline(settings)
    reports["cleanup"] = cleanup
    reports["import"] = {
        "source_fbx": imported.get("source_fbx", ""),
        "source_identity": imported.get("source_identity", ""),
        "imported_object_count": imported.get("imported_object_count", 0),
        "imported_mesh_count": imported.get("imported_mesh_count", 0),
        "imported_armature_count": imported.get("imported_armature_count", 0),
        "renamed_materials": imported.get("renamed_materials", []),
        "material_consolidation": imported.get("material_consolidation", {}),
        "speedtree_material_intents": imported.get(
            "speedtree_material_intents", {}
        ),
        "unreal_instance_profile": imported.get("unreal_instance_profile", {}),
        "rigid_fallback": imported.get("rigid_fallback"),
        "cluster_source_skin_contract": imported.get(
            "cluster_source_skin_contract"
        ),
        "unassigned_geometry_cleanup": imported.get(
            "unassigned_geometry_cleanup"
        ),
        "renderable_geometry": imported.get("renderable_geometry", {}),
    }
    import_cleanup = imported.get("unassigned_geometry_cleanup")
    if (
        isinstance(import_cleanup, dict)
        and import_cleanup.get("cleanup_authorized") is True
    ):
        reports["unassigned_geometry_cleanup"] = import_cleanup
        reports["renderable_geometry_after_cleanup"] = (
            imported.get("renderable_geometry", {})
        )
        source_fbx_path = settings.get("source_fbx_path", "")
        current_source_objects = [
            obj
            for obj in bpy.context.scene.objects
            if belongs_to_source_fbx(obj, source_fbx_path)
        ]
        reports["unassigned_geometry_cleanup_recheck"] = (
            discard_unassigned_geometry_before_repair(
                current_source_objects,
                texture_contract=texture_contract,
                spm_path=settings.get("spm_path", ""),
                source_fbx_path=source_fbx_path,
            )
        )
    pipeline_path = reports.get("paths", {}).get("pipeline_report", "")
    if pipeline_path:
        write_report(pipeline_path, reports)
    return reports
