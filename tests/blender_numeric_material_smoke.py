"""Blender-only smoke checks for numeric material collision normalization."""

from __future__ import annotations

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

print("NUMERIC_MATERIAL_SMOKE_OK")
