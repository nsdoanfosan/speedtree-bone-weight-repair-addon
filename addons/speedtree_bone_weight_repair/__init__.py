bl_info = {
    "name": "SpeedTree Bone/Weight Repair",
    "author": "OpenAI Codex",
    "version": (0, 1, 0),
    "blender": (5, 1, 0),
    "location": "View3D > Sidebar > SpeedTree > Bone/Weight Repair",
    "description": "Repair SpeedTree orphan branch root bones, loose leaf/cap instances, invalid weights, and export a merged skeletal FBX.",
    "category": "Import-Export",
}

import importlib
import traceback

import bpy
from bpy.props import (
    BoolProperty,
    FloatProperty,
    IntProperty,
    PointerProperty,
    StringProperty,
)
from bpy.types import Operator, Panel, PropertyGroup

from . import core


def get_core():
    return importlib.reload(core)


class STBWR_Settings(PropertyGroup):
    spm_path: StringProperty(
        name="SPM",
        description="Original SpeedTree .spm file used to recover Base/BaseRef branch parent links",
        subtype="FILE_PATH",
        default="",
    )
    armature_name: StringProperty(
        name="Armature",
        description="Armature object name",
        default="Root",
    )
    true_root: StringProperty(
        name="True Root",
        description="Only this _Start bone is treated as the real skeleton root",
        default="Bone_1_Start",
    )
    scale_value: StringProperty(
        name="SPM Scale",
        description="Use auto unless SPM coordinates need an explicit conversion scale",
        default="auto",
    )
    tolerance: FloatProperty(
        name="Tolerance",
        description="Maximum distance for matching SPM branch roots to Blender start bones",
        default=0.08,
        min=0.0,
        precision=4,
    )
    strict_reparent: BoolProperty(
        name="Strict Reparent",
        description="Block the step when SPM-to-bone parent mapping has unresolved entries",
        default=True,
    )
    leaf_name_contains: StringProperty(
        name="Loose Mesh Filter",
        description="Substring used to find loose leaf/cap instance meshes with no usable skinning",
        default="leaf",
    )
    leaf_out_name: StringProperty(
        name="Skinned Loose Mesh",
        description="Output object name for reconstructed loose instance skinning",
        default="Leaves_Skinned_Codex",
    )
    hide_originals: BoolProperty(
        name="Hide Originals",
        description="Hide original loose instance objects after creating the skinned replacement mesh",
        default=True,
    )
    skip_leaf_skin: BoolProperty(
        name="Skip Loose Mesh Skin",
        description="Skip reconstruction of disconnected loose leaf/cap instance skinning",
        default=False,
    )
    fallback_all_bones: BoolProperty(
        name="Fallback All Bones",
        description="When parent mesh groups are insufficient, allow nearest-bone assignment across the armature",
        default=False,
    )
    mesh_regex: StringProperty(
        name="Mesh Regex",
        description="Optional regular expression limiting weight repair and merge/export source meshes",
        default="",
    )
    fill_zero_weight: BoolProperty(
        name="Fill Zero Weights",
        description="Assign nearest valid bone to vertices with no usable bone weight",
        default=True,
    )
    remove_empty_invalid_groups: BoolProperty(
        name="Remove Empty Invalid Groups",
        description="Remove empty vertex groups that do not correspond to armature bones",
        default=True,
    )
    include_hidden: BoolProperty(
        name="Include Hidden In Merge",
        description="Include hidden skinned mesh objects in merged export source selection",
        default=False,
    )
    export_fbx: BoolProperty(
        name="Export FBX",
        description="Export merged skeletal FBX after repair",
        default=True,
    )
    max_samples_per_object: IntProperty(
        name="Report Samples",
        description="Maximum invalid/zero-weight sample vertices recorded per object",
        default=20,
        min=0,
        max=1000,
    )
    out_dir: StringProperty(
        name="Output Directory",
        description="Output directory. Empty uses the current .blend directory",
        subtype="DIR_PATH",
        default="",
    )
    name_stem: StringProperty(
        name="Output Name Stem",
        description="Output filename stem. Empty uses the current .blend filename",
        default="",
    )
    merged_name: StringProperty(
        name="Merged Object Name",
        description="Merged skeletal mesh object name. Empty uses an automatic name",
        default="",
    )

    def as_dict(self):
        return {
            "spm_path": bpy.path.abspath(self.spm_path),
            "armature_name": self.armature_name,
            "true_root": self.true_root,
            "scale_value": self.scale_value,
            "tolerance": self.tolerance,
            "strict_reparent": self.strict_reparent,
            "leaf_name_contains": self.leaf_name_contains,
            "leaf_out_name": self.leaf_out_name,
            "hide_originals": self.hide_originals,
            "skip_leaf_skin": self.skip_leaf_skin,
            "fallback_all_bones": self.fallback_all_bones,
            "mesh_regex": self.mesh_regex,
            "fill_zero_weight": self.fill_zero_weight,
            "remove_empty_invalid_groups": self.remove_empty_invalid_groups,
            "include_hidden": self.include_hidden,
            "export_fbx": self.export_fbx,
            "max_samples_per_object": self.max_samples_per_object,
            "out_dir": bpy.path.abspath(self.out_dir) if self.out_dir else "",
            "name_stem": self.name_stem,
            "merged_name": self.merged_name,
        }


class STBWR_OT_Base(Operator):
    bl_options = {"REGISTER"}

    def settings(self):
        return bpy.context.scene.speedtree_bwr_settings

    def fail(self, message):
        traceback.print_exc()
        self.report({"ERROR"}, message)
        return {"CANCELLED"}


class STBWR_OT_RunFullPipeline(STBWR_OT_Base):
    bl_idname = "speedtree_bwr.run_full_pipeline"
    bl_label = "Run Full Repair Pipeline"
    bl_description = "Run SPM reparent, loose mesh skinning, invalid weight repair, merge, and optional FBX export"

    def execute(self, context):
        settings = self.settings().as_dict()
        if not settings["spm_path"]:
            return self.fail("SPM path is required.")
        try:
            result = get_core().run_full_pipeline(settings)
        except Exception as exc:
            return self.fail(str(exc))
        self.report({"INFO"}, "SpeedTree repair pipeline done: " + result["paths"]["pipeline_report"])
        return {"FINISHED"}


class STBWR_OT_ReparentFromSPM(STBWR_OT_Base):
    bl_idname = "speedtree_bwr.reparent_from_spm"
    bl_label = "Repair Bone Parents From SPM"
    bl_description = "Use SPM Base/BaseRef relationships to reconnect orphan _Start branch roots"

    def execute(self, context):
        settings = self.settings().as_dict()
        if not settings["spm_path"]:
            return self.fail("SPM path is required.")
        try:
            paths = get_core().default_paths(settings)
            result = get_core().run_reparent_from_spm(
                settings["spm_path"],
                settings["armature_name"],
                settings["true_root"],
                settings["scale_value"],
                settings["tolerance"],
                apply=True,
                strict=settings["strict_reparent"],
                report_path=paths["reparent_report"],
            )
        except Exception as exc:
            return self.fail(str(exc))
        self.report({"INFO"}, f"Reparent status: {result.get('status')}, mapped {result.get('mapping_count', 0)} bones")
        return {"FINISHED"}


class STBWR_OT_SkinLooseInstances(STBWR_OT_Base):
    bl_idname = "speedtree_bwr.skin_loose_instances"
    bl_label = "Skin Loose Mesh Instances"
    bl_description = "Convert disconnected loose leaf/cap meshes into one skinned mesh using nearest valid branch bones"

    def execute(self, context):
        settings = self.settings().as_dict()
        try:
            paths = get_core().default_paths(settings)
            result = get_core().run_skin_loose_instances(
                settings["armature_name"],
                settings["leaf_name_contains"],
                settings["leaf_out_name"],
                hide_originals=settings["hide_originals"],
                fallback_all_bones=settings["fallback_all_bones"],
                apply=True,
                report_path=paths["leaf_report"],
            )
        except Exception as exc:
            return self.fail(str(exc))
        self.report({"INFO"}, f"Loose skin status: {result.get('status')}, instances {result.get('instance_count', 0)}")
        return {"FINISHED"}


class STBWR_OT_RepairInvalidWeights(STBWR_OT_Base):
    bl_idname = "speedtree_bwr.repair_invalid_weights"
    bl_label = "Repair Invalid Weights"
    bl_description = "Remove invalid non-bone influences and fill unweighted vertices with nearest valid bones"

    def execute(self, context):
        settings = self.settings().as_dict()
        try:
            paths = get_core().default_paths(settings)
            result = get_core().run_repair_invalid_weights(
                settings["armature_name"],
                mesh_regex=settings["mesh_regex"],
                fill_zero_weight=settings["fill_zero_weight"],
                remove_empty_invalid_groups=settings["remove_empty_invalid_groups"],
                max_samples_per_object=settings["max_samples_per_object"],
                report_path=paths["weight_report"],
            )
        except Exception as exc:
            return self.fail(str(exc))
        remaining = result.get("integrity_after_repair", {})
        self.report({"INFO"}, f"Weight repair done. Remaining invalid vertices: {remaining.get('invalid_weight_vertices', 0)}")
        return {"FINISHED"}


class STBWR_OT_MergeExport(STBWR_OT_Base):
    bl_idname = "speedtree_bwr.merge_export"
    bl_label = "Merge And Export FBX"
    bl_description = "Merge skinned meshes and export a skeletal FBX with valid bone weights only"

    def execute(self, context):
        settings = self.settings().as_dict()
        try:
            paths = get_core().default_paths(settings)
            fbx_path = paths["fbx"] if settings["export_fbx"] else ""
            result = get_core().run_merge_export(
                settings["armature_name"],
                paths["merged_name"],
                fbx_path=fbx_path,
                mesh_regex=settings["mesh_regex"],
                include_hidden=settings["include_hidden"],
                report_path=paths["export_report"],
            )
        except Exception as exc:
            return self.fail(str(exc))
        self.report({"INFO"}, f"Merge/export done. Zero-weight vertices: {result.get('zero_weight_vertices', 0)}")
        return {"FINISHED"}


class STBWR_PT_Main(Panel):
    bl_idname = "STBWR_PT_main"
    bl_label = "Bone/Weight Repair"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "SpeedTree"

    def draw(self, context):
        layout = self.layout
        settings = context.scene.speedtree_bwr_settings

        box = layout.box()
        box.label(text="Source")
        box.prop(settings, "spm_path")
        row = box.row(align=True)
        row.prop(settings, "armature_name")
        row.prop(settings, "true_root")
        row = box.row(align=True)
        row.prop(settings, "scale_value")
        row.prop(settings, "tolerance")
        box.prop(settings, "strict_reparent")

        box = layout.box()
        box.label(text="Loose Mesh Skinning")
        box.prop(settings, "skip_leaf_skin")
        col = box.column()
        col.enabled = not settings.skip_leaf_skin
        col.prop(settings, "leaf_name_contains")
        col.prop(settings, "leaf_out_name")
        row = col.row(align=True)
        row.prop(settings, "hide_originals")
        row.prop(settings, "fallback_all_bones")

        box = layout.box()
        box.label(text="Weight Repair / Export")
        box.prop(settings, "mesh_regex")
        row = box.row(align=True)
        row.prop(settings, "fill_zero_weight")
        row.prop(settings, "remove_empty_invalid_groups")
        row = box.row(align=True)
        row.prop(settings, "include_hidden")
        row.prop(settings, "export_fbx")
        box.prop(settings, "max_samples_per_object")

        box = layout.box()
        box.label(text="Outputs")
        box.prop(settings, "out_dir")
        box.prop(settings, "name_stem")
        box.prop(settings, "merged_name")

        layout.separator()
        layout.operator("speedtree_bwr.run_full_pipeline", icon="MOD_ARMATURE")
        row = layout.row(align=True)
        row.operator("speedtree_bwr.reparent_from_spm", text="Reparent")
        row.operator("speedtree_bwr.skin_loose_instances", text="Skin Loose")
        row = layout.row(align=True)
        row.operator("speedtree_bwr.repair_invalid_weights", text="Weights")
        row.operator("speedtree_bwr.merge_export", text="Merge/FBX")


classes = (
    STBWR_Settings,
    STBWR_OT_RunFullPipeline,
    STBWR_OT_ReparentFromSPM,
    STBWR_OT_SkinLooseInstances,
    STBWR_OT_RepairInvalidWeights,
    STBWR_OT_MergeExport,
    STBWR_PT_Main,
)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)
    bpy.types.Scene.speedtree_bwr_settings = PointerProperty(type=STBWR_Settings)


def unregister():
    if hasattr(bpy.types.Scene, "speedtree_bwr_settings"):
        del bpy.types.Scene.speedtree_bwr_settings
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
