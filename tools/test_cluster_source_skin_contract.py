"""Blender smoke test for authored multi-axis and unskinned single-axis Clusters."""

from __future__ import annotations

import json
import sys

import addon_utils
import bpy


arguments = sys.argv[sys.argv.index("--") + 1 :] if "--" in sys.argv else []
if len(arguments) < 2:
    raise SystemExit(
        "Usage: blender -b --python test_cluster_source_skin_contract.py -- "
        "FILE.fbx FILE.xml"
    )

fbx_path = arguments[0]
xml_path = arguments[1]
bpy.ops.wm.read_factory_settings(use_empty=True)
addon_utils.enable(
    "speedtree_bone_weight_repair",
    default_set=False,
    persistent=False,
)

from speedtree_bone_weight_repair.core import run_import_source_fbx


result = run_import_source_fbx(
    fbx_path,
    rigid_fallback=True,
    cluster_source_skin_contract=True,
    cluster_source_xml_path=xml_path,
)
contract = result.get("cluster_source_skin_contract") or {}
status = contract.get("status")
if status not in {
    "bound_unskinned_single_axis",
    "preserved_authored_skin",
}:
    raise RuntimeError(f"Cluster skin contract was not resolved: {contract}")

armature = bpy.data.objects.get(contract["armature"])
if armature is None:
    raise RuntimeError("Bound Cluster armature is missing.")
meshes = [
    obj
    for obj in bpy.context.scene.objects
    if obj.type == "MESH" and obj.data and len(obj.data.vertices) > 0
]
axis_normalization = contract.get("axis_normalization") or {}
expected_axes = int(axis_normalization.get("axis_count") or 0)
bone_names = [bone.name for bone in armature.data.bones]
if (
    expected_axes <= 0
    or len(bone_names) != expected_axes
    or bone_names != [f"Bone_{index}_Start" for index in range(1, expected_axes + 1)]
):
    raise RuntimeError(
        f"Cluster axes were not canonicalized to one Start bone per XML root: "
        f"{bone_names}, contract={axis_normalization}"
    )
bone_name = contract.get("bone")
if status == "bound_unskinned_single_axis":
    for obj in meshes:
        if [group.name for group in obj.vertex_groups] != [bone_name]:
            raise RuntimeError(
                f"Unexpected Cluster vertex groups on {obj.name}: "
                f"{[group.name for group in obj.vertex_groups]}"
            )
        group_index = obj.vertex_groups[bone_name].index
        for vertex in obj.data.vertices:
            weights = {
                element.group: float(element.weight)
                for element in vertex.groups
            }
            if abs(weights.get(group_index, 0.0) - 1.0) > 1.0e-7:
                raise RuntimeError(
                    f"Cluster vertex is not rigidly bound: "
                    f"{obj.name}[{vertex.index}]"
                )
        modifiers = [
            modifier
            for modifier in obj.modifiers
            if modifier.type == "ARMATURE"
        ]
        if len(modifiers) != 1 or modifiers[0].object != armature:
            raise RuntimeError(
                f"Cluster mesh has an invalid armature modifier: {obj.name}"
            )
        if obj.parent != armature:
            raise RuntimeError(
                f"Cluster mesh is not parented to the axis armature: {obj.name}"
            )
else:
    if contract.get("deform_bone_count", 0) <= 1:
        raise RuntimeError(
            "Authored-skin preservation did not retain a multi-axis Cluster."
        )
    if contract.get("skinned_mesh_count", 0) <= 0:
        raise RuntimeError("Authored Cluster skin inventory is empty.")

print(
    "CODEX_RESULT="
    + json.dumps(
        {
            "status": "ok",
            "skin_status": status,
            "fbx": fbx_path,
            "armature": armature.name,
            "bone": bone_name,
            "bone_count": len(armature.data.bones),
            "mesh_count": len(meshes),
            "vertex_count": sum(len(obj.data.vertices) for obj in meshes),
            "binding": contract,
        },
        ensure_ascii=False,
    )
)
sys.stdout.flush()
