"""Blender background smoke test for SpeedTree material consolidation/normalization.

Run with Blender:
  blender.exe --factory-startup --background --python tools/smoke_test_material_pipeline.py -- \
      --fbx X.fbx --report result.json
"""
import argparse
import gzip
import hashlib
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


def make_armature_object(name):
    armature_data = bpy.data.armatures.new(f"{name}_Data")
    armature = bpy.data.objects.new(name, armature_data)
    bpy.context.scene.collection.objects.link(armature)
    bpy.context.view_layer.objects.active = armature
    armature.select_set(True)
    bpy.ops.object.mode_set(mode="EDIT")
    bone = armature_data.edit_bones.new("Bone_1_Start")
    bone.head = (0.0, 0.0, 0.0)
    bone.tail = (0.0, 0.0, 1.0)
    bpy.ops.object.mode_set(mode="OBJECT")
    armature.select_set(False)
    return armature


def run_contract_smoke(
    normalize_speedtree_material_textures,
    consolidate_speedtree_group_materials,
    speedtree_manifest_binding,
    speedtree_manifest_texture_binding,
    apply_speedtree_material_intents,
    handoff_contract,
    load_speedtree_texture_readiness_contract,
    inspect_spm_unreal_instance_profile,
    apply_spm_unreal_instance_profile,
    merge_skinned_meshes,
    clear_previous_codex_build,
):
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
        (
            cluster_dir / "leaf_cluster_01_auto_capture_manifest.json"
        ).write_text(
            json.dumps({
                "kind": "speedtree_cluster_blender_auto_capture",
                "version": 2,
                "workflow_mode": "PHYSICAL_DIRECT_CAPTURE",
                "direct_uv_source":
                    "same_blender_physical_capture_projection",
                "material_id": "2",
                "material_name": "M_leaf_cluster_01",
                "physical_capture_contract_sha256": "c" * 64,
                "maps": [
                    {
                        "role": role,
                        "path": str(path),
                        "sha256": hashlib.sha256(
                            path.read_bytes()
                        ).hexdigest(),
                    }
                    for role, path in cluster_sources.items()
                ],
            }),
            encoding="utf-8",
        )

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
            "M_Material": {},
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
        strict_preserve_contract = {
            "status": "ok",
            "strict_speedtree_pipeline_contract": True,
            "speedtree_pipeline_contract": {"material_intents": []},
            "bindings": [
                {
                    "material": preserved_material.name,
                    "material_key": "mleafcluster01",
                    "production_group_base": preserved_material.name,
                    "status": "not_managed",
                    "texture_source_mode": "preserve_declared_sources",
                    "files": {},
                    "missing_roles": [],
                }
            ],
        }
        strict_preserved_cluster = normalize_speedtree_material_textures(
            [preserved_object], texture_contract=strict_preserve_contract
        )
        if strict_preserved_cluster.get("status") != "preserved_cluster":
            raise AssertionError(strict_preserved_cluster)
        if preserved_material.node_tree.nodes.get("ClusterSentinelNode") is None:
            raise AssertionError("strict contract removed a preserved Cluster node")

        source_free_material = bpy.data.materials.new("M_Material")
        source_free_material["codex_source_fbx"] = str(source_fbx)
        source_free_object = make_mesh_object(
            "SourceFreeDeclaredObject", [source_free_material]
        )
        source_free_contract = {
            "status": "ok",
            "strict_speedtree_pipeline_contract": True,
            "speedtree_pipeline_contract": {"material_intents": []},
            "bindings": [{
                "material": source_free_material.name,
                "material_key": "mmaterial",
                "production_group_base": source_free_material.name,
                "status": "not_managed",
                "texture_source_mode": "preserve_declared_sources",
                "files": {},
                "missing_roles": [],
            }],
        }
        source_free_result = normalize_speedtree_material_textures(
            [source_free_object], texture_contract=source_free_contract
        )
        if source_free_result.get("status") != "ok":
            raise AssertionError(source_free_result)
        source_free_rows = source_free_result.get("materials") or []
        if (
            not source_free_rows
            or source_free_rows[0].get("status")
            != "preserved_declared_sources"
            or source_free_rows[0].get("source_free") is not True
        ):
            raise AssertionError(source_free_result)

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
        shared_contract = {
            "status": "ok",
            "bindings": [
                {
                    "material": f"{material.name}_Mat",
                    "status": "ok",
                    "texture_base": "T_Leaf_Grass_atlas_01",
                    "texture_dir": str(texture_dir),
                    "stmat_roles": ["color", "normal", "extra", "height", "subsurface"],
                    "files": {role: str(path) for role, path in shared.items()},
                    "missing_roles": [],
                }
                for material in shared_materials
            ],
        }
        shared_contract_result = normalize_speedtree_material_textures(
            [shared_object], texture_contract=shared_contract
        )
        if shared_contract_result.get("status") != "ok":
            raise AssertionError(shared_contract_result)
        if {
            row.get("match_source")
            for row in shared_contract_result.get("materials", [])
        } != {"shared_texture_contract"}:
            raise AssertionError(shared_contract_result)
        strict_intents = []
        shared_files = {role: str(path) for role, path in shared.items()}
        for index, material in enumerate(shared_materials):
            intent = handoff_contract.central_contract_api().build_material_intent(
                material.name
            )
            intent.update(
                {
                    "stmat_material_index": index,
                    "stmat_material_id": str(index + 1),
                    "material_name": material.name,
                    "texture_source_mode": "managed_texture_set",
                    "texture_binding": {
                        "status": "ok",
                        "set_key": "leafgrassatlas01",
                        "texture_base": "T_Leaf_Grass_atlas_01",
                        "texture_dir": str(texture_dir),
                        "files": shared_files,
                        "missing_roles": [],
                    },
                }
            )
            strict_intents.append(intent)
        strict_envelope = {"material_intents": strict_intents}
        strict_contract = {
            "status": "ok",
            "strict_speedtree_pipeline_contract": True,
            "speedtree_pipeline_contract": strict_envelope,
            "bindings": handoff_contract.texture_bindings_from_envelope(
                strict_envelope
            ),
        }
        strict_intent_result = apply_speedtree_material_intents(
            [shared_object], strict_contract
        )
        strict_shared_result = normalize_speedtree_material_textures(
            [shared_object], texture_contract=strict_contract
        )
        if strict_shared_result.get("status") != "ok" or {
            row.get("match_source")
            for row in strict_shared_result.get("materials", [])
        } != {"speedtree_material_intent"}:
            raise AssertionError(strict_shared_result)
        if any(
            material.get("unreal_tree_part") != "leaf"
            or material.get("unreal_tree_shading") != "foliage"
            for material in shared_materials
        ):
            raise AssertionError(strict_intent_result)

        collision_source = fbx_dir / "collision_source.fbx"
        collision_source.write_bytes(b"collision-source")
        collision_material = bpy.data.materials.new(
            "M_Bark_numeric_collision_01"
        )
        collision_duplicate = bpy.data.materials.new(
            "M_Bark_numeric_collision_01"
        )
        collision_material["codex_source_fbx"] = str(source_fbx)
        collision_duplicate["codex_source_fbx"] = str(collision_source)
        collision_object = make_mesh_object(
            "NumericCollisionObject",
            [collision_material, collision_duplicate],
        )
        collision_contract = {
            "status": "ok",
            "strict_speedtree_pipeline_contract": True,
            "bindings": [
                {
                    "material": collision_material.name,
                    "texture_source_mode": "managed_texture_set",
                    "status": "ok",
                    "set_key": "barknumericcollision01",
                    "texture_base": "T_Bark_numeric_collision_01",
                    "files": shared_files,
                    "missing_roles": [],
                }
            ],
        }
        collision_result = consolidate_speedtree_group_materials(
            [collision_object], texture_contract=collision_contract
        )
        if list(collision_object.data.materials) != [collision_material]:
            raise AssertionError(collision_result)
        collision_groups = [
            group
            for group in collision_result.get("groups", [])
            if group.get("mode") == "blender_numeric_collision"
        ]
        if (
            len(collision_groups) != 1
            or collision_groups[0].get("target_material")
            != collision_material.name
            or collision_groups[0].get("proofs")
            != ["strict_texture_contract"]
        ):
            raise AssertionError(collision_result)

        distinct_collision_material = bpy.data.materials.new(
            "M_Bark_distinct_collision_01"
        )
        distinct_collision_duplicate = bpy.data.materials.new(
            "M_Bark_distinct_collision_01"
        )
        distinct_collision_material["codex_source_fbx"] = str(source_fbx)
        distinct_collision_duplicate["codex_source_fbx"] = str(
            collision_source
        )
        distinct_collision_object = make_mesh_object(
            "DistinctNumericCollisionObject",
            [
                distinct_collision_material,
                distinct_collision_duplicate,
            ],
        )
        distinct_collision_contract = {
            "status": "ok",
            "strict_speedtree_pipeline_contract": True,
            "bindings": [
                {
                    "material": distinct_collision_material.name,
                    "texture_source_mode": "managed_texture_set",
                    "status": "ok",
                    "set_key": "barkdistinctcollision01a",
                    "texture_base": "T_Bark_distinct_collision_01_A",
                    "files": shared_files,
                    "missing_roles": [],
                },
                {
                    "material": distinct_collision_duplicate.name,
                    "texture_source_mode": "managed_texture_set",
                    "status": "ok",
                    "set_key": "barkdistinctcollision01b",
                    "texture_base": "T_Bark_distinct_collision_01_B",
                    "files": {
                        role: str(path) for role, path in green.items()
                    },
                    "missing_roles": [],
                },
            ],
        }
        distinct_collision_result = consolidate_speedtree_group_materials(
            [distinct_collision_object],
            texture_contract=distinct_collision_contract,
        )
        if (
            len(distinct_collision_object.data.materials) != 2
            or not distinct_collision_result.get("skipped_groups")
        ):
            raise AssertionError(distinct_collision_result)

        green_material = bpy.data.materials.new("M_Leaf_common_grass_01_green")
        dead_material = bpy.data.materials.new("M_Leaf_common_grass_01_dead")
        for material in (green_material, dead_material):
            material["codex_source_fbx"] = str(source_fbx)
        try:
            normalize_speedtree_material_textures(
                [make_mesh_object("StrictNoFallbackObject", [green_material])],
                texture_contract={
                    "status": "ok",
                    "strict_speedtree_pipeline_contract": True,
                    "speedtree_pipeline_contract": {"material_intents": []},
                    "bindings": [],
                },
            )
        except RuntimeError as exc:
            if "texture binding is missing" not in str(exc):
                raise
            strict_no_fallback = str(exc)
        else:
            raise AssertionError("strict contract used a material-name fallback")
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

        arbitrary_materials = [
            bpy.data.materials.new("M_Leaf_arbitrary_grass_01_fresh_custom"),
            bpy.data.materials.new("M_Leaf_arbitrary_grass_01_winter_dry"),
        ]
        for material in arbitrary_materials:
            material["codex_source_fbx"] = str(source_fbx)
        arbitrary_intents = []
        for index, material in enumerate(arbitrary_materials):
            intent = handoff_contract.central_contract_api().build_material_intent(
                material.name
            )
            intent.update(
                {
                    "stmat_material_index": index,
                    "stmat_material_id": str(index + 20),
                    "material_name": material.name,
                    "texture_source_mode": "managed_texture_set",
                    "texture_binding": {
                        "status": "ok",
                        "set_key": "leafgrassatlas01",
                        "texture_base": "T_Leaf_Grass_atlas_01",
                        "texture_dir": str(texture_dir),
                        "files": shared_files,
                        "missing_roles": [],
                    },
                }
            )
            arbitrary_intents.append(intent)
        arbitrary_envelope = {"material_intents": arbitrary_intents}
        arbitrary_contract = {
            "status": "ok",
            "strict_speedtree_pipeline_contract": True,
            "speedtree_pipeline_contract": arbitrary_envelope,
            "bindings": handoff_contract.texture_bindings_from_envelope(
                arbitrary_envelope
            ),
        }
        arbitrary_object = make_mesh_object(
            "ArbitraryCollectionSuffixObject", arbitrary_materials
        )
        arbitrary_result = consolidate_speedtree_group_materials(
            [arbitrary_object], texture_contract=arbitrary_contract
        )
        arbitrary_names = [
            material.name for material in arbitrary_object.data.materials if material
        ]
        if arbitrary_names != ["M_Leaf_arbitrary_grass_01"]:
            raise AssertionError((arbitrary_names, arbitrary_result))
        arbitrary_normalization = normalize_speedtree_material_textures(
            [arbitrary_object], texture_contract=arbitrary_contract
        )
        if arbitrary_normalization.get("status") != "ok":
            raise AssertionError(arbitrary_normalization)

        distinct_materials = [
            bpy.data.materials.new("M_Leaf_distinct_grass_01_wet_custom"),
            bpy.data.materials.new("M_Leaf_distinct_grass_01_dry_custom"),
        ]
        for material in distinct_materials:
            material["codex_source_fbx"] = str(source_fbx)
        distinct_bindings = []
        for material, files in zip(distinct_materials, (shared_files, green)):
            distinct_bindings.append(
                {
                    "material": material.name,
                    "production_group_base": "M_Leaf_distinct_grass_01",
                    "texture_source_mode": "managed_texture_set",
                    "status": "ok",
                    "set_key": material.name,
                    "texture_base": material.name.replace("M_", "T_", 1),
                    "files": {role: str(path) for role, path in files.items()},
                    "missing_roles": [],
                }
            )
        distinct_contract = {
            "status": "ok",
            "strict_speedtree_pipeline_contract": True,
            "speedtree_pipeline_contract": {"material_intents": []},
            "bindings": distinct_bindings,
        }
        distinct_object = make_mesh_object(
            "DistinctSourceSignatureObject", distinct_materials
        )
        distinct_result = consolidate_speedtree_group_materials(
            [distinct_object], texture_contract=distinct_contract
        )
        if len(distinct_object.data.materials) != 2 or not (
            distinct_result.get("skipped_groups")
        ):
            raise AssertionError(distinct_result)

        isolated_objects = []
        isolated_sources = []
        for source_index in (1, 2):
            isolated_asset = asset / f"isolated_{source_index}"
            isolated_fbx = isolated_asset / "fbx" / "source.fbx"
            isolated_fbx.parent.mkdir(parents=True)
            isolated_fbx.write_bytes(f"source-{source_index}".encode("ascii"))
            touch_texture_set(
                isolated_asset / "texture" / "substance",
                "T_Leaf_source_guard_01",
            )
            isolated_materials = [
                bpy.data.materials.new("M_Leaf_source_guard_01_fresh"),
                bpy.data.materials.new("M_Leaf_source_guard_01_winter"),
            ]
            for material in isolated_materials:
                material["codex_source_fbx"] = str(isolated_fbx)
            isolated_objects.append(
                make_mesh_object(
                    f"SourceIsolationObject{source_index}", isolated_materials
                )
            )
            isolated_sources.append(str(isolated_fbx.resolve()).casefold())
        isolated_result = consolidate_speedtree_group_materials(isolated_objects)
        isolated_targets = [
            obj.data.materials[0] for obj in isolated_objects
        ]
        if any(len(obj.data.materials) != 1 for obj in isolated_objects):
            raise AssertionError(isolated_result)
        if isolated_targets[0] == isolated_targets[1]:
            raise AssertionError("different source FBXs shared one consolidated material")
        if {
            str(material.get("codex_source_fbx", "")).casefold()
            for material in isolated_targets
        } != set(isolated_sources):
            raise AssertionError(isolated_result)

        manifest_material = bpy.data.materials.new(
            "M_Leaf_manifest_grass_01_winter_dry"
        )
        manifest_material["codex_source_fbx"] = str(source_fbx)
        manifest_path = asset / "speedtree_import_manifest.json"
        manifest_path.write_text(
            json.dumps(
                {
                    "source_collection": "Atlas_Leaf_Meshes",
                    "blend_file": str(asset / "M_Leaf_manifest_grass_01.blend"),
                    "material": None,
                    "material_groups": [
                        {
                            "collection": "Fresh Custom",
                            "material": "M_Leaf_manifest_grass_01_fresh_custom",
                        },
                        {
                            "collection": "Winter Dry",
                            "material": "M_Leaf_manifest_grass_01_winter_dry",
                        },
                    ],
                }
            ),
            encoding="utf-8",
        )
        manifest_stmat = {
            "materials": {
                "mleafmanifestgrass01winterdry": {
                    "user_data": {},
                    "source_paths": [],
                }
            }
        }
        manifest_binding = speedtree_manifest_binding(
            source_fbx, manifest_material, stmat_data=manifest_stmat
        )
        if not manifest_binding or manifest_binding.get("target_name") != (
            "M_Leaf_manifest_grass_01"
        ):
            raise AssertionError(manifest_binding)

        canonical_asset = asset / "manifest_canonical"
        canonical_fbx = canonical_asset / "fbx" / "canonical.fbx"
        canonical_fbx.parent.mkdir(parents=True)
        canonical_fbx.write_bytes(b"canonical-fbx")
        canonical_texture_root = canonical_asset / "texture"
        manifest_canonical_files = touch_texture_set(
            canonical_texture_root, "T_Leaf_manifest_canonical_01"
        )
        canonical_manifest = canonical_asset / "speedtree_import_manifest.json"
        canonical_manifest.write_text(
            json.dumps(
                {
                    "atlas_asset_name": "M_Leaf_manifest_canonical_01",
                    "texture_contract_status": "canonical_pcg_output",
                    "canonical_texture_outputs": [
                        {
                            "texture_contract_status": "canonical_pcg_output",
                            "material_name": "M_Leaf_manifest_canonical_01",
                            "target_spm": str(
                                canonical_asset / "canonical.spm"
                            ),
                            "texture_root": str(canonical_texture_root),
                            "texture_base": "T_Leaf_manifest_canonical_01",
                            "files": {
                                role: str(path)
                                for role, path in manifest_canonical_files.items()
                            },
                        }
                    ],
                    "material_groups": [
                        {
                            "material": "M_Leaf_manifest_canonical_01",
                            "texture_contract_status": "canonical_pcg_output",
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        canonical_material = bpy.data.materials.new(
            "M_Leaf_manifest_canonical_01"
        )
        canonical_material["codex_source_fbx"] = str(canonical_fbx)
        canonical_object = make_mesh_object(
            "ManifestCanonicalObject", [canonical_material]
        )
        canonical_manifest_binding = speedtree_manifest_texture_binding(
            canonical_fbx, canonical_material
        )
        canonical_manifest_result = normalize_speedtree_material_textures(
            [canonical_object]
        )
        if (
            canonical_manifest_binding.get("texture_contract_status")
            != "canonical_pcg_output"
            or canonical_manifest_result.get("status") != "ok"
            or canonical_manifest_result["materials"][0].get("match_source")
            != "atlas_import_manifest"
        ):
            raise AssertionError(
                (canonical_manifest_binding, canonical_manifest_result)
            )
        strict_canonical_manifest_result = (
            normalize_speedtree_material_textures(
                [canonical_object],
                texture_contract={
                    "status": "ok",
                    "strict_speedtree_pipeline_contract": True,
                    "bindings": [{
                        "material": canonical_material.name,
                        "status": "not_managed",
                        "texture_source_mode": (
                            "preserve_declared_sources"
                        ),
                    }],
                },
            )
        )
        if (
            strict_canonical_manifest_result.get("status") != "ok"
            or strict_canonical_manifest_result["materials"][0].get(
                "match_source"
            )
            != "atlas_import_manifest"
        ):
            raise AssertionError(strict_canonical_manifest_result)

        scoped_payload = json.loads(
            canonical_manifest.read_text(encoding="utf-8")
        )
        scoped_payload["export_scope_id"] = "atlas-scope-owned-by-leaf"
        canonical_manifest.write_text(
            json.dumps(scoped_payload), encoding="utf-8"
        )
        scoped_manifest_dir = (
            canonical_asset / ".atlas_leaf_speedtree_scopes"
        )
        scoped_manifest_dir.mkdir()
        consumer_scoped_manifest = (
            scoped_manifest_dir
            / "atlas-scope-owned-by-leaf__canonical.json"
        )
        consumer_scoped_manifest.write_text(
            json.dumps(scoped_payload), encoding="utf-8"
        )
        consumer_scoped_binding = speedtree_manifest_texture_binding(
            canonical_fbx,
            canonical_material,
            stmat_data={
                "materials": {
                    "mleafmanifestcanonical01": {
                        "user_data": {},
                        "source_paths": [],
                    }
                }
            },
        )
        if (
            consumer_scoped_binding.get("texture_contract_status")
            != "canonical_pcg_output"
            or Path(
                consumer_scoped_binding.get("manifest_path", "")
            ).resolve()
            != consumer_scoped_manifest.resolve()
        ):
            raise AssertionError(consumer_scoped_binding)
        unrelated_material = bpy.data.materials.new(
            "M_bark_unrelated_to_atlas"
        )
        unrelated_material["codex_source_fbx"] = str(canonical_fbx)
        unrelated_stmat = {
            "materials": {
                "mbarkunrelatedtoatlas": {
                    "user_data": {},
                    "source_paths": [],
                }
            }
        }
        scoped_unrelated_texture_binding = (
            speedtree_manifest_texture_binding(
                canonical_fbx,
                unrelated_material,
                stmat_data=unrelated_stmat,
            )
        )
        scoped_unrelated_group_binding = speedtree_manifest_binding(
            canonical_fbx,
            unrelated_material,
            stmat_data=unrelated_stmat,
        )
        if (
            scoped_unrelated_texture_binding is not None
            or scoped_unrelated_group_binding is not None
        ):
            raise AssertionError(
                (
                    scoped_unrelated_texture_binding,
                    scoped_unrelated_group_binding,
                )
            )

        provisional_asset = asset / "manifest_provisional"
        provisional_fbx = provisional_asset / "fbx" / "provisional.fbx"
        provisional_fbx.parent.mkdir(parents=True)
        provisional_fbx.write_bytes(b"provisional-fbx")
        original_root = asset / "original_atlas_sources"
        original_root.mkdir()
        sample_tga = next(iter(manifest_canonical_files.values())).read_bytes()
        original_sources = {
            "albedo": original_root / "leaf_albedo.tga",
            "alpha": original_root / "leaf_opacity.tga",
            "normal": original_root / "leaf_normal.tga",
        }
        for path in original_sources.values():
            path.write_bytes(sample_tga)
        promoted_texture_root = provisional_asset / "texture"
        expected_t_paths = {
            role: str(
                promoted_texture_root
                / f"T_Leaf_manifest_provisional_01_{role}.tga"
            )
            for role in TEXTURE_ROLES
        }
        provisional_manifest = (
            provisional_asset / "speedtree_import_manifest.json"
        )
        provisional_payload = {
            "atlas_asset_name": "M_Leaf_manifest_provisional_01",
            "texture_contract_status": (
                "source_fallback_needs_pcg_generation"
            ),
            "source_texture_fallbacks": [
                {
                    "texture_contract_status": (
                        "source_fallback_needs_pcg_generation"
                    ),
                    "material": "M_Leaf_manifest_provisional_01",
                    "source_origin": "atlas_mesh_build_source",
                    "source_paths": {
                        role: str(path)
                        for role, path in original_sources.items()
                    },
                    "source_roles": sorted(original_sources),
                    "expected_t_paths": expected_t_paths,
                    "expected_texture_base": (
                        "T_Leaf_manifest_provisional_01"
                    ),
                    "remediation": "run PCG ST9 Texture",
                    "warning": "canonical T_* is not generated yet",
                    "provisional_receipt": {
                        "kind": "speedtree_texture_provisional_receipt",
                        "version": 1,
                        "status": (
                            "source_fallback_needs_pcg_generation"
                        ),
                        "source_origin": "atlas_mesh_build_source",
                        "material": "M_Leaf_manifest_provisional_01",
                        "target_spm": str(
                            provisional_asset / "provisional.spm"
                        ),
                        "source_roles": sorted(original_sources),
                        "warning": "canonical T_* is not generated yet",
                        "remediation": "run PCG ST9 Texture",
                        "canonical_promotion_required": True,
                    },
                }
            ],
            "material_groups": [
                {
                    "material": "M_Leaf_manifest_provisional_01",
                    "texture_contract_status": (
                        "source_fallback_needs_pcg_generation"
                    ),
                }
            ],
        }
        provisional_manifest.write_text(
            json.dumps(provisional_payload), encoding="utf-8"
        )
        provisional_material = bpy.data.materials.new(
            "M_Leaf_manifest_provisional_01"
        )
        provisional_material["codex_source_fbx"] = str(provisional_fbx)
        provisional_object = make_mesh_object(
            "ManifestProvisionalObject", [provisional_material]
        )
        provisional_result = normalize_speedtree_material_textures(
            [provisional_object]
        )
        provisional_signature = {
            str(path.resolve()).casefold()
            for path in original_sources.values()
        }
        provisional_node_signature = {
            str(
                Path(
                    bpy.path.abspath(
                        node.image.filepath_raw or node.image.filepath
                    )
                ).resolve()
            ).casefold()
            for node in provisional_material.node_tree.nodes
            if node.type == "TEX_IMAGE" and node.image
        }
        if (
            provisional_result.get("status")
            != "needs_pcg_generation"
            or provisional_result.get("needs_pcg_generation_count") != 1
            or provisional_node_signature != provisional_signature
        ):
            raise AssertionError(provisional_result)
        strict_provisional_manifest_result = (
            normalize_speedtree_material_textures(
                [provisional_object],
                texture_contract={
                    "status": "ok",
                    "strict_speedtree_pipeline_contract": True,
                    "bindings": [{
                        "material": provisional_material.name,
                        "status": "not_managed",
                        "texture_source_mode": (
                            "preserve_declared_sources"
                        ),
                    }],
                },
            )
        )
        if (
            strict_provisional_manifest_result.get("status")
            != "needs_pcg_generation"
            or strict_provisional_manifest_result.get(
                "needs_pcg_generation_count"
            )
            != 1
        ):
            raise AssertionError(strict_provisional_manifest_result)

        promoted_files = touch_texture_set(
            promoted_texture_root, "T_Leaf_manifest_provisional_01"
        )
        provisional_payload["texture_contract_status"] = (
            "canonical_pcg_output"
        )
        provisional_payload.pop("source_texture_fallbacks")
        provisional_payload["canonical_texture_outputs"] = [
            {
                "texture_contract_status": "canonical_pcg_output",
                "material_name": "M_Leaf_manifest_provisional_01",
                "target_spm": str(
                    provisional_asset / "provisional.spm"
                ),
                "texture_root": str(promoted_texture_root),
                "texture_base": "T_Leaf_manifest_provisional_01",
                "files": {
                    role: str(path)
                    for role, path in promoted_files.items()
                },
            }
        ]
        provisional_payload["material_groups"][0][
            "texture_contract_status"
        ] = "canonical_pcg_output"
        provisional_manifest.write_text(
            json.dumps(provisional_payload), encoding="utf-8"
        )
        promoted_result = normalize_speedtree_material_textures(
            [provisional_object]
        )
        promoted_signature = {
            str(path.resolve()).casefold() for path in promoted_files.values()
        }
        promoted_node_signature = {
            str(
                Path(
                    bpy.path.abspath(
                        node.image.filepath_raw or node.image.filepath
                    )
                ).resolve()
            ).casefold()
            for node in provisional_material.node_tree.nodes
            if node.type == "TEX_IMAGE" and node.image
        }
        if (
            promoted_result.get("status") != "ok"
            or promoted_node_signature != promoted_signature
        ):
            raise AssertionError((promoted_result, promoted_signature))

        malformed_asset = asset / "manifest_malformed"
        malformed_fbx = malformed_asset / "fbx" / "malformed.fbx"
        malformed_fbx.parent.mkdir(parents=True)
        malformed_fbx.write_bytes(b"malformed-fbx")
        (malformed_asset / "speedtree_import_manifest.json").write_text(
            "{ malformed json",
            encoding="utf-8",
        )
        malformed_material = bpy.data.materials.new(
            "M_Leaf_manifest_malformed_01"
        )
        malformed_material["codex_source_fbx"] = str(malformed_fbx)
        malformed_material.use_nodes = True
        malformed_sentinel = malformed_material.node_tree.nodes.new(
            "ShaderNodeTexImage"
        )
        malformed_sentinel.name = "MalformedManifestSentinel"
        malformed_sentinel.image = bpy.data.images.new(
            "MalformedManifestSentinelImage", width=1, height=1
        )
        malformed_object = make_mesh_object(
            "ManifestMalformedObject", [malformed_material]
        )
        malformed_manifest_blocked = False
        try:
            normalize_speedtree_material_textures([malformed_object])
        except RuntimeError as exc:
            malformed_manifest_blocked = "malformed" in str(exc).casefold()
        if (
            not malformed_manifest_blocked
            or malformed_material.node_tree.nodes.get(
                "MalformedManifestSentinel"
            )
            is None
        ):
            raise AssertionError(
                "malformed authoritative manifest did not fail before "
                "node mutation"
            )

        invalid_asset = asset / "manifest_invalid"
        invalid_fbx = invalid_asset / "fbx" / "invalid.fbx"
        invalid_fbx.parent.mkdir(parents=True)
        invalid_fbx.write_bytes(b"invalid-fbx")
        copied_root = invalid_asset / "cache"
        copied_root.mkdir()
        copied_albedo = copied_root / "copied_albedo.png"
        copied_albedo.write_bytes(sample_tga)
        (invalid_asset / "speedtree_import_manifest.json").write_text(
            json.dumps(
                {
                    "texture_contract_status": (
                        "source_fallback_needs_pcg_generation"
                    ),
                    "source_texture_fallbacks": [
                        {
                            "material": "M_Leaf_manifest_invalid_01",
                            "source_paths": {
                                "albedo": str(copied_albedo)
                            },
                            "expected_t_paths": {},
                            "remediation": "run PCG ST9 Texture",
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        invalid_material = bpy.data.materials.new(
            "M_Leaf_manifest_invalid_01"
        )
        invalid_material["codex_source_fbx"] = str(invalid_fbx)
        invalid_material.use_nodes = True
        invalid_sentinel = invalid_material.node_tree.nodes.new(
            "ShaderNodeTexImage"
        )
        invalid_sentinel.name = "InvalidManifestSentinel"
        invalid_sentinel.image = bpy.data.images.new(
            "InvalidManifestSentinelImage", width=1, height=1
        )
        invalid_object = make_mesh_object(
            "ManifestInvalidObject", [invalid_material]
        )
        invalid_manifest_blocked = False
        try:
            normalize_speedtree_material_textures([invalid_object])
        except RuntimeError as exc:
            invalid_manifest_blocked = "never fall back" in str(exc)
        if (
            not invalid_manifest_blocked
            or invalid_material.node_tree.nodes.get(
                "InvalidManifestSentinel"
            )
            is None
        ):
            raise AssertionError(
                "invalid Atlas manifest did not fail before node mutation"
            )

        profile_materials = [
            bpy.data.materials.new("M_Profile_stem_01"),
            bpy.data.materials.new("M_Profile_leaf_01"),
        ]
        profile_object = make_mesh_object("ProfileObject", profile_materials)
        name_only_materials = [
            bpy.data.materials.new("M_Leaf_contract_green"),
            bpy.data.materials.new("M_Leaf_contract_yellow"),
            bpy.data.materials.new("M_Stem_contract_dead"),
        ]
        name_only_object = make_mesh_object(
            "NameOnlyProfileObject", name_only_materials
        )
        stale_profile_material = bpy.data.materials.new("M_Stale_Profile")
        stale_profile_material["unreal_instance_profile"] = "dead"
        stale_profile_object = make_mesh_object(
            "StaleProfileObject", [stale_profile_material]
        )
        spm_path = asset / "profile_contract.spm"

        def write_profile_spm(value, compressed=False):
            root = ET.Element("SpeedTree")
            generator = ET.SubElement(root, "Generator", Type="Tree")
            prop = ET.SubElement(generator, "Property")
            ET.SubElement(prop, "Name").text = "SpeedTree SDK:User data"
            ET.SubElement(prop, "Value").text = value
            payload = ET.tostring(
                root, encoding="utf-8", xml_declaration=True
            )
            if compressed:
                with gzip.open(spm_path, "wb") as handle:
                    handle.write(payload)
            else:
                spm_path.write_bytes(payload)

        write_profile_spm("Dead", compressed=True)
        profile_inspection = inspect_spm_unreal_instance_profile(spm_path)
        if profile_inspection.get("profile") != "dead":
            raise AssertionError(profile_inspection)

        def source_identity(path):
            path = Path(path).resolve()
            stat = path.stat()
            return {
                "canonical_path": str(path),
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                "size": stat.st_size,
                "mtime_ns": stat.st_mtime_ns,
            }

        source_identity_payload = {
            "spm": source_identity(spm_path),
            "stmat": [source_identity(source_fbx.with_suffix(".stmat"))],
        }

        def strict_profile_envelope(profile):
            api = handoff_contract.central_contract_api()
            intents = []
            for index, material in enumerate(profile_materials):
                intent = api.build_material_intent(
                    material.name, instance_profile=profile
                )
                intent.update(
                    {
                        "stmat_material_index": index,
                        "stmat_material_id": str(index + 1),
                        "material_name": material.name,
                        "texture_source_mode": "managed_texture_set",
                        "texture_binding": {
                            "status": "ok",
                            "set_key": "leafgrassatlas01",
                            "texture_base": "T_Leaf_Grass_atlas_01",
                            "texture_dir": str(texture_dir),
                            "files": shared_files,
                            "missing_roles": [],
                        },
                    }
                )
                intents.append(intent)
            return {
                "kind": "speedtree_material_preflight",
                "schema_version": 1,
                "speedtree_handoff_contract": api.build_sidecar_descriptor(
                    spm_path.stem, source=source_identity_payload
                ),
                "outcome": "ok",
                "source": source_identity_payload,
                "source_fingerprint": handoff_contract.source_fingerprint(
                    source_identity_payload
                ),
                "instance_profile": profile,
                "tree_user_data": {
                    "property": "SpeedTree SDK:User data",
                    "raw": profile,
                    "normalized": profile,
                    "status": "ok" if profile else "empty",
                },
                "material_intents": intents,
                "dynamic_wind": {},
                "issues": [],
            }

        strict_report_path = asset / "strict_material_preflight.json"
        strict_report_path.write_text(
            json.dumps(
                {
                    "status": "ok",
                    "speedtree_pipeline_contract": strict_profile_envelope(
                        "dead"
                    ),
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        loaded_strict_contract = load_speedtree_texture_readiness_contract(
            strict_report_path,
            spm_path=spm_path,
            source_fbx_path=source_fbx,
        )
        if not loaded_strict_contract.get("strict_speedtree_pipeline_contract"):
            raise AssertionError(loaded_strict_contract)

        mismatch_report_path = asset / "mismatch_material_preflight.json"
        mismatch_report_path.write_text(
            json.dumps(
                {
                    "status": "ok",
                    "speedtree_pipeline_contract": strict_profile_envelope(""),
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        try:
            load_speedtree_texture_readiness_contract(
                mismatch_report_path,
                spm_path=spm_path,
                source_fbx_path=source_fbx,
            )
        except RuntimeError as exc:
            if "instance_profile mismatch" not in str(exc):
                raise
            profile_mismatch_blocked = str(exc)
        else:
            raise AssertionError("profile mismatch contract was not blocked")
        profile_result = apply_spm_unreal_instance_profile(
            [profile_object], spm_path
        )
        if {
            material.get("unreal_instance_profile")
            for material in profile_materials
        } != {"dead"}:
            raise AssertionError(profile_result)
        consolidated_profile_result = apply_spm_unreal_instance_profile(
            [canonical_object], spm_path
        )
        if len(canonical_object.data.materials) != 1 or (
            canonical_object.data.materials[0].get("unreal_instance_profile")
            != "dead"
        ):
            raise AssertionError(consolidated_profile_result)

        merge_armature = make_armature_object("ProfileMergeArmature")
        source_profile_materials = {
            material.as_pointer()
            for obj in (profile_object, canonical_object)
            for material in obj.data.materials
            if material
        }
        merged_profile_object, *_merge_result = merge_skinned_meshes(
            merge_armature,
            [profile_object, canonical_object],
            "ProfileMergedObject",
        )
        merged_profile_materials = [
            material
            for material in merged_profile_object.data.materials
            if material
        ]
        if not merged_profile_materials or any(
            material.get("unreal_instance_profile") != "dead"
            for material in merged_profile_materials
        ):
            raise AssertionError(
                {
                    "materials": [material.name for material in merged_profile_materials],
                    "profiles": [
                        material.get("unreal_instance_profile")
                        for material in merged_profile_materials
                    ],
                }
            )
        if not source_profile_materials.issubset(
            {material.as_pointer() for material in merged_profile_materials}
        ):
            raise AssertionError("merge replaced one or more profiled material datablocks")

        write_profile_spm("")
        cleared_profile_result = apply_spm_unreal_instance_profile(
            [stale_profile_object], spm_path
        )
        if "unreal_instance_profile" in stale_profile_material:
            raise AssertionError(cleared_profile_result)
        name_only_result = apply_spm_unreal_instance_profile(
            [name_only_object], spm_path
        )
        if any(
            "unreal_instance_profile" in material
            for material in name_only_materials
        ):
            raise AssertionError(name_only_result)

        write_profile_spm("../dead")
        invalid_profile = inspect_spm_unreal_instance_profile(spm_path)
        if invalid_profile.get("status") != "inspection_error":
            raise AssertionError(invalid_profile)

        stable_source_identity = asset / "Cluster" / "SK_contract.spm"
        canonical_source_fbx = asset / "Cluster" / "fbx" / "SK_contract.fbx"
        current_cache_fbx = (
            asset
            / "Cluster"
            / ".sk_batch_isolated_bark"
            / "current"
            / "SK_contract.fbx"
        )
        old_cache_fbx = (
            asset
            / "Cluster"
            / ".sk_batch_isolated_bark"
            / "old"
            / "SK_contract.fbx"
        )
        alias_owned = bpy.data.objects.new("Cleanup_Alias_Owned", None)
        alias_owned["codex_source_fbx"] = str(canonical_source_fbx)
        bpy.context.scene.collection.objects.link(alias_owned)
        alias_owned_name = alias_owned.name
        identity_owned = bpy.data.objects.new("Cleanup_Identity_Owned", None)
        identity_owned["codex_source_fbx"] = str(old_cache_fbx)
        identity_owned["codex_source_identity"] = str(stable_source_identity)
        bpy.context.scene.collection.objects.link(identity_owned)
        identity_owned_name = identity_owned.name
        unrelated = bpy.data.objects.new("Cleanup_Unrelated", None)
        unrelated["codex_source_fbx"] = str(old_cache_fbx)
        unrelated["codex_source_identity"] = str(asset / "other.spm")
        bpy.context.scene.collection.objects.link(unrelated)
        unrelated_name = unrelated.name
        cleanup_result = clear_previous_codex_build(
            {
                "source_fbx_path": str(current_cache_fbx),
                "source_fbx_cleanup_aliases": [str(canonical_source_fbx)],
                "source_identity_path": str(stable_source_identity),
                "out_dir": str(asset),
                "name_stem": "SK_contract",
            }
        )
        if bpy.data.objects.get(alias_owned_name) is not None:
            raise AssertionError(cleanup_result)
        if bpy.data.objects.get(identity_owned_name) is not None:
            raise AssertionError(cleanup_result)
        if bpy.data.objects.get(unrelated_name) is None:
            raise AssertionError(cleanup_result)

        return {
            "preserved_cluster": normalization,
            "strict_preserved_cluster": strict_preserved_cluster,
            "outside_cluster_rejected": outside_result,
            "missing_cluster_source_rejected": missing_result,
            "shared_stmat_texture_set": shared_result,
            "shared_texture_contract": shared_contract_result,
            "strict_shared_material_intent": strict_intent_result,
            "strict_shared_texture_contract": strict_shared_result,
            "strict_name_fallback_blocked": strict_no_fallback,
            "variant_only_consolidation": variant_only,
            "variant_only_materials": variant_names,
            "canonical_files": {role: str(path) for role, path in canonical.items()},
            "canonical_consolidation": canonical_result,
            "canonical_materials": canonical_names,
            "arbitrary_suffix_consolidation": arbitrary_result,
            "arbitrary_suffix_materials": arbitrary_names,
            "arbitrary_suffix_normalization": arbitrary_normalization,
            "distinct_signature_not_consolidated": distinct_result,
            "source_isolation_consolidation": isolated_result,
            "legacy_manifest_binding": manifest_binding,
            "canonical_manifest_rewire": canonical_manifest_result,
            "strict_canonical_manifest_rewire": (
                strict_canonical_manifest_result
            ),
            "provisional_manifest_rewire": provisional_result,
            "strict_provisional_manifest_rewire": (
                strict_provisional_manifest_result
            ),
            "provisional_manifest_promoted": promoted_result,
            "malformed_manifest_blocked": malformed_manifest_blocked,
            "invalid_manifest_blocked": invalid_manifest_blocked,
            "instance_profile": profile_result,
            "strict_contract_load": {
                "contract_path": loaded_strict_contract.get("contract_path"),
                "binding_count": len(
                    loaded_strict_contract.get("bindings") or []
                ),
            },
            "profile_mismatch_blocked": profile_mismatch_blocked,
            "consolidated_instance_profile": consolidated_profile_result,
            "merged_instance_profile": {
                "object": merged_profile_object.name,
                "materials": [material.name for material in merged_profile_materials],
                "profiles": [
                    material.get("unreal_instance_profile")
                    for material in merged_profile_materials
                ],
            },
            "name_only_material_groups": name_only_result,
            "cleared_instance_profile": cleared_profile_result,
            "invalid_instance_profile": invalid_profile,
            "stable_source_cleanup": cleanup_result,
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
            _speedtree_manifest_binding,
            _speedtree_manifest_texture_binding,
            apply_speedtree_material_intents,
            apply_spm_unreal_instance_profile,
            consolidate_speedtree_group_materials,
            inspect_spm_unreal_instance_profile,
            load_speedtree_texture_readiness_contract,
            merge_skinned_meshes,
            normalize_speedtree_material_textures,
            run_import_source_fbx,
            clear_previous_codex_build,
        )
        from speedtree_bone_weight_repair import handoff_contract

        if args.contracts:
            result = run_contract_smoke(
                normalize_speedtree_material_textures,
                consolidate_speedtree_group_materials,
                _speedtree_manifest_binding,
                _speedtree_manifest_texture_binding,
                apply_speedtree_material_intents,
                handoff_contract,
                load_speedtree_texture_readiness_contract,
                inspect_spm_unreal_instance_profile,
                apply_spm_unreal_instance_profile,
                merge_skinned_meshes,
                clear_previous_codex_build,
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
