import sys
import unittest
from pathlib import Path

import bpy


ADDONS = Path(__file__).resolve().parents[1] / "addons"
if str(ADDONS) not in sys.path:
    sys.path.insert(0, str(ADDONS))

from speedtree_bone_weight_repair import core  # noqa: E402


def records():
    return [
        {
            "name": "Trunk",
            "bone_index": 0,
            "parent_index": -1,
            "group": 0,
        },
        {
            "name": "Branch",
            "bone_index": 1,
            "parent_index": 0,
            "group": 1,
        },
        {
            "name": "Leaf",
            "bone_index": 2,
            "parent_index": 1,
            "group": 2,
        },
    ]


class FinalSkeletonWindContractTests(unittest.TestCase):
    def test_wind_json_uses_final_name_index_parent_order(self):
        data = core.build_dynamic_wind_data(
            records(),
            [
                {"index": 0, "is_trunk_group": True},
                {"index": 1, "mean_radius": 0.5},
                {"index": 2, "mean_radius": 0.1},
            ],
            import_root_name="Root",
        )

        self.assertEqual(data["SkeletonContract"]["SchemaVersion"], 2)
        self.assertEqual(data["SkeletonContract"]["BoneCount"], 3)
        self.assertEqual(
            [(row["BoneName"], row["BoneIndex"], row["ParentIndex"])
             for row in data["SkeletonContract"]["Bones"]],
            [
                ("Trunk", 0, -1),
                ("Branch", 1, 0),
                ("Leaf", 2, 1),
            ],
        )
        self.assertEqual(
            [(row["JointName"], row["BoneIndex"], row["ParentIndex"])
             for row in data["Joints"]],
            [("Trunk", 0, -1), ("Branch", 1, 0), ("Leaf", 2, 1)],
        )
        self.assertEqual(
            len(data["SkeletonContract"]["BoneNameIndexParentSha1"]), 40
        )

    def test_missing_native_id_zero_cluster_restores_exact_root_remainder(self):
        armature_data = bpy.data.armatures.new("ReceiptArmatureData")
        armature = bpy.data.objects.new("ReceiptArmature", armature_data)
        bpy.context.scene.collection.objects.link(armature)
        bpy.context.view_layer.objects.active = armature
        armature.select_set(True)
        bpy.ops.object.mode_set(mode="EDIT")
        root = armature_data.edit_bones.new("Root")
        root.head = (0.0, 0.0, 0.0)
        root.tail = (0.0, 0.0, 1.0)
        child = armature_data.edit_bones.new("Bone_1_Start")
        child.head = (0.0, 0.0, 1.0)
        child.tail = (0.0, 0.0, 2.0)
        child.parent = root
        bpy.ops.object.mode_set(mode="OBJECT")

        mesh_data = bpy.data.meshes.new("ReceiptMeshData")
        mesh_data.from_pydata(
            [(0.0, 0.0, 0.0), (1.0, 0.0, 0.0), (0.0, 1.0, 0.0)],
            [],
            [(0, 1, 2)],
        )
        mesh = bpy.data.objects.new("ReceiptMesh", mesh_data)
        bpy.context.scene.collection.objects.link(mesh)
        child_group = mesh.vertex_groups.new(name="Bone_1_Start")
        child_group.add([0], 0.75, "REPLACE")
        child_group.add([1], 1.0, "REPLACE")

        result = core.restore_omitted_native_root_weights(
            [armature, mesh],
            {"id_zero_cluster_write": "omitted_no_exact_bone_record"},
        )

        self.assertEqual(result["status"], "applied")
        self.assertEqual(result["changed_vertex_count"], 2)
        root_group = mesh.vertex_groups["Root"]
        self.assertAlmostEqual(root_group.weight(0), 0.25, places=6)
        self.assertAlmostEqual(root_group.weight(2), 1.0, places=6)
        with self.assertRaises(RuntimeError):
            root_group.weight(1)

    def test_index_gap_is_rejected(self):
        broken = records()
        broken[2]["bone_index"] = 3

        with self.assertRaisesRegex(RuntimeError, "unique and contiguous"):
            core.build_dynamic_wind_data(broken, [], import_root_name="ArmatureRoot")

    def test_duplicate_name_is_rejected(self):
        broken = records()
        broken[2]["name"] = "Branch"

        with self.assertRaisesRegex(RuntimeError, "duplicate final bone names"):
            core.build_dynamic_wind_data(broken, [], import_root_name="ArmatureRoot")

    def test_parent_out_of_range_is_rejected(self):
        broken = records()
        broken[2]["parent_index"] = 7

        with self.assertRaisesRegex(RuntimeError, "parent index out of range"):
            core.build_dynamic_wind_data(broken, [], import_root_name="ArmatureRoot")

    def test_ungrouped_bone_is_identity_only(self):
        broken = records()
        broken[1]["group"] = None

        data = core.build_dynamic_wind_data(
            broken,
            [{"index": 0, "is_trunk_group": True}, {"index": 2}],
            import_root_name="ArmatureRoot",
        )

        self.assertEqual(data["SkeletonContract"]["BoneCount"], 3)
        self.assertEqual(
            [row["JointName"] for row in data["Joints"]],
            ["Trunk", "Leaf"],
        )

    def test_armature_object_name_is_not_part_of_exported_skeleton(self):
        data = core.build_dynamic_wind_data(
            records(), [], import_root_name="Trunk"
        )

        self.assertEqual(
            data["SkeletonContract"]["ImportRoot"],
            {
                "BoneName": "Trunk",
                "BoneIndex": 0,
                "ParentIndex": -1,
                "Source": "blender_armature_bone",
                "ExportContract": "send2ue_fbx_authored_bone_root",
            },
        )

    def test_elm_pilot_shape_preserves_authored_root_indices(self):
        blender_bones = [
            {
                "name": f"Bone_{index}",
                "bone_index": index,
                "parent_index": index - 1,
                "group": 0,
            }
            for index in range(1687)
        ]

        data = core.build_dynamic_wind_data(
            blender_bones,
            [{"index": 0, "is_trunk_group": True}],
            import_root_name="Root",
        )

        self.assertEqual(data["SkeletonContract"]["BoneCount"], 1687)
        self.assertEqual(len(data["SkeletonContract"]["Bones"]), 1687)
        self.assertEqual(len(data["Joints"]), 1687)
        self.assertEqual(
            data["SkeletonContract"]["Bones"][0],
            {"BoneName": "Bone_0", "BoneIndex": 0, "ParentIndex": -1},
        )
        self.assertEqual(data["Joints"][0]["BoneIndex"], 0)
        self.assertEqual(data["Joints"][0]["ParentIndex"], -1)

    def test_negative_simulation_group_is_rejected(self):
        broken = records()
        broken[1]["group"] = -1

        with self.assertRaisesRegex(RuntimeError, "group index is negative"):
            core.build_dynamic_wind_data(
                broken,
                [],
                import_root_name="ArmatureRoot",
            )

    def test_loaded_isolated_blend_uses_authored_bone_root(self):
        armatures = [obj for obj in bpy.data.objects if obj.type == "ARMATURE"]
        if not bpy.data.filepath or not armatures:
            self.skipTest("no isolated Blender armature loaded")
        self.assertEqual(len(armatures), 1)
        armature = armatures[0]
        bones = list(armature.data.bones)
        source_records = [
            {
                "name": bone.name,
                "bone_index": index,
                "parent_index": (
                    armature.data.bones.find(bone.parent.name)
                    if bone.parent is not None
                    else -1
                ),
                "group": 0,
            }
            for index, bone in enumerate(bones)
        ]

        data = core.build_dynamic_wind_data(
            source_records,
            [{"index": 0, "is_trunk_group": True}],
            import_root_name=armature.name,
        )

        self.assertEqual(
            data["SkeletonContract"]["BoneCount"], len(bones)
        )
        self.assertEqual(
            data["SkeletonContract"]["Bones"][0]["BoneName"], bones[0].name
        )
        if len(bones) == 1687 and armature.name == "Root":
            self.assertEqual(data["SkeletonContract"]["BoneCount"], 1687)
            self.assertEqual(len(data["Joints"]), 1687)


if __name__ == "__main__":
    unittest.main(argv=[__file__])
