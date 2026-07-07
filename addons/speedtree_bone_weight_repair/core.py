import colorsys
import gzip
import json
import math
import os
import re
import subprocess
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

import bpy
import numpy as np
from mathutils import Vector, kdtree

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
JSON_PREVIEW_OBJECT_KEYS = (
    "codex_json_group",
    "codex_json_group_matched_by",
    "codex_json_preview_source",
    # Legacy key from the old material-swap preview; still cleared off old scenes.
    "codex_json_preview_original_materials",
)


def write_report(path, data):
    if not path:
        return
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def utc_timestamp():
    return datetime.now(timezone.utc).isoformat()


BUNDLED_PRESET_DIR = Path(__file__).parent / "presets" / "speedtree_10_1"
BUNDLED_FBX_EXPORT_OPTIONS = BUNDLED_PRESET_DIR / "Options_MA_Fbx.ini"
BUNDLED_XML_EXPORT_OPTIONS = BUNDLED_PRESET_DIR / "Options_HI_Xml.ini"
LEGACY_BUNDLED_EXPORT_OPTIONS = Path(__file__).parent / "presets" / "Options_Fbx.ini"


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
):
    spm = Path(spm_path)
    if not spm.exists():
        raise RuntimeError(f"SPM does not exist: {spm_path}")

    exe = Path(speedtree_exe_path or r"C:\Program Files\SpeedTree\SpeedTree Modeler v10.1.0\win64\SpeedTree_Modeler.exe")
    if not exe.exists():
        raise RuntimeError(f"SpeedTree Modeler executable does not exist: {exe}")

    root = Path(output_root) if output_root else spm.parent
    stem = name_stem or spm.stem
    targets = []
    if export_fbx:
        options = Path(fbx_export_options_path or export_options_path or default_speedtree_export_options(spm_path, "fbx"))
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
        command = [
            str(exe),
            str(spm),
            "-export_options",
            str(options),
            "-export",
            str(target),
        ]
        started = utc_timestamp()
        try:
            completed = subprocess.run(
                command,
                cwd=str(spm.parent),
                timeout=timeout_seconds,
                capture_output=True,
                text=True,
            )
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(f"SpeedTree {kind.upper()} export timed out after {timeout_seconds} seconds.") from exc

        results[kind] = {
            "path": str(target),
            "export_options": str(options),
            "exists": target.exists(),
            "size": target.stat().st_size if target.exists() else 0,
            "returncode": completed.returncode,
            "started": started,
            "finished": utc_timestamp(),
            "stdout": completed.stdout[-4000:],
            "stderr": completed.stderr[-4000:],
        }
        if completed.returncode != 0:
            raise RuntimeError(f"SpeedTree {kind.upper()} export failed with code {completed.returncode}: {completed.stderr[-1000:]}")
        if not target.exists() or target.stat().st_size == 0:
            raise RuntimeError(f"SpeedTree {kind.upper()} export finished but did not create a valid file: {target}")

    return {
        "speedtree_exe": str(exe),
        "spm": str(spm),
        "export_options": export_options,
        "output_root": str(root),
        "name_stem": stem,
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


def tag_speedtree_import_materials(objects, source_fbx_path):
    for material in collect_object_materials(objects):
        material["codex_source_fbx"] = str(source_fbx_path)


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


MATERIAL_GROUP_TOKENS = {"green", "twig", "twigs", "stem", "stems", "dead"}


def material_name_tokens(name):
    normalized = re.sub(r"[^a-zA-Z0-9]+", "_", name or "").lower()
    normalized = re.sub(r"_+", "_", normalized).strip("_")
    return [token for token in normalized.split("_") if token]


def material_group_token(material):
    tokens = material_name_tokens(material.name if material else "")
    for token in tokens:
        if token in MATERIAL_GROUP_TOKENS:
            return "twig" if token == "twigs" else "stem" if token == "stems" else token
    return ""


def material_base_name(material):
    if material is None:
        return ""
    name = re.sub(r"(\.\d{3})$", "", material.name)
    parts = [part for part in re.split(r"[^a-zA-Z0-9]+", name) if part]
    kept = [part for part in parts if part.lower() not in MATERIAL_GROUP_TOKENS]
    return "_".join(kept).strip("_")


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


def consolidate_speedtree_group_materials(objects):
    # SpeedTree FBX grouped by material can come back as Green/Twig/Stem slots
    # even when they are all the same atlas texture. Collapse those slots before
    # merge/weight export; do not touch object transforms, UVs, vertex groups, or weights.
    mesh_objects = [obj for obj in objects if obj.type == "MESH" and obj.data]
    if not mesh_objects:
        return {"status": "skipped", "reason": "no mesh objects", "groups": []}

    grouped = defaultdict(list)
    for obj in mesh_objects:
        for slot_index, material in enumerate(obj.data.materials):
            group_token = material_group_token(material)
            if not group_token:
                continue
            key = (material_base_name(material), material_texture_signature(material))
            grouped[key].append((obj, slot_index, material, group_token))

    reports = []
    for (base_name, texture_signature), entries in grouped.items():
        group_tokens = sorted({entry[3] for entry in entries})
        if len(group_tokens) < 2:
            continue
        source_materials = []
        seen_materials = set()
        for _obj, _slot_index, material, _token in entries:
            if material and material.name not in seen_materials:
                seen_materials.add(material.name)
                source_materials.append(material)
        if not source_materials:
            continue

        target_name = unified_material_name(base_name, source_materials)
        target_material = bpy.data.materials.get(target_name)
        if target_material is None:
            target_material = source_materials[0].copy()
            target_material.name = target_name
            target_material["codex_speedtree_consolidated_from"] = [material.name for material in source_materials]

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
            slot_map = {}
            new_materials = [target_material]
            non_candidate_indices = {}
            for old_index, material in enumerate(mesh.materials):
                if old_index in candidate_slots:
                    slot_map[old_index] = 0
                    continue
                if material is None:
                    slot_map[old_index] = 0
                    continue
                if material.name not in non_candidate_indices:
                    non_candidate_indices[material.name] = len(new_materials)
                    new_materials.append(material)
                slot_map[old_index] = non_candidate_indices[material.name]
            for poly in mesh.polygons:
                if poly.material_index in candidate_slots:
                    changed_faces += 1
            remap_mesh_materials(mesh, slot_map, new_materials)
            obj["codex_speedtree_unified_material"] = target_material.name
            changed_objects.append(obj.name)

        reports.append(
            {
                "target_material": target_material.name,
                "source_materials": [material.name for material in source_materials],
                "group_tokens": group_tokens,
                "texture_signature": list(texture_signature),
                "object_count": len(changed_objects),
                "objects": changed_objects[:200],
                "changed_faces": changed_faces,
            }
        )

    return {
        "status": "applied" if reports else "skipped",
        "groups": reports,
        "changed_object_count": sum(group["object_count"] for group in reports),
        "changed_face_count": sum(group["changed_faces"] for group in reports),
    }


def run_import_source_fbx(source_fbx_path, source_collection_name="SpeedTree_Source"):
    path = Path(source_fbx_path)
    if not source_fbx_path or not path.exists():
        raise RuntimeError(f"Source FBX does not exist: {source_fbx_path}")

    before = {obj.name for obj in bpy.data.objects}
    bpy.ops.import_scene.fbx(filepath=str(path))
    imported = [obj for obj in bpy.data.objects if obj.name not in before]
    applied_scales = apply_object_scales(imported)
    # Keep raw imports out of the Export collection (the FBX importer links to
    # the active collection) — send2ue exports every unit found in Export, so
    # stray source objects there would split the asset into multiple FBX files.
    source_collection = ensure_scene_collection(source_collection_name)
    for obj in imported:
        obj["codex_source_fbx"] = str(path)
        ensure_only_collection(obj, source_collection)
    tag_speedtree_import_materials(imported, path)
    removed_phantoms = remove_phantom_image_nodes(imported)
    renamed_materials = strip_speedtree_material_suffixes(imported)

    return {
        "source_fbx": str(path),
        "source_collection": source_collection.name,
        "imported_object_count": len(imported),
        "imported_armature_count": sum(1 for obj in imported if obj.type == "ARMATURE"),
        "imported_mesh_count": sum(1 for obj in imported if obj.type == "MESH"),
        "imported_objects": [obj.name for obj in imported[:200]],
        "applied_scales": applied_scales,
        "removed_phantom_texture_nodes": removed_phantoms,
        "renamed_materials": renamed_materials,
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
# Unreal only has to call the (stable C++) import function — no conversion code
# stranded editor-side. Per-group influence values are sensible defaults an
# artist can override in Unreal afterwards.


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


def build_dynamic_wind_groups(simulation_groups, flexibility=1.0):
    flex_by_group = derive_group_flex(simulation_groups, flexibility)
    max_index = max((group["index"] for group in simulation_groups), default=0)
    by_index = {group["index"]: group for group in simulation_groups}
    groups = []
    for group_index in range(max_index + 1):
        source = by_index.get(group_index, {})
        if source.get("is_trunk_group", group_index == 0):
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


def build_dynamic_wind_data(bone_records, simulation_groups, gust_attenuation=0.25, ground_cover=False, flexibility=1.0):
    joints = []
    for bone in bone_records or []:
        name = bone.get("name")
        group = bone.get("group")
        if not name or group is None:
            continue
        joints.append({"JointName": name, "SimulationGroupIndex": int(group)})
    if not joints:
        raise RuntimeError("Cannot build dynamic wind JSON: no joint→group entries (needs the SpeedTree XML).")
    return {
        "Joints": joints,
        "SimulationGroups": build_dynamic_wind_groups(simulation_groups or [], flexibility),
        "bIsGroundCover": bool(ground_cover),
        "GustAttenuation": float(gust_attenuation),
    }


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
    xml_path = settings.get("xml_path", "")
    if xml_path:
        xml_bones, xml_info = build_xml_bone_metadata(
            xml_path,
            armature,
            settings.get("xml_trunk_generator_regex", "trunk"),
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
    warnings = []
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
            "note": "This JSON is the rich record. The Unreal-ready import file is the sibling *_dynamic_wind_import_from_megaplant_groups.json. Per-group influence defaults may be overridden in Unreal.",
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
        )
        write_report(dynamic_wind_path, dynamic_wind)
        result["dynamic_wind_path"] = dynamic_wind_path
        result["dynamic_wind"] = {
            "joint_count": len(dynamic_wind["Joints"]),
            "simulation_group_count": len(dynamic_wind["SimulationGroups"]),
        }
    return result


# ---------------------------------------------------------------------------
# SPM parsing / bone reparent
# ---------------------------------------------------------------------------


def read_spm_xml(path):
    with open(path, "rb") as handle:
        magic = handle.read(2)
    if magic == b"\x1f\x8b":
        with gzip.open(path, "rb") as handle:
            return handle.read()
    with open(path, "rb") as handle:
        return handle.read()


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
    root = ET.fromstring(read_spm_xml(path))
    generators = {}
    nodes = {}
    node_order = []

    for gen in root.findall(".//Generator"):
        guid = child_text(gen, "GUID")
        if not guid:
            continue
        generators[guid] = {
            "guid": guid,
            "type": gen.attrib.get("Type"),
            "name": child_text(gen, "Name", ""),
            "level": child_text(gen, "Level"),
            "hidden": child_bool(gen, "Hidden", False),
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


def find_base_ref_pairs(tree, raw_tolerance=0.05):
    nodes = tree["nodes"]
    base_refs = [node for node in nodes.values() if node["type"] == "BaseRef" and node["coord"]]
    refs_by_key = defaultdict(list)
    for ref in base_refs:
        refs_by_key[coord_key(ref["coord"])].append(ref)

    records = []
    issues = []
    for guid in tree["node_order"]:
        node = nodes[guid]
        if node["type"] != "Branch":
            continue
        base = nodes.get(node["parent"])
        if not base or base["type"] != "Base" or not base["coord"] or not node["coord"]:
            continue

        candidates = refs_by_key.get(coord_key(base["coord"]), [])
        if not candidates:
            candidates = [ref for ref in base_refs if dist(ref["coord"], base["coord"]) <= raw_tolerance]
        candidates = [ref for ref in candidates if nodes.get(ref["parent"], {}).get("type") == "Branch"]
        if not candidates:
            issues.append({"child_branch": guid, "base": base["guid"], "error": "no matching BaseRef"})
            continue

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
    sample_records = records[: min(len(records), 500)]
    scores = []
    for scale in candidates:
        nearest = []
        for rec in sample_records:
            point = vec_div(rec["child_coord_raw"], scale)
            nearest.append(min(dist(point, bones[root]["head"]) for root in orphan_roots))
        nearest.sort()
        scores.append({"scale": scale, "median_nearest_child_root": nearest[len(nearest) // 2]})
    scores.sort(key=lambda item: item["median_nearest_child_root"])
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
    used_children = set()
    problems = []

    for rec in records:
        child_point = vec_div(rec["child_coord_raw"], scale)
        child_bone, child_distance = nearest_unused_start(child_point, orphan_roots, bones, used_children)
        if not child_bone:
            problems.append({**rec, "error": "no unused orphan root for child branch"})
            continue
        if child_distance is None or child_distance > tolerance:
            problems.append({**rec, "error": "child root distance over tolerance", "distance": child_distance})
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
        used_children.add(child_bone)
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

    mapping, details, problems, roots_before, orphan_roots = build_reparent_map(
        records, bones, children, true_root, scale, tolerance
    )

    report = {
        "blend": bpy.data.filepath,
        "spm": spm_path,
        "spm_version": tree["version"],
        "armature": armature.name,
        "bones": len(bones),
        "roots_before": len(roots_before),
        "root_names_before": roots_before[:100],
        "true_root": true_root,
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
        "applied": False,
    }

    # Strict mode should block only when the usable orphan-root mapping is
    # incomplete or the SPM parse itself has issues. Some SpeedTree 10.1 files
    # contain extra BaseRef records that do not correspond to remaining orphan
    # roots; those are still reported as problems, but should not stop the
    # export structure when every orphan root has a parent mapping.
    blocked = strict and (len(mapping) != len(orphan_roots) or spm_issues)
    if blocked:
        report["status"] = "blocked"
        report["error"] = "Reparent mapping was not complete; no changes applied."
    elif apply:
        applied = apply_reparent_mapping(armature, mapping)
        roots_after = [bone.name for bone in armature.data.bones if bone.parent is None]
        report.update(
            {
                "status": "applied",
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
                            "radius": float(attrs.get("Radius", "0") or 0.0),
                            "start": (float(attrs["StartX"]), float(attrs["StartY"]), float(attrs["StartZ"])),
                            "end": (float(attrs["EndX"]), float(attrs["EndY"]), float(attrs["EndZ"])),
                            "mass": float(attrs.get("Mass", "0") or 0.0),
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


def collect_loose_instances(armature, name_contains):
    instances = []
    needle = name_contains.lower()
    for obj in bpy.context.scene.objects:
        if obj.type != "MESH":
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
):
    armature = get_armature(armature_name)
    instances = collect_loose_instances(armature, name_contains)
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


def structure_export_unit(armature, merged_obj, unit_name, mesh_unit_name, collection_name="Export", source_collection_name="SpeedTree_Source"):
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

    # send2ue exports one FBX per unit it finds in the Export collection, so
    # sweep everything that is not part of this unit out of it: stale childless
    # empties from earlier runs are deleted, all other strays move to the
    # source collection.
    unit_members = {armature, mesh_unit_empty, merged_obj}
    swept_to_source = []
    deleted_stale_empties = []
    strays = [obj for obj in list(coll.objects) if obj not in unit_members]
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


def run_merge_export(armature_name, merged_name, fbx_path="", mesh_regex="", include_hidden=False, report_path="", settings=None):
    armature = get_armature(armature_name)
    name_filter = compile_optional_regex(mesh_regex)
    source_objects = []
    for obj in bpy.context.scene.objects:
        if obj.type != "MESH":
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

    merged_obj, ranges, material_count, uv_names, removed_invalid_groups = merge_skinned_meshes(
        armature, source_objects, merged_name
    )
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
        "material_count": material_count,
        "uv_layers": uv_names,
        "color_attributes": [attr.name for attr in merged_obj.data.color_attributes],
        "merge_method": "join",
        "sample_sources": ranges[:80],
    }

    make_export_structure = settings.get("make_export_structure", settings.get("make_handoff_structure", True)) if settings else False
    if settings and make_export_structure:
        source_fbx = settings.get("source_fbx_path", "")
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
        "leaf_report": os.path.join(reports_dir, f"{name_stem}_leaf_skin_report_codex.json"),
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

    source_import_objects = [
        obj
        for obj in bpy.context.scene.objects
        if "codex_source_fbx" in obj
    ]
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

    removed_phantoms = remove_phantom_image_nodes(bpy.context.scene.objects)
    if removed_phantoms:
        reports["steps"].append(
            {"name": "remove_phantom_texture_nodes", "status": "applied", "removed": removed_phantoms}
        )

    material_consolidation = consolidate_speedtree_group_materials(source_import_meshes)
    if material_consolidation.get("status") == "applied":
        reports["steps"].append(
            {
                "name": "consolidate_speedtree_group_materials",
                "status": "applied",
                "changed_object_count": material_consolidation.get("changed_object_count", 0),
                "changed_face_count": material_consolidation.get("changed_face_count", 0),
                "groups": material_consolidation.get("groups", []),
            }
        )

    def stage_save(path):
        if save_stage_blends:
            save_blend(path)
            reports["saved_blends"].append(path)

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
        )
        reports["steps"].append({"name": "skin_loose_instances", "status": leaf.get("status"), "report": paths["leaf_report"]})
        stage_save(paths["leaf_blend"])

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
    )
    reports["steps"].append(
        {
            "name": "merge_export",
            "status": "applied",
            "report": paths["export_report"],
            "zero_weight_vertices": export.get("zero_weight_vertices"),
            "removed_invalid_groups": export.get("removed_invalid_groups"),
            "export_structure": export.get("export_structure", {}),
        }
    )
    reports["export_structure"] = export.get("export_structure", {})
    stage_save(paths["export_blend"])
    reports["status"] = "done"
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
    # it, so merged/leaf outputs are caught too); merged outputs are also matched
    # by name as a belt-and-suspenders fallback.
    if bpy.context.scene.get(JSON_PREVIEW_SCENE_KEY):
        restore_json_group_preview()

    doomed = [
        obj
        for obj in list(bpy.data.objects)
        if "codex_source_fbx" in obj or is_codex_merged_output_name(obj.name)
    ]
    candidate_materials = collect_object_materials(doomed)
    removed_objects = [obj.name for obj in doomed]
    for obj in doomed:
        remove_object_and_orphan_data(obj)

    # Sweep now-childless empties left behind in the Export collection (the
    # source-mesh unit Empty is not id-tagged, so remove it once it is empty).
    removed_empties = []
    export_collection = bpy.data.collections.get(settings.get("export_collection_name", "Export"))
    if export_collection:
        for obj in list(export_collection.objects):
            if obj.type == "EMPTY" and not obj.children:
                removed_empties.append(obj.name)
                bpy.data.objects.remove(obj, do_unlink=True)

    tagged_orphan_materials = [
        material
        for material in bpy.data.materials
        if "codex_source_fbx" in material and material.users == 0
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

    # Purge any data blocks the object removals orphaned.
    for datablocks in (bpy.data.meshes, bpy.data.armatures):
        for block in list(datablocks):
            if block.users == 0:
                datablocks.remove(block)

    return {
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
    cleanup = clear_previous_codex_build(settings)
    imported = run_import_source_fbx(
        settings["source_fbx_path"],
        settings.get("source_collection_name", "SpeedTree_Source"),
    )
    source_collection = bpy.data.collections.get(settings.get("source_collection_name", "SpeedTree_Source"))
    if source_collection:
        source_collection.hide_viewport = False
    reports = run_full_pipeline(settings)
    reports["cleanup"] = cleanup
    reports["import"] = {
        "imported_object_count": imported.get("imported_object_count", 0),
        "imported_mesh_count": imported.get("imported_mesh_count", 0),
        "imported_armature_count": imported.get("imported_armature_count", 0),
        "renamed_materials": imported.get("renamed_materials", []),
    }
    return reports
