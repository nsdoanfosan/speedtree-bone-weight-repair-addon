"""Regression coverage for BaseRef-only SpeedTree root parenting."""

from __future__ import annotations

import sys
from pathlib import Path


REPO_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_DIR / "addons"))

from speedtree_bone_weight_repair import core


def bone(parent=None):
    return {
        "parent": parent,
        "head": (0.0, 0.0, 0.0),
        "tail": (0.0, 0.0, 1.0),
    }


bones = {
    "Bone_1_Start": bone(),
    "Bone_1_End": bone("Bone_1_Start"),
    "Bone_2_Start": bone(),
    "Bone_2_End": bone("Bone_2_Start"),
    "Bone_3_Start": bone(),
    "Bone_3_End": bone("Bone_3_Start"),
}

mapping, details, problems, roots, orphan_roots = (
    core.build_root_fallback_reparent_map(bones, "Bone_1_Start")
)

assert mapping == {}, mapping
assert problems == [], problems
assert roots == ["Bone_1_Start", "Bone_2_Start", "Bone_3_Start"], roots
assert orphan_roots == ["Bone_2_Start", "Bone_3_Start"], orphan_roots
assert [row["child_bone"] for row in details] == orphan_roots, details
assert all(row["parent_bone"] is None for row in details), details
assert {
    row["method"] for row in details
} == {"preserve_independent_root_without_baseref"}, details

mixed_details = core.build_independent_root_preservation_details(
    ["Bone_3_Start"],
    "no usable Base/BaseRef match for this FBX root",
)
assert mixed_details == [
    {
        "child_bone": "Bone_3_Start",
        "parent_bone": None,
        "method": "preserve_independent_root_without_baseref",
        "reason": "no usable Base/BaseRef match for this FBX root",
    }
], mixed_details

mixed_bones = {
    "Bone_1_Start": {
        **bone(),
        "head": (0.0, 0.0, 0.0),
        "tail": (0.0, 0.0, 1.0),
    },
    "Bone_1_End": {
        **bone("Bone_1_Start"),
        "head": (0.0, 0.0, 1.0),
        "tail": (0.0, 0.0, 2.0),
    },
    "Bone_2_Start": {
        **bone(),
        "head": (1.0, 0.0, 0.0),
        "tail": (1.0, 0.0, 1.0),
    },
    "Bone_3_Start": {
        **bone(),
        "head": (10.0, 0.0, 0.0),
        "tail": (10.0, 0.0, 1.0),
    },
}
mixed_mapping, _, _, _, mixed_orphans = core.build_reparent_map(
    [
        {
            "child_coord_raw": (1.0, 0.0, 0.0),
            "parent_branch_coord_raw": (0.0, 0.0, 0.0),
            "attach_coord_raw": (0.0, 0.0, 0.5),
        }
    ],
    mixed_bones,
    {"Bone_1_Start": ["Bone_1_End"]},
    "Bone_1_Start",
    1.0,
    0.08,
)
assert set(mixed_mapping) == {"Bone_2_Start"}, mixed_mapping
assert [
    root for root in mixed_orphans if root not in mixed_mapping
] == ["Bone_3_Start"], (mixed_orphans, mixed_mapping)

# The Blender roots remain independent, while the final FBX/Unreal identity
# contract gives all of them the armature-object root at index zero.
final_bones, skeleton_contract = core.build_final_skeleton_wind_contract(
    [
        {
            "name": "Bone_1_Start",
            "bone_index": 0,
            "parent_index": -1,
            "group": 0,
        },
        {
            "name": "Bone_2_Start",
            "bone_index": 1,
            "parent_index": -1,
            "group": 0,
        },
        {
            "name": "Bone_3_Start",
            "bone_index": 2,
            "parent_index": -1,
            "group": 0,
        },
    ],
    "Root",
)
assert final_bones[0]["name"] == "Root", final_bones
assert [row["parent_index"] for row in final_bones[1:]] == [0, 0, 0], final_bones
assert skeleton_contract["ImportRoot"]["BoneName"] == "Root", skeleton_contract

print("BASEREF_ROOT_CONTRACT_SMOKE_OK")
