"""Canonicalize Blender numeric material collisions in one saved blend."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

import bpy


REPO_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_DIR / "addons"))

from speedtree_bone_weight_repair import core


def parse_args():
    values = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
    parser = argparse.ArgumentParser()
    parser.add_argument("blend")
    return parser.parse_args(values)


def main():
    args = parse_args()
    blend = Path(args.blend).expanduser().resolve()
    bpy.ops.wm.open_mainfile(filepath=str(blend), load_ui=False)
    mesh_objects = [
        obj for obj in bpy.context.scene.objects
        if obj.type == "MESH" and obj.data
    ]
    before = sorted(
        material.name
        for material in bpy.data.materials
        if re.search(r"\.\d{3}$", material.name)
    )
    result = core._consolidate_blender_numeric_material_duplicates(
        mesh_objects
    )
    if result.get("skipped_groups"):
        raise RuntimeError(
            "Unproven numeric material collisions remain: "
            + json.dumps(
                result["skipped_groups"],
                ensure_ascii=False,
                sort_keys=True,
            )
        )
    removed_orphans = []
    for material in list(bpy.data.materials):
        if material.users == 0 and re.search(r"\.\d{3}$", material.name):
            removed_orphans.append(material.name)
            bpy.data.materials.remove(material)
    remaining = sorted(
        material.name
        for material in bpy.data.materials
        if re.search(r"\.\d{3}$", material.name)
    )
    if remaining:
        raise RuntimeError(
            "Active numeric material names remain after repair: "
            + ", ".join(remaining)
        )
    bpy.ops.wm.save_as_mainfile(filepath=str(blend))
    print(
        json.dumps(
            {
                "status": "ok",
                "blend": str(blend),
                "before": before,
                "after": remaining,
                "removed_orphans": removed_orphans,
                "normalization": result,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
