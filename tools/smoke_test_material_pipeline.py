"""Blender background smoke test for SpeedTree material consolidation/normalization.

Run with Blender:
  blender.exe --factory-startup --background --python tools/smoke_test_material_pipeline.py -- \
      --fbx X.fbx --report result.json
"""
import argparse
import json
import sys
import tempfile
import traceback
import xml.etree.ElementTree as ET
from pathlib import Path

import addon_utils
import bpy


def parse_args():
    argv = sys.argv[sys.argv.index("--") + 1 :] if "--" in sys.argv else []
    parser = argparse.ArgumentParser()
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--fbx")
    source.add_argument("--blend")
    source.add_argument("--contracts", action="store_true")
    parser.add_argument("--report", required=True)
    return parser.parse_args(argv)


TEXTURE_ROLES = ("color", "normal", "extra", "height", "opacity", "subsurface")


def touch_texture_set(folder, base):
    folder.mkdir(parents=True, exist_ok=True)
    paths = {}
    # Minimal uncompressed 1x1, 24-bit TGA that Blender can really load.
    tga_pixel = bytes((
        0, 0, 2, 0, 0, 0, 0, 0, 0, 0, 0, 0,
        1, 0, 1, 0, 24, 0, 128, 128, 128,
    ))
    for role in TEXTURE_ROLES:
        path = folder / f"{base}_{role}.tga"
        path.write_bytes(tga_pixel)
        paths[role] = path
    return paths


def write_stmat(path, materials):
    root = ET.Element("Materials", Count=str(len(materials)), Mesh=path.with_suffix(".fbx").name)
    for material_name, source_maps in materials.items():
        material = ET.SubElement(root, "Material", Name=f"{material_name}_Mat")
        for map_name, source in source_maps.items():
            ET.SubElement(
                material,
                "Map",
                Name=map_name,
                Source=str(source).replace("\\", "/"),
            )
    ET.ElementTree(root).write(path, encoding="utf-8", xml_declaration=True)


def make_mesh_object(name, materials):
    mesh = bpy.data.meshes.new(f"{name}_Mesh")
    mesh.from_pydata(
        [(0, 0, 0), (1, 0, 0), (0, 1, 0), (1, 1, 0)],
        [],
        [(0, 1, 2), (1, 3, 2)],
    )
    for material in materials:
        mesh.materials.append(material)
    for index, polygon in enumerate(mesh.polygons):
        polygon.material_index = min(index, len(materials) - 1)
    obj = bpy.data.objects.new(name, mesh)
    bpy.context.scene.collection.objects.link(obj)
    return obj


def run_contract_smoke(normalize_speedtree_material_textures, consolidate_speedtree_group_materials):
    with tempfile.TemporaryDirectory(prefix="bwr_material_contract_") as temporary:
        asset = Path(temporary) / "asset"
        fbx_dir = asset / "fbx"
        cluster_dir = asset / "cluster"
        texture_dir = asset / "texture" / "substance"
        fbx_dir.mkdir(parents=True)
        cluster_dir.mkdir(parents=True)
        source_fbx = fbx_dir / "contract.fbx"
        source_fbx.write_bytes(b"contract-smoke")

        cluster_names = {
            "Color": "leaf_cluster_01.tga",
            "Opacity": "leaf_cluster_01_Opacity.tga",
            "Normal": "leaf_cluster_01_Normal.tga",
            "Gloss": "leaf_cluster_01_Gloss.tga",
            "SubsurfaceColor": "leaf_cluster_01_Subsurface.tga",
            "SubsurfaceAmount": "leaf_cluster_01_SubsurfaceAmount.tga",
            "AO": "leaf_cluster_01_AO.tga",
            "Height": "leaf_cluster_01_Height.tga",
        }
        cluster_sources = {}
        for role, filename in cluster_names.items():
            path = cluster_dir / filename
            path.write_bytes(b"cluster-contract")
            cluster_sources[role] = path

        outside_source = asset / "texture" / "outside_cluster.tga"
        outside_source.parent.mkdir(parents=True, exist_ok=True)
        outside_source.write_bytes(b"outside-cluster-contract")
        outside_cluster_sources = dict(cluster_sources)
        outside_cluster_sources[""] = outside_source
        missing_cluster_sources = dict(cluster_sources)
        missing_cluster_sources["Normal"] = cluster_dir / "missing_original_Normal.tga"

        green = touch_texture_set(texture_dir, "T_Leaf_common_grass_01_green")
        dead = touch_texture_set(texture_dir, "T_Leaf_common_grass_01_dead")
        shared = touch_texture_set(texture_dir, "T_Leaf_Grass_atlas_01")
        stmat_maps = {
            "M_leaf_cluster_01": cluster_sources,
            "M_leaf_outside_cluster_01": outside_cluster_sources,
            "M_leaf_missing_cluster_01": missing_cluster_sources,
            "M_Leaf_common_grass_01_green": {
                "Color": green["color"],
                "Normal": green["normal"],
                "Gloss": green["extra"],
                "Height": green["height"],
                "Opacity": green["opacity"],
                "SubsurfaceColor": green["subsurface"],
            },
            "M_Leaf_common_grass_01_dead": {
                "Color": dead["color"],
                "Normal": dead["normal"],
                "Gloss": dead["extra"],
                "Height": dead["height"],
                "Opacity": dead["opacity"],
                "SubsurfaceColor": dead["subsurface"],
            },
            "M_Leaf_shared_grass_01_green": {
                "Color": shared["color"],
                "Normal": shared["normal"],
                "Gloss": shared["extra"],
                "Height": shared["height"],
                "SubsurfaceColor": shared["subsurface"],
            },
            "M_Leaf_shared_grass_01_dead": {
                "Color": shared["color"],
                "Normal": shared["normal"],
                "Gloss": shared["extra"],
                "Height": shared["height"],
                "SubsurfaceColor": shared["subsurface"],
            },
        }
        write_stmat(source_fbx.with_suffix(".stmat"), stmat_maps)

        preserved_material = bpy.data.materials.new("M_leaf_cluster_01")
        preserved_material.use_nodes = True
        sentinel_image = bpy.data.images.new("ClusterSentinel", width=1, height=1)
        sentinel_node = preserved_material.node_tree.nodes.new("ShaderNodeTexImage")
        sentinel_node.name = "ClusterSentinelNode"
        sentinel_node.image = sentinel_image
        preserved_material["codex_source_fbx"] = str(source_fbx)
        preserved_object = make_mesh_object("PreservedClusterObject", [preserved_material])
        normalization = normalize_speedtree_material_textures([preserved_object])
        if normalization.get("status") != "preserved_cluster":
            raise AssertionError(normalization)
        if normalization.get("missing"):
            raise AssertionError(normalization["missing"])
        if preserved_material.node_tree.nodes.get("ClusterSentinelNode") is None:
            raise AssertionError("preserved Cluster image node was removed")

        outside_material = bpy.data.materials.new("M_leaf_outside_cluster_01")
        outside_material.use_nodes = True
        outside_node = outside_material.node_tree.nodes.new("ShaderNodeTexImage")
        outside_node.name = "OutsideClusterSentinelNode"
        outside_node.image = bpy.data.images.new("OutsideClusterSentinel", width=1, height=1)
        outside_material["codex_source_fbx"] = str(source_fbx)
        outside_object = make_mesh_object("OutsideClusterObject", [outside_material])
        outside_result = normalize_speedtree_material_textures([outside_object])
        if outside_result.get("status") != "missing":
            raise AssertionError(outside_result)
        if outside_material.node_tree.nodes.get("OutsideClusterSentinelNode") is not None:
            raise AssertionError("outside-Cluster image node was incorrectly preserved")

        missing_material = bpy.data.materials.new("M_leaf_missing_cluster_01")
        missing_material["codex_source_fbx"] = str(source_fbx)
        missing_object = make_mesh_object("MissingClusterObject", [missing_material])
        missing_result = normalize_speedtree_material_textures([missing_object])
        if missing_result.get("status") != "missing":
            raise AssertionError(missing_result)

        shared_materials = [
            bpy.data.materials.new("M_Leaf_shared_grass_01_green"),
            bpy.data.materials.new("M_Leaf_shared_grass_01_dead"),
        ]
        for material in shared_materials:
            material["codex_source_fbx"] = str(source_fbx)
        shared_object = make_mesh_object(
            "SharedTextureSetObject", shared_materials
        )
        shared_result = normalize_speedtree_material_textures([shared_object])
        if shared_result.get("status") != "ok" or shared_result.get("missing"):
            raise AssertionError(shared_result)
        if {
            row.get("texture_base")
            for row in shared_result.get("materials", [])
        } != {"T_Leaf_Grass_atlas_01"}:
            raise AssertionError(shared_result)
        if {
            row.get("match_source")
            for row in shared_result.get("materials", [])
        } != {"stmat_reference"}:
            raise AssertionError(shared_result)

        green_material = bpy.data.materials.new("M_Leaf_common_grass_01_green")
        dead_material = bpy.data.materials.new("M_Leaf_common_grass_01_dead")
        for material in (green_material, dead_material):
            material["codex_source_fbx"] = str(source_fbx)
        variant_object = make_mesh_object(
            "VariantOnlyObject", [green_material, dead_material]
        )
        variant_only = consolidate_speedtree_group_materials([variant_object])
        variant_names = [
            material.name for material in variant_object.data.materials if material
        ]
        if set(variant_names) != {
            "M_Leaf_common_grass_01_green",
            "M_Leaf_common_grass_01_dead",
        }:
            raise AssertionError(variant_names)

        canonical = touch_texture_set(texture_dir, "T_Leaf_common_grass_01")
        canonical_object = make_mesh_object(
            "CanonicalObject", [green_material, dead_material]
        )
        canonical_result = consolidate_speedtree_group_materials([canonical_object])
        canonical_names = [
            material.name for material in canonical_object.data.materials if material
        ]
        if canonical_names != ["M_Leaf_common_grass_01"]:
            raise AssertionError(canonical_names)

        return {
            "preserved_cluster": normalization,
            "outside_cluster_rejected": outside_result,
            "missing_cluster_source_rejected": missing_result,
            "shared_stmat_texture_set": shared_result,
            "variant_only_consolidation": variant_only,
            "variant_only_materials": variant_names,
            "canonical_files": {role: str(path) for role, path in canonical.items()},
            "canonical_consolidation": canonical_result,
            "canonical_materials": canonical_names,
        }


def main():
    args = parse_args()
    source_path = Path(args.fbx or args.blend).resolve() if not args.contracts else None
    report = {
        "source": str(source_path) if source_path else "synthetic contracts",
        "status": "failed",
    }
    try:
        addon_utils.enable("speedtree_bone_weight_repair", default_set=False)
        from speedtree_bone_weight_repair.core import (
            consolidate_speedtree_group_materials,
            normalize_speedtree_material_textures,
            run_import_source_fbx,
        )

        if args.contracts:
            result = run_contract_smoke(
                normalize_speedtree_material_textures,
                consolidate_speedtree_group_materials,
            )
        elif args.blend:
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
        normalization_result = (
            result.get("texture_normalization")
            or result.get("preserved_cluster")
            or {}
        )
        missing = normalization_result.get("missing") or []
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
