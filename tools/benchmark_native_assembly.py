"""Measure one native FBX/SPM assembly run inside Blender."""

import json
import sys
from pathlib import Path
from time import perf_counter

import bpy


REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "addons"))
from speedtree_bone_weight_repair import core


args = sys.argv[sys.argv.index("--") + 1:]
fbx_path = Path(args[0]).resolve()
spm_path = Path(args[1]).resolve()

bpy.ops.wm.read_factory_settings(use_empty=True)
started = perf_counter()
result = core.run_import_and_assemble({
    "source_fbx_path": str(fbx_path),
    "spm_path": str(spm_path),
    "armature_name": "Root",
    "source_collection_name": "SpeedTree_Source",
    "export_collection_name": "Export",
    "make_export_structure": True,
    "write_unreal_json": False,
    "export_fbx": False,
    "include_hidden": False,
    "mesh_regex": "",
    "save_intermediate_blends": False,
    "defer_pipeline_report_write": True,
})
elapsed = perf_counter() - started
print("NATIVE_ASSEMBLY_BENCHMARK=" + json.dumps({
    "elapsed_seconds": elapsed,
    "selected_armature": result["import"]["selected_armature"],
    "steps": [row["name"] for row in result["steps"]],
}, sort_keys=True))
