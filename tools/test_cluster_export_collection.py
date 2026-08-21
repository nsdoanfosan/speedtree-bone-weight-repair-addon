"""Blender smoke test for Cluster-only Export pivots after assembly."""

from __future__ import annotations

import json

import addon_utils
import bpy


bpy.ops.wm.read_factory_settings(use_empty=True)
addon_utils.enable(
    "speedtree_bone_weight_repair",
    default_set=False,
    persistent=False,
)

from speedtree_bone_weight_repair.core import park_cluster_source_full_reference


export = bpy.data.collections.new("Export")
source = bpy.data.collections.new("SpeedTree_Source")
bpy.context.scene.collection.children.link(export)
bpy.context.scene.collection.children.link(source)

part_mesh_data = bpy.data.meshes.new("SK_branch_elm_01_01_MeshData")
part_mesh = bpy.data.objects.new("SK_branch_elm_01_01_Mesh", part_mesh_data)
part_armature_data = bpy.data.armatures.new("SK_branch_elm_01_01_ArmatureData")
part_armature = bpy.data.objects.new(
    "SK_branch_elm_01_01_Armature",
    part_armature_data,
)
part_pivot = bpy.data.objects.new("SK_branch_elm_01_01", None)
for obj, role in (
    (part_mesh, "skeletal_mesh"),
    (part_armature, "skeletal_armature"),
    (part_pivot, "send2ue_pivot"),
):
    obj["speedtree_cluster_generated"] = True
    obj["speedtree_cluster_asset_role"] = role
    export.objects.link(obj)
part_mesh.parent = part_armature
part_armature.parent = part_pivot

root_data = bpy.data.armatures.new("RootData")
root = bpy.data.objects.new("Root", root_data)
merged_data = bpy.data.meshes.new("SK_branch_elm_01_MergedData")
merged = bpy.data.objects.new(
    "SK_branch_elm_01_Codex_Assembled",
    merged_data,
)
full_pivot = bpy.data.objects.new("SK_branch_elm_01", None)
source_fbx = r"D:\Tree_elm\Cluster\fbx\SK_branch_elm_01.fbx"
for obj in (root, merged, full_pivot):
    obj["codex_source_fbx"] = source_fbx
    export.objects.link(obj)
root.parent = None
full_pivot.parent = root
merged.parent = full_pivot

result = park_cluster_source_full_reference(
    root,
    merged,
    source_fbx,
)

export_units = sorted(
    obj.name
    for obj in export.objects
    if obj.type == "EMPTY" and obj.children
)
if export_units != ["SK_branch_elm_01_01"]:
    raise RuntimeError(f"Unexpected Cluster Export units: {export_units}")
if bpy.data.objects.get("SK_branch_elm_01") is not None:
    raise RuntimeError("Unsuffixed Cluster Full SK pivot was not removed.")
for obj in (root, merged):
    if [collection.name for collection in obj.users_collection] != [
        "SpeedTree_Source"
    ]:
        raise RuntimeError(
            f"Full reference was not parked in SpeedTree_Source: {obj.name}"
        )

print(
    "CODEX_RESULT="
    + json.dumps(
        {
            "status": "ok",
            "export_units": export_units,
            "park": result,
        },
        ensure_ascii=False,
    )
)
