bl_info = {
    "name": "SpeedTree Bone/Weight Repair",
    "author": "OpenAI Codex",
    "version": (0, 3, 0),
    "blender": (5, 1, 0),
    "location": "View3D > Sidebar > SpeedTree > Bone/Weight Repair",
    "description": "Repair SpeedTree orphan branch root bones, loose leaf/cap instances, invalid weights, and export a merged skeletal FBX.",
    "category": "Import-Export",
}

# MegaPlant conversion roadmap
#
# This add-on is becoming a staged SpeedTree-to-MegaPlant conversion tool, not
# just a one-off bone repair utility. Keep new work aligned to these stages:
#
# 1. Repair skeleton hierarchy from the original SPM.
#    SpeedTree exports can leave branch root bones with no usable parent
#    relationship. Rebuild those Base/BaseRef links first; all later generated
#    3D geometry must bind to this repaired armature, not to the broken import.
#
# 2. Parked note: convert branch/frond cards into 3D branch clusters.
#    This direction is currently not part of the active add-on UI/operator set.
#    Keep the idea as a reference only unless the workflow returns to it later.
#    The previous prototype matched SpeedTree XML Frond names/material IDs to
#    pre-grouped clusters such as branch_elm_01_A/B/C, but it is not registered.
#
# 3. Convert leaf cards/instances into 3D leaf geometry.
#    Leaf data may be stored as instances rather than the same branch-card
#    structure, so this should be a separate stage with its own matching and
#    weight-transfer rules.
#
# 4. Merge/export using the repaired skeleton and generated 3D meshes.
#    Any active generated replacements must be skinned to the existing repaired
#    bones before merge/export.
#
# 5. Emit Unreal/MegaPlant grouping data.
#    The Unreal-side process needs JSON grouping metadata so generated branch
#    and leaf geometry can be interpreted as MegaPlant-style data. This JSON
#    should be deterministic conversion metadata: provenance, generated object
#    records, repaired skeleton summary, binding summaries, and validation
#    results. It should not become a live Unreal wind-tuning scratchpad.
#
# 6. Validate Unreal import/runtime behavior.
#    Import the exported skeletal FBX and JSON into a separate Unreal test path
#    first. Verify that the repaired hierarchy imports cleanly, generated
#    geometry remains skinned, JSON groups resolve to imported geometry, and
#    TreeWind/DynamicWind runtime control is owned by Unreal actor/controller
#    code without preview-wind conflicts or hard sine-like stepping.

import importlib
import traceback
from pathlib import Path

import bpy
from bpy.props import (
    BoolProperty,
    EnumProperty,
    FloatProperty,
    IntProperty,
    PointerProperty,
    StringProperty,
)
from bpy.types import Operator, Panel, PropertyGroup

from . import core


BUNDLED_PRESET_DIR = Path(__file__).parent / "presets" / "speedtree_10_1"
BUNDLED_FBX_EXPORT_OPTIONS = str(BUNDLED_PRESET_DIR / "Options_MA_Fbx.ini")
BUNDLED_XML_EXPORT_OPTIONS = str(BUNDLED_PRESET_DIR / "Options_HI_Xml.ini")


# Immutable wind response categories. These values are only default snapshots
# for backward compatibility; Unreal owns one shared editable profile per ID.
# The level/weather DynamicWind source controller is a separate system.
WIND_PRESETS = {
    "TREE": {"flexibility": 1.0, "gust_attenuation": 0.25, "ground_cover": False},
    "BUSH": {"flexibility": 1.4, "gust_attenuation": 0.40, "ground_cover": False},
    "WEED": {"flexibility": 1.8, "gust_attenuation": 0.60, "ground_cover": True},
    "NONE": {"flexibility": 0.0, "gust_attenuation": 0.0, "ground_cover": False},
}

WIND_PRESET_ALIASES = {"GRASS": "WEED"}

WIND_PRESET_ITEMS = (
    ("TREE", "Tree", "Immutable TREE response category; shared values are edited in Unreal"),
    ("BUSH", "Bush", "Immutable BUSH response category; shared values are edited in Unreal"),
    ("WEED", "Weed", "Immutable WEED response category; shared values are edited in Unreal"),
    ("NONE", "None", "Immutable NONE response category; shared Unreal defaults are zero"),
)


def canonical_wind_preset_id(value):
    preset_id = str(value or "TREE").strip().upper()
    preset_id = WIND_PRESET_ALIASES.get(preset_id, preset_id)
    return preset_id if preset_id in WIND_PRESETS else "TREE"


def resolve_wind_values(settings):
    return dict(WIND_PRESETS[canonical_wind_preset_id(settings.wind_preset)])


def get_core():
    return importlib.reload(core)


class STBWR_Settings(PropertyGroup):
    source_fbx_path: StringProperty(
        name="Source FBX",
        description="Source SpeedTree FBX to import into Blender before repair",
        subtype="FILE_PATH",
        default="",
    )
    spm_path: StringProperty(
        name="SPM",
        description="Original SpeedTree .spm file used to recover Base/BaseRef branch parent links",
        subtype="FILE_PATH",
        default="",
    )
    texture_contract_path: StringProperty(
        name="Texture Contract",
        description="Optional SK preflight report containing the shared SpeedTree texture bindings",
        subtype="FILE_PATH",
        default="",
    )
    speedtree_exe_path: StringProperty(
        name="SpeedTree 10.1",
        description="SpeedTree Modeler executable used for command-line SPM export",
        subtype="FILE_PATH",
        default=r"C:\Program Files\SpeedTree\SpeedTree Modeler v10.1.0\win64\SpeedTree_Modeler.exe",
    )
    speedtree_export_options_path: StringProperty(
        name="Export INI",
        description="Legacy fallback SpeedTree export options .ini used only when the FBX/XML INI fields are empty",
        subtype="FILE_PATH",
        default="",
    )
    speedtree_fbx_export_options_path: StringProperty(
        name="FBX Export INI",
        description="SpeedTree FBX export preset. Default is bundled Options_MA_Fbx.ini (FBX grouped by material)",
        subtype="FILE_PATH",
        default=BUNDLED_FBX_EXPORT_OPTIONS,
    )
    speedtree_xml_export_options_path: StringProperty(
        name="XML Export INI",
        description="SpeedTree XML export preset. Default is bundled Options_HI_Xml.ini (XML grouped by hierarchy)",
        subtype="FILE_PATH",
        default=BUNDLED_XML_EXPORT_OPTIONS,
    )
    speedtree_output_root: StringProperty(
        name="ST Output Root",
        description="Root folder for SpeedTree exports. Empty writes FBX/XML subfolders next to the SPM",
        subtype="DIR_PATH",
        default="",
    )
    speedtree_export_fbx: BoolProperty(
        name="FBX",
        description="Export FBX from SpeedTree before Blender repair",
        default=True,
    )
    speedtree_export_xml: BoolProperty(
        name="XML",
        description="Export SpeedTree Raw XML from SpeedTree before Blender repair",
        default=True,
    )
    xml_path: StringProperty(
        name="XML",
        description="SpeedTreeRaw XML export; adds per-bone Generator/Mass/Radius wind-group metadata to the MegaPlant JSON",
        subtype="FILE_PATH",
        default="",
    )
    xml_trunk_generator_regex: StringProperty(
        name="Trunk Generators",
        description="Regex for XML Generator names that form simulation group 0 (the trunk group)",
        default="trunk",
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
    # Parked 3D Branch Cluster settings.
    # These were part of an experimental Frond_* card replacement direction and
    # are intentionally not exposed/registering as active add-on controls now.
    #
    # tree_xml_path
    # branch_cluster_blend
    # branch_material_id
    # branch_cluster_prefix
    # branch_replacement_collection
    # hide_original_branch_cards
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
        default=False,
    )
    save_intermediate_blends: BoolProperty(
        name="Save Stage Blends",
        description="Save a .blend snapshot after each pipeline stage (slow on large scenes; off keeps everything in the open file)",
        default=False,
    )
    make_export_structure: BoolProperty(
        name="Build Export Structure",
        description="After merge, build Export collection > armature > source mesh Empty > merged mesh",
        default=True,
    )
    export_collection_name: StringProperty(
        name="Export Collection",
        description="Collection that receives the final armature/source mesh Empty/mesh hierarchy",
        default="Export",
    )
    source_collection_name: StringProperty(
        name="Source Collection",
        description="Collection for raw FBX imports and non-unit objects swept out of the Export collection",
        default="SpeedTree_Source",
    )
    write_unreal_json: BoolProperty(
        name="Write MegaPlant JSON",
        description="Write the per-bone MegaPlant simulation-group JSON (rich record + Unreal-ready dynamic wind import file)",
        default=True,
    )
    write_dynamic_wind_json: BoolProperty(
        name="Write Dynamic Wind Import JSON",
        description="Also write the lean *_dynamic_wind_import_from_megaplant_groups.json that Unreal's CodexDynamicWindImportLibrary imports directly (needs the XML)",
        default=True,
    )
    wind_preset: EnumProperty(
        name="Wind Preset",
        description="Immutable response category assigned upstream; edit its shared numeric profile in Unreal",
        items=WIND_PRESET_ITEMS,
        default="TREE",
    )
    dynamic_wind_flexibility: FloatProperty(
        name="Wind Flexibility",
        description="How much the whole tree sways. Per-group influence is derived automatically from bone thickness (thin twigs sway, thick trunk stiff); this one knob scales it all",
        default=1.0,
        min=0.0,
        max=2.0,
    )
    dynamic_wind_gust_attenuation: FloatProperty(
        name="Gust Attenuation",
        description="GustAttenuation written into the dynamic wind import JSON (overridable in Unreal)",
        default=0.25,
        min=0.0,
        max=1.0,
    )
    dynamic_wind_ground_cover: BoolProperty(
        name="Ground Cover",
        description="bIsGroundCover flag written into the dynamic wind import JSON",
        default=False,
    )
    preview_influence: BoolProperty(
        name="Color by Wind Influence",
        description="Preview colors vertices by how much they sway (blue stiff -> red flexible) instead of by simulation group",
        default=True,
    )
    json_output_path: StringProperty(
        name="JSON Path",
        description="Optional explicit MegaPlant tree grouping JSON output path. Empty uses the default output name",
        subtype="FILE_PATH",
        default="",
    )
    json_asset_id: StringProperty(
        name="Asset ID",
        description="Stable asset identifier written to the Unreal/MegaPlant JSON",
        default="",
    )
    json_unreal_content_path: StringProperty(
        name="Unreal Path",
        description="Expected Unreal content path or import label written as metadata",
        default="/Game/MegaPlants/Test",
    )
    show_advanced_grouping: BoolProperty(
        name="Advanced JSON Fields",
        description="Show asset id, Unreal content path, and explicit JSON output path",
        default=False,
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
        wind = resolve_wind_values(self)
        return {
            "source_fbx_path": bpy.path.abspath(self.source_fbx_path) if self.source_fbx_path else "",
            "spm_path": bpy.path.abspath(self.spm_path),
            "texture_contract_path": (
                bpy.path.abspath(self.texture_contract_path)
                if self.texture_contract_path
                else ""
            ),
            "speedtree_exe_path": bpy.path.abspath(self.speedtree_exe_path) if self.speedtree_exe_path else "",
            "speedtree_export_options_path": bpy.path.abspath(self.speedtree_export_options_path) if self.speedtree_export_options_path else "",
            "speedtree_fbx_export_options_path": bpy.path.abspath(self.speedtree_fbx_export_options_path) if self.speedtree_fbx_export_options_path else "",
            "speedtree_xml_export_options_path": bpy.path.abspath(self.speedtree_xml_export_options_path) if self.speedtree_xml_export_options_path else "",
            "speedtree_output_root": bpy.path.abspath(self.speedtree_output_root) if self.speedtree_output_root else "",
            "speedtree_export_fbx": self.speedtree_export_fbx,
            "speedtree_export_xml": self.speedtree_export_xml,
            "xml_path": bpy.path.abspath(self.xml_path) if self.xml_path else "",
            "xml_trunk_generator_regex": self.xml_trunk_generator_regex,
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
            "save_intermediate_blends": self.save_intermediate_blends,
            "make_export_structure": self.make_export_structure,
            "export_collection_name": self.export_collection_name,
            "source_collection_name": self.source_collection_name,
            "write_unreal_json": self.write_unreal_json,
            "write_dynamic_wind_json": self.write_dynamic_wind_json,
            "wind_preset": canonical_wind_preset_id(self.wind_preset),
            "dynamic_wind_flexibility": wind["flexibility"],
            "dynamic_wind_gust_attenuation": wind["gust_attenuation"],
            "dynamic_wind_ground_cover": wind["ground_cover"],
            "preview_influence": self.preview_influence,
            "json_output_path": bpy.path.abspath(self.json_output_path) if self.json_output_path else "",
            "json_asset_id": self.json_asset_id,
            "json_unreal_content_path": self.json_unreal_content_path,
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


def set_viewport_to_group_preview(context):
    # Show the active Color Attribute directly; preview must not swap materials.
    screen = getattr(context, "screen", None)
    if not screen:
        return
    for area in screen.areas:
        if area.type != "VIEW_3D":
            continue
        for space in area.spaces:
            if space.type == "VIEW_3D":
                space.shading.type = "SOLID"
                for color_type in ("ATTRIBUTE", "VERTEX"):
                    try:
                        space.shading.color_type = color_type
                        break
                    except TypeError:
                        continue
                space.shading.light = "FLAT"


class STBWR_OT_ImportSourceFBX(STBWR_OT_Base):
    bl_idname = "speedtree_bwr.import_source_fbx"
    bl_label = "Import Source FBX"
    bl_description = "Import the selected SpeedTree source FBX into the current Blender scene"

    def execute(self, context):
        settings = self.settings().as_dict()
        if not settings["source_fbx_path"]:
            return self.fail("Source FBX path is required.")
        try:
            core = get_core()
            texture_contract = core.load_speedtree_runtime_texture_contract(
                settings.get("texture_contract_path", ""),
                spm_path=settings.get("spm_path", ""),
                source_fbx_path=settings.get("source_fbx_path", ""),
            )
            result = core.run_import_source_fbx(
                settings["source_fbx_path"],
                settings.get("source_collection_name", "SpeedTree_Source"),
                spm_path=settings.get("spm_path", ""),
                texture_contract=texture_contract,
            )
        except Exception as exc:
            return self.fail(str(exc))
        self.report(
            {"INFO"},
            f"Imported source FBX objects: {result.get('imported_object_count', 0)}, armatures: {result.get('imported_armature_count', 0)}",
        )
        return {"FINISHED"}


class STBWR_OT_ExportFromSpeedTree(STBWR_OT_Base):
    bl_idname = "speedtree_bwr.export_from_speedtree"
    bl_label = "SpeedTree -> Import -> Repair"
    bl_description = (
        "One button from the SPM: export FBX/XML from SpeedTree, import them, and "
        "run the full repair pipeline. Press again to update — the previous "
        "import/merge/export result is wiped first, so nothing stacks up"
    )

    def execute(self, context):
        scene_settings = self.settings()
        settings = scene_settings.as_dict()
        if not settings["spm_path"]:
            return self.fail("SPM path is required.")
        core = get_core()

        # 1) Export FBX/XML from SpeedTree and record the produced paths.
        try:
            export = core.run_speedtree_cli_export(
                settings["spm_path"],
                speedtree_exe_path=settings["speedtree_exe_path"],
                export_options_path=settings["speedtree_export_options_path"],
                fbx_export_options_path=settings["speedtree_fbx_export_options_path"],
                xml_export_options_path=settings["speedtree_xml_export_options_path"],
                output_root=settings["speedtree_output_root"],
                name_stem=settings["name_stem"],
                export_fbx=settings["speedtree_export_fbx"],
                export_xml=settings["speedtree_export_xml"],
            )
        except Exception as exc:
            return self.fail(str(exc))

        fbx = export["exports"].get("fbx", {})
        xml = export["exports"].get("xml", {})
        if fbx.get("exists"):
            scene_settings.source_fbx_path = fbx["path"]
        if xml.get("exists"):
            scene_settings.xml_path = xml["path"]
        if not scene_settings.source_fbx_path:
            return self.fail("SpeedTree export produced no FBX to import.")

        # 2) Wipe the previous build, re-import, and run the full pipeline.
        settings = scene_settings.as_dict()
        try:
            result = core.run_import_and_repair(settings)
        except Exception as exc:
            return self.fail(str(exc))

        warnings = result.get("warnings", [])
        cleared = len(result.get("cleanup", {}).get("removed_objects", []))
        export_structure = result.get("export_structure", {})
        if warnings:
            self.report({"WARNING"}, f"Done (cleared {cleared} old). JSON hollow: " + "; ".join(warnings))
        elif settings.get("make_export_structure", True) and not export_structure:
            self.report({"WARNING"}, f"Repair done, but Export structure was not built (cleared {cleared} old).")
        else:
            hierarchy = " > ".join(export_structure.get("hierarchy", []))
            suffix = f" Export: {hierarchy}" if hierarchy else ""
            self.report({"INFO"}, f"SpeedTree -> import -> repair done (cleared {cleared} old).{suffix}")
        return {"FINISHED"}


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
    bl_description = "Convert disconnected loose leaf/cap meshes into one skinned mesh using SPM leaf-node parent branches when available"

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
                spm_path=settings["spm_path"],
                true_root=settings["true_root"],
                scale_value=settings["scale_value"],
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


class STBWR_OT_PreviewJsonGroups(STBWR_OT_Base):
    bl_idname = "speedtree_bwr.preview_json_groups"
    bl_label = "Preview JSON Groups"
    bl_description = (
        "Toggle a per-vertex Color Attribute preview: each vertex is colored by "
        "the simulation group of its dominant bone (HSV families). Needs the XML"
    )

    def execute(self, context):
        settings = self.settings().as_dict()
        try:
            result = get_core().run_preview_json_groups(settings)
            if result.get("status") != "preview-restored":
                set_viewport_to_group_preview(context)
        except Exception as exc:
            return self.fail(str(exc))
        if result.get("status") == "preview-restored":
            self.report({"INFO"}, f"Preview off ({result.get('restored_count', 0)} meshes)")
            return {"FINISHED"}
        groups = result.get("simulation_group_count", 0)
        objects = result.get("objects", [])
        colored = sum(item.get("colored_vertices", 0) for item in objects)
        mode = "wind influence" if result.get("preview_mode") == "wind_influence" else "sim groups"
        self.report(
            {"INFO"},
            f"Preview on ({mode}): {groups} groups, {colored} verts across {len(objects)} mesh(es)",
        )
        return {"FINISHED"}


class STBWR_OT_WriteUnrealJson(STBWR_OT_Base):
    bl_idname = "speedtree_bwr.write_unreal_json"
    bl_label = "Write JSON Only"
    bl_description = "Write the MegaPlant tree grouping JSON from the current Blender scene without exporting FBX"

    def execute(self, context):
        settings = self.settings().as_dict()
        try:
            core = get_core()
            paths = core.default_paths(settings)
            result = core.write_unreal_json_from_scene(settings, paths)
        except Exception as exc:
            return self.fail(str(exc))
        warnings = result.get("warnings", [])
        if warnings:
            self.report({"WARNING"}, "Wrote JSON but it is hollow: " + "; ".join(warnings))
        else:
            wind = result.get("dynamic_wind_path", "")
            msg = "Wrote MegaPlant JSON: " + result["path"]
            if wind:
                dw = result.get("dynamic_wind", {})
                msg += f"  (+ dynamic wind: {dw.get('joint_count', 0)} joints, {dw.get('simulation_group_count', 0)} groups)"
            self.report({"INFO"}, msg)
        return {"FINISHED"}


# Parked 3D Branch Cluster operator.
# This operator is intentionally not registered. The direction is preserved as
# a note only because the active workflow is focused on repaired skeleton,
# skinned geometry, FBX export, and Unreal-side runtime validation.
#
# class STBWR_OT_ReplaceBranchClusters(STBWR_OT_Base):
#     bl_idname = "speedtree_bwr.replace_branch_clusters"
#     bl_label = "Place 3D Branch Clusters"
#     bl_description = "Replace branch frond cards with named 3D cluster meshes using SpeedTree XML frond names"


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
                settings=settings,
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
        box.label(text="Source (SPM -> MegaPlant)")
        box.prop(settings, "spm_path")
        st_box = box.box()
        st_box.label(text="SpeedTree Export")
        st_box.prop(settings, "speedtree_exe_path")
        st_box.prop(settings, "speedtree_fbx_export_options_path")
        st_box.prop(settings, "speedtree_xml_export_options_path")
        st_box.prop(settings, "speedtree_output_root")
        row = st_box.row(align=True)
        row.prop(settings, "speedtree_export_fbx")
        row.prop(settings, "speedtree_export_xml")

        # One-button flow: export from SpeedTree, import, and run the full repair
        # pipeline. Pressing it again wipes the previous build and re-runs (update).
        run_row = box.row()
        run_row.scale_y = 1.5
        run_row.operator("speedtree_bwr.export_from_speedtree", icon="PLAY")

        row = box.row(align=True)
        row.prop(settings, "xml_path")
        row.prop(settings, "xml_trunk_generator_regex", text="Trunk Gen")
        row = box.row(align=True)
        row.prop(settings, "armature_name")
        row.prop(settings, "true_root")
        row = box.row(align=True)
        row.prop(settings, "scale_value")
        row.prop(settings, "tolerance")
        box.prop(settings, "strict_reparent")

        man = box.box()
        man.label(text="Manual re-import / debug", icon="TOOL_SETTINGS")
        man.prop(settings, "source_fbx_path")
        man.operator("speedtree_bwr.import_source_fbx", icon="IMPORT")

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
        box.label(text="Send to Unreal Structure")
        row = box.row(align=True)
        row.prop(settings, "make_export_structure")
        row.prop(settings, "export_collection_name", text="")
        box.prop(settings, "source_collection_name")

        box = layout.box()
        box.label(text="MegaPlant JSON")
        box.prop(settings, "write_unreal_json")
        box.prop(settings, "write_dynamic_wind_json")
        if settings.write_dynamic_wind_json:
            box.prop(settings, "wind_preset")
            wind = resolve_wind_values(settings)
            box.label(
                text=(
                    f"Default: Flexibility {wind['flexibility']:.1f} · Gust {wind['gust_attenuation']:.2f}"
                    + (" · Ground Cover" if wind["ground_cover"] else "")
                ),
                icon="INFO",
            )
            box.label(text="Shared response values are edited and applied in Unreal.")
        # The JSON's real payload is per-bone simulation groups, which come from
        # the XML. No XML -> nothing for Unreal's wind import to consume.
        if not settings.xml_path:
            box.label(text="No XML: JSON groups will be hollow", icon="ERROR")

        row = box.row(align=True)
        row.operator("speedtree_bwr.preview_json_groups", icon="RESTRICT_COLOR_ON", text="Preview JSON")
        row.operator("speedtree_bwr.write_unreal_json", icon="TEXT", text="Write JSON")
        box.prop(settings, "preview_influence")
        if settings.preview_influence:
            box.label(text="Preview: blue = stiff, red = sways most", icon="INFO")
        else:
            box.label(text="Preview: distinct color per simulation group", icon="INFO")

        box.prop(settings, "show_advanced_grouping", icon="TRIA_DOWN" if settings.show_advanced_grouping else "TRIA_RIGHT", emboss=False)
        if settings.show_advanced_grouping:
            sub = box.box()
            sub.prop(settings, "json_asset_id", text="Asset ID")
            sub.prop(settings, "json_unreal_content_path")
            sub.prop(settings, "json_output_path")

        box = layout.box()
        box.label(text="Manual steps (debug)")
        box.operator("speedtree_bwr.run_full_pipeline", icon="MOD_ARMATURE")
        row = box.row(align=True)
        row.operator("speedtree_bwr.reparent_from_spm", text="Reparent")
        row.operator("speedtree_bwr.skin_loose_instances", text="Skin Loose")
        row = box.row(align=True)
        row.operator("speedtree_bwr.repair_invalid_weights", text="Weights")
        row.operator("speedtree_bwr.merge_export", text="Merge/FBX")


classes = (
    STBWR_Settings,
    STBWR_OT_ImportSourceFBX,
    STBWR_OT_ExportFromSpeedTree,
    STBWR_OT_RunFullPipeline,
    STBWR_OT_ReparentFromSPM,
    STBWR_OT_SkinLooseInstances,
    STBWR_OT_RepairInvalidWeights,
    STBWR_OT_PreviewJsonGroups,
    STBWR_OT_WriteUnrealJson,
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
