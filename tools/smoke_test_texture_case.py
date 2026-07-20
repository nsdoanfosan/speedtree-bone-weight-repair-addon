"""Blender background regression check: case-variant texture base spellings.

A single differently cased role file (e.g. T_leaf_x_atlas_02_extra.tga next
to T_Leaf_x_Atlas_02_*.tga) must not split one managed texture set into two
ambiguous bases.  Mirrors the same fix in speedtree-batch-tools'
speedtree_texture_contract.py.

Run with Blender:
  blender.exe --factory-startup --background --python tools/smoke_test_texture_case.py
"""
import sys
import tempfile
import types
from pathlib import Path

import addon_utils

addon_utils.enable("speedtree_bone_weight_repair")
from speedtree_bone_weight_repair import core

failures = []


def check(name, condition, detail=""):
    if condition:
        print(f"PASS {name}")
    else:
        failures.append(name)
        print(f"FAIL {name} {detail}")


with tempfile.TemporaryDirectory() as temporary:
    root = Path(temporary)
    texture_dir = root / "texture"
    texture_dir.mkdir()
    for role in ("color", "normal", "height", "opacity", "subsurface"):
        (texture_dir / f"T_Leaf_velvet_grass_Atlas_02_{role}.tga").write_bytes(b"x")
    (texture_dir / "T_leaf_velvet_grass_atlas_02_extra.tga").write_bytes(b"x")

    indexed = core._speedtree_texture_sets(texture_dir)
    key = core._speedtree_texture_set_key("T_Leaf_velvet_grass_Atlas_02")
    match = indexed.get(key)
    check("index_has_set", match is not None)
    if match:
        check(
            "single_canonical_base",
            match["bases"] == {"T_Leaf_velvet_grass_Atlas_02"},
            f"bases={match['bases']}",
        )
        check(
            "all_six_roles",
            set(match["files"]) == set(core.SPEEDTREE_TEXTURE_ROLES),
            f"files={sorted(match['files'])}",
        )

    # STMAT referencing mixed-case spellings of the same base must count as
    # one reference and resolve to the on-disk canonical set.
    fbx_path = root / "fbx" / "SK_test.fbx"
    fbx_path.parent.mkdir()
    fbx_path.write_bytes(b"fbx")
    source_paths = [
        str(texture_dir / "T_leaf_velvet_grass_atlas_02_color.tga"),
        str(texture_dir / "T_Leaf_velvet_grass_Atlas_02_normal.tga"),
    ]
    material_key = core._speedtree_material_name_key("M_Leaf_velvet_grass_Atlas_02_Mat")
    stmat_data = {"materials": {material_key: {"source_paths": source_paths}}}
    material = types.SimpleNamespace(name="M_Leaf_velvet_grass_Atlas_02_Mat")
    resolved = core._speedtree_stmat_texture_set(str(fbx_path), material, stmat_data)
    check("stmat_set_resolved", resolved is not None, f"resolved={resolved}")
    if resolved:
        check(
            "stmat_canonical_base",
            resolved["texture_base"] == "T_Leaf_velvet_grass_Atlas_02",
            f"base={resolved['texture_base']}",
        )
        check(
            "stmat_all_roles",
            set(resolved["files"]) == set(core.SPEEDTREE_TEXTURE_ROLES),
        )

print("RESULT:", "FAILED " + ",".join(failures) if failures else "ALL_OK")
sys.exit(1 if failures else 0)
