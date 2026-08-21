bl_info = {
    "name": "SpeedTree Assembly",
    "author": "OpenAI Codex",
    "version": (0, 4, 0),
    "blender": (5, 1, 0),
    "location": "View3D > Sidebar > SpeedTree > Assembly",
    "description": "Import native SpeedTree skeletal FBX data unchanged, assemble materials and geometry, and prepare Unreal handoff artifacts.",
    "category": "Import-Export",
}

# MegaPlant conversion roadmap
#
# Native SpeedTree export owns the skeleton hierarchy and every skin weight.
# Blender treats that data as immutable input. The add-on owns deterministic
# assembly and handoff stages:
#
# 1. Import the native FBX/XML outputs and prepare material/texture metadata.
# 2. Merge the already-skinned source meshes without altering vertex groups.
# 3. Build the Export collection hierarchy.
# 4. Emit Unreal/MegaPlant grouping data.
#    The Unreal-side process needs JSON grouping metadata so generated branch
#    and leaf geometry can be interpreted as MegaPlant-style data. This JSON
#    should be deterministic conversion metadata: provenance, generated object
#    records, native skeleton summary, binding summaries, and validation
#    results. It should not become a live Unreal wind-tuning scratchpad.
#
# 5. Validate Unreal import/runtime behavior.

import importlib
import traceback
from pathlib import Path

import bpy
from bpy.props import (
    BoolProperty,
    EnumProperty,
    FloatProperty,
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
        description="Native SpeedTree FBX to import into Blender before assembly",
        subtype="FILE_PATH",
        default="",
    )
    spm_path: StringProperty(
        name="SPM",
        description="Original SpeedTree .spm file used for export and handoff metadata",
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
        description="Export FBX from SpeedTree before Blender assembly",
        default=True,
    )
    speedtree_export_xml: BoolProperty(
        name="XML",
        description="Export SpeedTree Raw XML from SpeedTree before Blender assembly",
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
        description="Optional regular expression limiting assembly source meshes",
        default="",
    )
    include_hidden: BoolProperty(
        name="Include Hidden In Merge",
        description="Include hidden skinned mesh objects in merged export source selection",
        default=False,
    )
    export_fbx: BoolProperty(
        name="Export FBX",
        description="Export the assembled skeletal FBX without changing native bone data",
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
            "mesh_regex": self.mesh_regex,
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
    bl_label = "SpeedTree -> Import -> Assemble"
    bl_description = (
        "One button from the SPM: export FBX/XML from SpeedTree, import them, and "
        "run the assembly pipeline. Press again to update — the previous "
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

        # 2) Wipe the previous build, re-import native skin data unchanged, and
        # run material/geometry/handoff assembly.
        settings = scene_settings.as_dict()
        try:
            result = core.run_import_and_assemble(settings)
        except Exception as exc:
            return self.fail(str(exc))

        warnings = result.get("warnings", [])
        cleared = len(result.get("cleanup", {}).get("removed_objects", []))
        export_structure = result.get("export_structure", {})
        if warnings:
            self.report({"WARNING"}, f"Done (cleared {cleared} old). JSON hollow: " + "; ".join(warnings))
        elif settings.get("make_export_structure", True) and not export_structure:
            self.report({"WARNING"}, f"Assembly done, but Export structure was not built (cleared {cleared} old).")
        else:
            hierarchy = " > ".join(export_structure.get("hierarchy", []))
            suffix = f" Export: {hierarchy}" if hierarchy else ""
            self.report({"INFO"}, f"SpeedTree -> import -> assembly done (cleared {cleared} old).{suffix}")
        return {"FINISHED"}


class STBWR_OT_RunAssemblyPipeline(STBWR_OT_Base):
    bl_idname = "speedtree_bwr.run_assembly_pipeline"
    bl_label = "Run Assembly Pipeline"
    bl_description = "Assemble imported native skin data, materials, Export structure, JSON, and optional FBX"

    def execute(self, context):
        settings = self.settings().as_dict()
        if not settings["spm_path"]:
            return self.fail("SPM path is required.")
        try:
            result = get_core().run_assembly_pipeline(settings)
        except Exception as exc:
            return self.fail(str(exc))
        self.report({"INFO"}, "SpeedTree assembly pipeline done: " + result["paths"]["pipeline_report"])
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
# a note only because the active workflow is focused on native skinned geometry,
# FBX export, and Unreal-side runtime validation.
#
# class STBWR_OT_ReplaceBranchClusters(STBWR_OT_Base):
#     bl_idname = "speedtree_bwr.replace_branch_clusters"
#     bl_label = "Place 3D Branch Clusters"
#     bl_description = "Replace branch frond cards with named 3D cluster meshes using SpeedTree XML frond names"


class STBWR_OT_MergeExport(STBWR_OT_Base):
    bl_idname = "speedtree_bwr.merge_export"
    bl_label = "Merge And Export FBX"
    bl_description = "Merge native skinned meshes without changing weights and optionally export a skeletal FBX"

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
        self.report({"INFO"}, "Native-skin merge/export done.")
        return {"FINISHED"}


class STBWR_PT_Main(Panel):
    bl_idname = "STBWR_PT_main"
    bl_label = "Assembly"
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

        # One-button flow: native export/import followed by non-bone assembly.
        run_row = box.row()
        run_row.scale_y = 1.5
        run_row.operator("speedtree_bwr.export_from_speedtree", icon="PLAY")

        row = box.row(align=True)
        row.prop(settings, "xml_path")
        row.prop(settings, "xml_trunk_generator_regex", text="Trunk Gen")
        row = box.row(align=True)
        row.prop(settings, "armature_name")

        man = box.box()
        man.label(text="Manual re-import / debug", icon="TOOL_SETTINGS")
        man.prop(settings, "source_fbx_path")
        man.operator("speedtree_bwr.import_source_fbx", icon="IMPORT")

        box = layout.box()
        box.label(text="Assembly / Export")
        box.prop(settings, "mesh_regex")
        row = box.row(align=True)
        row.prop(settings, "include_hidden")
        row.prop(settings, "export_fbx")

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
        box.operator("speedtree_bwr.run_assembly_pipeline", icon="MOD_ARMATURE")
        row = box.row(align=True)
        row.operator("speedtree_bwr.import_source_fbx", text="Import")
        row.operator("speedtree_bwr.merge_export", text="Merge/FBX")


classes = (
    STBWR_Settings,
    STBWR_OT_ImportSourceFBX,
    STBWR_OT_ExportFromSpeedTree,
    STBWR_OT_RunAssemblyPipeline,
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
