import sys
import unittest
from pathlib import Path

import bpy


ADDONS = Path(__file__).resolve().parents[1] / "addons"
if str(ADDONS) not in sys.path:
    sys.path.insert(0, str(ADDONS))

from speedtree_bone_weight_repair import core  # noqa: E402


def make_mesh_object(name, vertices, faces):
    mesh = bpy.data.meshes.new(name + "Mesh")
    mesh.from_pydata(vertices, [], faces)
    mesh.update()
    obj = bpy.data.objects.new(name, mesh)
    bpy.context.scene.collection.objects.link(obj)
    return obj


class BranchPlaneSkinningTests(unittest.TestCase):
    def setUp(self):
        bpy.ops.wm.read_factory_settings(use_empty=True)

        armature_data = bpy.data.armatures.new("RootArmature")
        self.armature = bpy.data.objects.new("Root", armature_data)
        bpy.context.scene.collection.objects.link(self.armature)
        bpy.context.view_layer.objects.active = self.armature
        self.armature.select_set(True)
        bpy.ops.object.mode_set(mode="EDIT")
        bone = armature_data.edit_bones.new("Bone_1_End")
        bone.head = (0.0, 0.0, 0.0)
        bone.tail = (0.0, 0.0, 2.0)
        bpy.ops.object.mode_set(mode="OBJECT")

        bark = make_mesh_object(
            "M_Bark_test_01",
            [(-1.0, 0.0, 0.0), (1.0, 0.0, 0.0), (0.0, 0.0, 2.0)],
            [(0, 1, 2)],
        )
        bark_group = bark.vertex_groups.new(name="Bone_1_End")
        bark_group.add([0, 1, 2], 1.0, "REPLACE")
        bark_modifier = bark.modifiers.new(name="Root", type="ARMATURE")
        bark_modifier.object = self.armature

        self.branch = make_mesh_object(
            "M_branch_test_01",
            [
                (-0.4, 0.1, 0.5),
                (0.4, 0.1, 0.5),
                (0.0, 0.1, 1.0),
                (-0.3, -0.1, 1.2),
                (0.3, -0.1, 1.2),
                (0.0, -0.1, 1.7),
            ],
            [(0, 1, 2), (3, 4, 5)],
        )
        material = bpy.data.materials.new("M_branch_test_01")
        self.branch.data.materials.append(material)
        uv_map = self.branch.data.uv_layers.new(name="UVMap")
        uv_values = [
            0.0, 0.0,
            1.0, 0.0,
            0.0, 1.0,
            0.0, 0.0,
            1.0, 0.0,
            0.0, 1.0,
        ]
        uv_map.data.foreach_set("uv", uv_values)

    def test_branch_plane_material_and_geometry_survive_skinning(self):
        report = core.run_skin_loose_instances(
            "Root",
            "branch",
            "Branches_Skinned_Codex",
            hide_originals=True,
            fallback_all_bones=False,
            apply=True,
            spm_path="",
        )

        self.assertEqual(report["status"], "applied")
        self.assertEqual(report["created_faces"], 2)
        output = bpy.data.objects["Branches_Skinned_Codex"]
        self.assertEqual(
            [material.name for material in output.data.materials],
            ["M_branch_test_01"],
        )
        self.assertEqual(len(output.data.polygons), 2)
        self.assertEqual(
            [layer.name for layer in output.data.uv_layers],
            ["uv0", "blend_ao"],
        )
        output_uv0 = [
            tuple(round(float(value), 6) for value in item.uv)
            for item in output.data.uv_layers["uv0"].data
        ]
        self.assertEqual(
            output_uv0,
            [
                (0.0, 0.0),
                (1.0, 0.0),
                (0.0, 1.0),
                (0.0, 0.0),
                (1.0, 0.0),
                (0.0, 1.0),
            ],
        )
        self.assertTrue(
            all(
                tuple(float(value) for value in item.uv) == (1.0, 1.0)
                for item in output.data.uv_layers["blend_ao"].data
            )
        )
        self.assertTrue(
            all(
                any(link.weight > 0.0 for link in vertex.groups)
                for vertex in output.data.vertices
            )
        )
        self.assertIsNotNone(core.armature_modifier(output, self.armature))
        self.assertTrue(self.branch.hide_viewport)


if __name__ == "__main__":
    unittest.main(argv=[__file__])
