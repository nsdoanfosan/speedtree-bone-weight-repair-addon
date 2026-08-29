"""Blender-only smoke checks for strict merged placeholder normalization."""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

import bpy


REPO_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_DIR / "addons"))

from speedtree_bone_weight_repair import core


def mesh_object(name, materials):
    mesh = bpy.data.meshes.new(name + "Mesh")
    mesh.from_pydata(
        [
            (0.0, 0.0, 0.0),
            (1.0, 0.0, 0.0),
            (0.0, 1.0, 0.0),
            (1.0, 1.0, 0.0),
        ],
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


def write_png(path):
    image = bpy.data.images.new(path.stem + "_source", width=1, height=1)
    image.filepath_raw = str(path)
    image.file_format = "PNG"
    image.save()
    bpy.data.images.remove(image)


def intent(api, index, name, *, tree_part="", mode="", binding=None):
    return {
        "stmat_material_index": index,
        "stmat_material_id": str(index + 1),
        "material_name": name,
        "material_key": api.normalize_material_key(name),
        "production_group_base": api.production_group_base_name(name),
        "tree_part": tree_part,
        "tree_shading": "wood" if tree_part == "bark" else "",
        "texture_source_mode": mode,
        "texture_binding": binding or {},
    }


def contract(intents, *, runtime_tolerant=False):
    result = {
        "status": "ok",
        "strict_speedtree_pipeline_contract": True,
        "speedtree_pipeline_contract": {"material_intents": intents},
    }
    if runtime_tolerant:
        result[core.handoff_contract.TEXTURE_CONTRACT_MODE_FIELD] = (
            core.handoff_contract.RUNTIME_TOLERANT_TEXTURE_MODE
        )
    return result


def ready_binding(texture_dir, base):
    files = {}
    for role in core.SPEEDTREE_TEXTURE_ROLES:
        path = texture_dir / f"{base}_{role}.tga"
        path.write_bytes(role.encode("ascii"))
        files[role] = str(path)
    return {
        "status": "ok",
        "set_key": base.casefold(),
        "texture_base": base,
        "files": files,
        "missing_roles": [],
    }


bpy.ops.wm.read_factory_settings(use_empty=True)
api = core.handoff_contract.central_contract_api()

with tempfile.TemporaryDirectory(
    prefix="bwr_placeholder_material_"
) as temporary:
    texture_dir = Path(temporary)
    bark_binding = ready_binding(texture_dir, "T_bark_safe")
    default_intent = intent(
        api,
        0,
        "Default_Mat",
        mode="preserve_declared_sources",
        binding={"status": "not_managed", "files": {}},
    )
    bark_intent = intent(
        api,
        1,
        "M_bark_safe_Mat",
        tree_part="bark",
        mode="managed_texture_set",
        binding=bark_binding,
    )

    default_material = bpy.data.materials.new("Default")
    bark_material = bpy.data.materials.new("M_bark_safe")
    bark_material[core.UNREAL_TREE_PART_PROPERTY] = "bark"
    merged = mesh_object("Merged", [default_material, bark_material])
    shared = bpy.data.objects.new("SharedBeforeCopy", merged.data)
    bpy.context.scene.collection.objects.link(shared)

    assigned_result = core.normalize_merged_speedtree_placeholder_material(
        merged, contract([default_intent, bark_intent])
    )
    assert assigned_result["proof"] == (
        "strict_stmat_default_to_unique_semantic_bark"
    ), assigned_result
    assert assigned_result["changed_face_count"] == 1, assigned_result
    assert merged.data is not shared.data
    assert list(merged.data.materials) == [bark_material]
    assert [poly.material_index for poly in merged.data.polygons] == [0, 0]

    unused_default = mesh_object(
        "UnusedDefault", [default_material, bark_material]
    )
    for polygon in unused_default.data.polygons:
        polygon.material_index = 1
    result = core.normalize_merged_speedtree_placeholder_material(
        unused_default, contract([default_intent, bark_intent])
    )
    assert result["status"] == "applied", result
    assert result["changed_face_count"] == 0, result
    assert result["proof"] == "unused_strict_stmat_default_slot_removed"
    assert list(unused_default.data.materials) == [bark_material]
    assert [poly.material_index for poly in unused_default.data.polygons] == [0, 0]
    assert list(shared.data.materials) == [default_material, bark_material]
    assert core.normalize_merged_speedtree_placeholder_material(
        unused_default, contract([default_intent, bark_intent])
    )["status"] == "not_applicable"

    none_merged = mesh_object("NoneMerged", [None, bark_material])
    none_result = core.normalize_merged_speedtree_placeholder_material(
        none_merged, contract([default_intent, bark_intent])
    )
    assert none_result["changed_face_count"] == 1, none_result
    assert list(none_merged.data.materials) == [bark_material]

    second_bark = bpy.data.materials.new("M_bark_second")
    second_bark[core.UNREAL_TREE_PART_PROPERTY] = "bark"
    ambiguous = mesh_object(
        "AmbiguousDefault", [default_material, bark_material, second_bark]
    )
    second_intent = intent(
        api,
        2,
        "M_bark_second_Mat",
        tree_part="bark",
        mode="managed_texture_set",
        binding=ready_binding(texture_dir, "T_bark_second"),
    )
    try:
        core.normalize_merged_speedtree_placeholder_material(
            ambiguous,
            contract([default_intent, bark_intent, second_intent]),
        )
    except RuntimeError as exc:
        assert "exactly one semantic bark" in str(exc), exc
    else:
        raise AssertionError("ambiguous bark candidates were auto-remapped")

    unknown_none = mesh_object("UnknownNone", [bark_material, None])
    unknown_before = list(unknown_none.data.materials)
    unknown_result = core.normalize_merged_speedtree_placeholder_material(
        unknown_none, contract([default_intent, bark_intent])
    )
    assert unknown_result["status"] == "not_applicable", unknown_result
    assert list(unknown_none.data.materials) == unknown_before
    try:
        core.validate_face_assigned_material_slots(unknown_none)
    except RuntimeError as exc:
        assert "polygon-assigned empty material slots" in str(exc), exc
    else:
        raise AssertionError("face-assigned empty material slot was accepted")

    unused_none = mesh_object("UnusedNone", [bark_material, None])
    for polygon in unused_none.data.polygons:
        polygon.material_index = 0
    unused_none_validation = core.validate_face_assigned_material_slots(
        unused_none
    )
    assert unused_none_validation["status"] == "ok", unused_none_validation
    assert unused_none_validation["unused_empty_slot_indices"] == [1]

    tree_root = texture_dir / "tree"
    cluster_fbx = tree_root / "cluster" / "fbx" / "SK_leaf_test.fbx"
    cluster_fbx.parent.mkdir(parents=True)
    cluster_fbx.write_bytes(b"fbx")
    cluster_dirs = core._speedtree_material_texture_dirs(
        cluster_fbx,
        bark_material,
        {"materials": {}},
    )
    assert tree_root / "texture" in cluster_dirs, cluster_dirs
    assert tree_root / "texture" / "substance" in cluster_dirs, cluster_dirs
    assert tree_root / "cluster" / "texture" in cluster_dirs, cluster_dirs

    canonical_base = "T_leaf_test_atlas_01"
    canonical_files = {}
    canonical_texture_dir = tree_root / "texture"
    canonical_texture_dir.mkdir(parents=True, exist_ok=True)
    for role in core.SPEEDTREE_TEXTURE_ROLES:
        path = canonical_texture_dir / f"{canonical_base}_{role}.png"
        write_png(path)
        canonical_files[role] = path

    canonical_material = bpy.data.materials.new("M_leaf_test_atlas_01")
    canonical_material.use_nodes = True
    canonical_material["codex_source_fbx"] = str(cluster_fbx)
    canonical_material["codex_source_identity"] = str(
        tree_root / "cluster" / "SK_leaf_test.spm"
    )
    canonical_material["codex_speedtree_texture_base"] = canonical_base
    core._replace_speedtree_material_nodes(
        canonical_material,
        canonical_files,
    )

    isolated_root = (
        tree_root
        / "cluster"
        / ".sk_batch_isolated_bark"
        / "hash"
        / "tree"
        / "cluster"
        / "fbx"
    )
    isolated_root.mkdir(parents=True)
    isolated_material = bpy.data.materials.new(
        "M_leaf_test_atlas_01_green"
    )
    isolated_material.use_nodes = True
    isolated_material["codex_source_fbx"] = str(
        isolated_root / "SK_leaf_test.fbx"
    )
    isolated_image_path = isolated_root / "M_leaf_test_green.png"
    isolated_image_path.write_bytes(b"image")
    isolated_image = bpy.data.images.load(str(isolated_image_path))
    isolated_node = isolated_material.node_tree.nodes.new(
        "ShaderNodeTexImage"
    )
    isolated_node.image = isolated_image
    isolated_object = mesh_object(
        "IsolatedPrototype",
        [isolated_material],
    )

    rebound = core.rebind_blocked_speedtree_group_variants(
        [isolated_object]
    )
    assert rebound["status"] == "ok", rebound
    assert rebound["rebound_count"] == 1, rebound
    assert (
        isolated_material["codex_source_fbx"] == str(cluster_fbx)
    ), rebound
    assert not any(
        core._blocked_atlas_texture_path(path)
        for path in core.material_texture_signature(isolated_material)
    ), rebound
    assert {
        Path(path).name.casefold()
        for path in core.material_texture_signature(isolated_material)
    } == {
        f"{canonical_base}_{role}.png".casefold()
        for role in core.SPEEDTREE_TEXTURE_ROLES
    }, rebound

print("PLACEHOLDER_MATERIAL_SMOKE_OK")
