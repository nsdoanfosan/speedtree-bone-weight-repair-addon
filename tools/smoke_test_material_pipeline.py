"""Blender background smoke test for SpeedTree material consolidation/normalization.

Run with Blender:
  blender.exe --factory-startup --background --python tools/smoke_test_material_pipeline.py -- \
      --fbx X.fbx --report result.json
"""
import argparse
import json
import sys
import traceback
from pathlib import Path

import addon_utils
import bpy


def parse_args():
    argv = sys.argv[sys.argv.index("--") + 1 :] if "--" in sys.argv else []
    parser = argparse.ArgumentParser()
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--fbx")
    source.add_argument("--blend")
    parser.add_argument("--report", required=True)
    return parser.parse_args(argv)


def main():
    args = parse_args()
    source_path = Path(args.fbx or args.blend).resolve()
    report = {"source": str(source_path), "status": "failed"}
    try:
        addon_utils.enable("speedtree_bone_weight_repair", default_set=False)
        from speedtree_bone_weight_repair.core import (
            consolidate_speedtree_group_materials,
            normalize_speedtree_material_textures,
            run_import_source_fbx,
        )

        if args.blend:
            bpy.ops.wm.open_mainfile(filepath=str(source_path))
            consolidation = consolidate_speedtree_group_materials(bpy.context.scene.objects)
            normalization = normalize_speedtree_material_textures(bpy.context.scene.objects)
            result = {
                "material_consolidation": consolidation,
                "texture_normalization": normalization,
            }
        else:
            result = run_import_source_fbx(str(source_path), rigid_fallback=False)
        materials = sorted(
            {
                material.name
                for obj in bpy.context.scene.objects
                if obj.type == "MESH" and obj.data
                for material in obj.data.materials
                if material
            },
            key=str.casefold,
        )
        report.update(result)
        report["final_materials"] = materials
        missing = (result.get("texture_normalization") or {}).get("missing") or []
        if missing:
            raise RuntimeError(f"texture normalization still has missing rows: {missing}")
        report["status"] = "ok"
    except Exception as exc:
        report["error"] = str(exc)
        report["traceback"] = traceback.format_exc()
    path = Path(args.report)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    if report["status"] != "ok":
        raise SystemExit(1)


main()
