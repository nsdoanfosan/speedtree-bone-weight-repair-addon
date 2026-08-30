"""Blender-only smoke checks for numeric material collision normalization."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import bpy


REPO_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_DIR / "addons"))

from speedtree_bone_weight_repair import core


def mesh_object(name, material):
    mesh = bpy.data.meshes.new(name + "Mesh")
    mesh.from_pydata(
        [(0.0, 0.0, 0.0), (1.0, 0.0, 0.0), (0.0, 1.0, 0.0)],
        [],
        [(0, 1, 2)],
    )
    mesh.materials.append(material)
    obj = bpy.data.objects.new(name, mesh)
    bpy.context.scene.collection.objects.link(obj)
    return obj


bpy.ops.wm.read_factory_settings(use_empty=True)

single = bpy.data.materials.new("M_single.001")
single_obj = mesh_object("Single", single)
single_result = core._consolidate_blender_numeric_material_duplicates(
    [single_obj]
)
assert single.name == "M_single", single.name
assert single_result["groups"][0]["mode"] == "blender_orphan_numeric_suffix"

canonical = bpy.data.materials.new("M_merge")
duplicate = bpy.data.materials.new("M_merge.001")
source_fbx = r"D:\test\SK_material_collision.fbx"
canonical["codex_source_fbx"] = source_fbx
duplicate["codex_source_fbx"] = source_fbx
canonical_obj = mesh_object("Canonical", canonical)
duplicate_obj = mesh_object("Duplicate", duplicate)
merge_result = core._consolidate_blender_numeric_material_duplicates(
    [canonical_obj, duplicate_obj]
)
assert duplicate_obj.data.materials[0] is canonical
assert bpy.data.materials.get("M_merge.001") is None
assert merge_result["groups"][0]["removed_source_materials"] == ["M_merge.001"]

role_material = bpy.data.materials.new("M_leaf_role_01")
role_duplicate = bpy.data.materials.new("M_leaf_role_01.001")
role_duplicate["codex_source_fbx"] = r"D:\test\SK_leaf_role_01.fbx"
role_card = mesh_object("RoleCard", role_material)
role_source = mesh_object("RoleSource", role_duplicate)
role_result = core._consolidate_blender_numeric_material_duplicates(
    [role_card, role_source],
    authoritative_material_names=["M_leaf_role_01"],
)
assert role_source.data.materials[0] is role_material
assert bpy.data.materials.get("M_leaf_role_01.001") is None
assert role_result["groups"][0]["proofs"] == [
    "authoritative_cluster_material_identity"
]

production_material = bpy.data.materials.new("M_leaf_production_01")
stale_production_material = bpy.data.materials.new(
    "M_leaf_production_01.001"
)
production_source_material = bpy.data.materials.new(
    "M_leaf_production_01_green"
)
production_source_material["codex_source_fbx"] = (
    r"D:\test\SK_leaf_production_01.fbx"
)
production_source = mesh_object(
    "ProductionRoleSource",
    production_source_material,
)
api = core.handoff_contract.central_contract_api()
production_intent = api.build_material_intent(
    production_source_material.name,
    explicit_tree_part="leaf",
    explicit_tree_shading="foliage",
)
production_contract = {
    "strict_speedtree_pipeline_contract": True,
    "speedtree_pipeline_contract": {
        "material_intents": [production_intent]
    },
}
stale_production_material[
    "codex_speedtree_consolidation_target_proof"
] = json.dumps(
    {
        "production_group_base": api.normalize_material_key(
            api.production_group_base_name(production_source_material.name)
        ),
        "tree_part": "leaf",
        "tree_shading": "foliage",
        "instance_profile": "",
    },
    sort_keys=True,
    separators=(",", ":"),
    ensure_ascii=True,
)
production_result = core.consolidate_speedtree_group_materials(
    [production_source],
    texture_contract=production_contract,
    authoritative_material_names=[production_material.name],
)
assert production_source.data.materials[0] is production_material
production_groups = [
    row
    for row in production_result["groups"]
    if row.get("mode") == "production_group_suffix"
]
assert production_groups[0]["target_material"] == "M_leaf_production_01"
assert production_groups[0]["readiness_mode"] == (
    "authoritative_cluster_material_identity"
)

missing_source_material = bpy.data.materials.new(
    "M_leaf_missing_exact_01_green"
)
missing_source_material["codex_source_fbx"] = (
    r"D:\test\SK_leaf_missing_exact_01.fbx"
)
missing_source = mesh_object("MissingExactSource", missing_source_material)
missing_intent = api.build_material_intent(
    missing_source_material.name,
    explicit_tree_part="leaf",
    explicit_tree_shading="foliage",
)
missing_contract = {
    "strict_speedtree_pipeline_contract": True,
    "speedtree_pipeline_contract": {
        "material_intents": [missing_intent]
    },
}
missing_stale = bpy.data.materials.new("M_leaf_missing_exact_01.001")
missing_stale["codex_speedtree_consolidation_target_proof"] = json.dumps(
    {
        "production_group_base": api.normalize_material_key(
            api.production_group_base_name(missing_source_material.name)
        ),
        "tree_part": "leaf",
        "tree_shading": "foliage",
        "instance_profile": "",
    },
    sort_keys=True,
    separators=(",", ":"),
    ensure_ascii=True,
)
missing_result = core.consolidate_speedtree_group_materials(
    [missing_source],
    texture_contract=missing_contract,
    authoritative_material_names=["M_leaf_missing_exact_01"],
)
assert missing_source.data.materials[0].name == "M_leaf_missing_exact_01"
assert missing_source.data.materials[0] is not missing_stale
missing_groups = [
    row
    for row in missing_result["groups"]
    if row.get("mode") == "production_group_suffix"
]
assert missing_groups[0]["readiness_mode"] == (
    "authoritative_cluster_material_created"
)

print("NUMERIC_MATERIAL_SMOKE_OK")
