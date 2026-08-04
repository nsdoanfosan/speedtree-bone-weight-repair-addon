"""Blender smoke checks for non-blocking partial/unassigned texture handoff."""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

import bpy


REPO_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_DIR / "addons"))

from speedtree_bone_weight_repair import core


def mesh_object(name, material):
    mesh = bpy.data.meshes.new(name + "Mesh")
    mesh.from_pydata(
        [(0.0, 0.0, 0.0), (1.0, 0.0, 0.0), (0.0, 1.0, 0.0)],
        [],
        [(0, 1, 2)],
    )
    mesh.materials.append(material)
    obj = bpy.data.objects.new(name, mesh)
    bpy.context.scene.collection.objects.link(obj)
    return obj


def write_image(path):
    image = bpy.data.images.new(path.stem + "_source", width=1, height=1)
    image.filepath_raw = str(path)
    image.file_format = "PNG"
    image.save()
    bpy.data.images.remove(image)


def binding(material, files, *, status="partial", suffix=""):
    return {
        "material": material.name,
        "material_key": core._speedtree_material_name_key(material.name),
        "production_group_base": material.name,
        "status": status,
        "texture_source_mode": "managed_texture_set",
        "set_key": (material.name + suffix).casefold(),
        "texture_base": "T_" + material.name.removeprefix("M_") + suffix,
        "texture_dir": str(next(iter(files.values())).parent) if files else "",
        "files": {role: str(path) for role, path in files.items()},
        "missing_roles": [
            role for role in core.SPEEDTREE_TEXTURE_ROLES if role not in files
        ],
    }


def contract(bindings, intents=None):
    return {
        "status": "ok",
        "strict_speedtree_pipeline_contract": True,
        core.handoff_contract.TEXTURE_CONTRACT_MODE_FIELD: (
            core.handoff_contract.RUNTIME_TOLERANT_TEXTURE_MODE
        ),
        "atlas_manifest_prevalidated": True,
        "atlas_manifest_statuses": [],
        "bindings": bindings,
        "speedtree_pipeline_contract": {
            "material_intents": list(intents or [])
        },
    }


bpy.ops.wm.read_factory_settings(use_empty=True)

empty_runtime_contract = core._bat_runtime_texture_contract(None)
assert empty_runtime_contract["bindings"] == []
assert empty_runtime_contract[
    core.handoff_contract.TEXTURE_CONTRACT_MODE_FIELD
] == core.handoff_contract.RUNTIME_TOLERANT_TEXTURE_MODE

with tempfile.TemporaryDirectory(prefix="bwr_runtime_texture_") as temporary:
    root = Path(temporary)
    source_fbx = root / "fbx" / "SK_runtime.fbx"
    source_fbx.parent.mkdir(parents=True)
    source_fbx.write_bytes(b"fixture")
    source_fbx.with_suffix(".stmat").write_text(
        "<SpeedTreeMaterials />", encoding="utf-8"
    )

    missing_metadata = core.load_speedtree_runtime_texture_contract(
        root / "missing_texture_contract.json",
        spm_path=root / "unused.spm",
        source_fbx_path=source_fbx,
    )
    assert missing_metadata["bindings"] == [], missing_metadata
    assert missing_metadata["texture_warnings"] == [], missing_metadata
    malformed_path = root / "malformed_texture_contract.json"
    malformed_path.write_text("{not-json", encoding="utf-8")
    malformed_metadata = core.load_speedtree_runtime_texture_contract(
        malformed_path,
        spm_path=root / "unused.spm",
        source_fbx_path=source_fbx,
    )
    assert malformed_metadata["bindings"] == [], malformed_metadata
    assert malformed_metadata["texture_warnings"] == [], malformed_metadata

    partial_dir = root / "texture"
    partial_dir.mkdir()
    partial_files = {}
    for role in ("color", "normal"):
        path = partial_dir / f"T_Runtime_partial_{role}.png"
        write_image(path)
        partial_files[role] = path
    partial_material = bpy.data.materials.new("M_Runtime_partial")
    partial_material["codex_source_fbx"] = str(source_fbx)
    partial_object = mesh_object("PartialObject", partial_material)
    partial_contract = contract(
        [binding(partial_material, partial_files)]
    )
    partial_contract.pop("atlas_manifest_prevalidated")
    partial_preflight = core.preflight_speedtree_material_texture_contracts(
        [partial_object],
        partial_contract,
        source_fbx_override=str(source_fbx),
    )
    assert partial_preflight["status"] == "ok", partial_preflight
    assert partial_preflight["texture_outcome"] == "partial", partial_preflight
    assert partial_preflight["blocking"] == [], partial_preflight
    assert partial_preflight["warnings"] == [], partial_preflight
    partial_result = core.normalize_speedtree_material_textures(
        [partial_object],
        texture_contract=partial_preflight["texture_contract"],
    )
    partial_row = partial_result["materials"][0]
    assert partial_result["status"] == "ok", partial_result
    assert partial_result["texture_outcome"] == "partial", partial_result
    assert partial_result["blocking"] == [], partial_result
    assert partial_result["warnings"] == [], partial_result
    assert partial_row["status"] == "partial", partial_row
    assert set(partial_row["available_roles"]) == {"color", "normal"}
    assert set(partial_row["files"]) == {"color", "normal"}
    assert {
        Path(bpy.path.abspath(node.image.filepath_raw or node.image.filepath)).resolve()
        for node in partial_material.node_tree.nodes
        if node.type == "TEX_IMAGE" and node.image
    } == set(partial_files.values())

    corrupt_dir = root / "corrupt_texture"
    corrupt_dir.mkdir()
    corrupt_color = corrupt_dir / "T_Runtime_corrupt_color.png"
    corrupt_color.write_bytes(b"not-a-decodable-image")
    valid_normal = corrupt_dir / "T_Runtime_corrupt_normal.png"
    write_image(valid_normal)
    corrupt_files = {
        "color": corrupt_color,
        "normal": valid_normal,
    }
    corrupt_material = bpy.data.materials.new("M_Runtime_corrupt")
    corrupt_material["codex_source_fbx"] = str(source_fbx)
    corrupt_object = mesh_object("CorruptObject", corrupt_material)
    corrupt_result = core.normalize_speedtree_material_textures(
        [corrupt_object],
        texture_contract=contract(
            [binding(corrupt_material, corrupt_files)]
        ),
    )
    corrupt_row = corrupt_result["materials"][0]
    assert corrupt_row["status"] == "partial", corrupt_row
    assert corrupt_row["available_roles"] == ["normal"], corrupt_row
    assert "color" in corrupt_row["missing_roles"], corrupt_row
    assert corrupt_result["blocking"] == [], corrupt_result
    assert {
        row["code"] for row in corrupt_result["warnings"]
    } == {"texture_image_load_failed"}, corrupt_result
    assert {
        Path(bpy.path.abspath(node.image.filepath_raw or node.image.filepath)).resolve()
        for node in corrupt_material.node_tree.nodes
        if node.type == "TEX_IMAGE" and node.image
    } == {valid_normal}

    quarantined_material = bpy.data.materials.new("M_Runtime_quarantined")
    quarantined_material["codex_source_fbx"] = str(source_fbx)
    quarantined_object = mesh_object(
        "QuarantinedObject", quarantined_material
    )
    quarantined_binding = binding(
        quarantined_material,
        partial_files,
        status="unassigned",
    )
    quarantined_binding.update(
        {
            "binding_disposition": "leave_unassigned",
            "texture_contract_status": core.ATLAS_SOURCE_FALLBACK_STATUS,
            "source_evidence": "authoritative_global_original_root",
            "source_paths": {
                role: str(path) for role, path in partial_files.items()
            },
        }
    )
    quarantined_result = core.normalize_speedtree_material_textures(
        [quarantined_object],
        texture_contract=contract([quarantined_binding]),
    )
    quarantined_row = quarantined_result["materials"][0]
    assert quarantined_row["status"] == "unassigned", quarantined_row
    assert not any(
        node.type == "TEX_IMAGE"
        for node in quarantined_material.node_tree.nodes
    )
    assert "codex_speedtree_texture_base" not in quarantined_material

    ambiguous_material = bpy.data.materials.new("M_Runtime_ambiguous")
    ambiguous_material.use_nodes = True
    dummy = bpy.data.images.new("AmbiguousDummy", width=1, height=1)
    dummy_node = ambiguous_material.node_tree.nodes.new("ShaderNodeTexImage")
    dummy_node.image = dummy
    ambiguous_material["codex_source_fbx"] = str(source_fbx)
    ambiguous_object = mesh_object("AmbiguousObject", ambiguous_material)
    ambiguous_bindings = [
        binding(ambiguous_material, partial_files, suffix="_A"),
        binding(ambiguous_material, partial_files, suffix="_B"),
    ]
    ambiguous_result = core.normalize_speedtree_material_textures(
        [ambiguous_object],
        texture_contract=contract(ambiguous_bindings),
    )
    ambiguous_row = ambiguous_result["materials"][0]
    assert ambiguous_row["status"] == "unassigned", ambiguous_row
    assert ambiguous_row["binding_disposition"] == "leave_unassigned"
    assert not any(
        node.type == "TEX_IMAGE"
        for node in ambiguous_material.node_tree.nodes
    )
    assert ambiguous_result["warnings"] == [], ambiguous_result

    unsafe_dir = root / ".sk_batch_isolated_bark" / "texture"
    unsafe_dir.mkdir(parents=True)
    unsafe_files = {}
    for role in core.SPEEDTREE_TEXTURE_ROLES:
        path = unsafe_dir / f"T_Runtime_unsafe_{role}.png"
        write_image(path)
        unsafe_files[role] = path
    unsafe_material = bpy.data.materials.new("M_Runtime_unsafe")
    unsafe_material["codex_source_fbx"] = str(source_fbx)
    unsafe_object = mesh_object("UnsafeObject", unsafe_material)
    unsafe_result = core.normalize_speedtree_material_textures(
        [unsafe_object],
        texture_contract=contract(
            [binding(unsafe_material, unsafe_files, status="ok")]
        ),
    )
    unsafe_row = unsafe_result["materials"][0]
    assert unsafe_row["status"] == "unassigned", unsafe_row
    assert unsafe_result["blocking"] == [], unsafe_result
    assert unsafe_result["warnings"] == [], unsafe_result
    assert "unsafe_texture_path_quarantined" in {
        row["code"] for row in unsafe_result["diagnostics"]
    }, unsafe_result
    assert not any(
        node.type == "TEX_IMAGE"
        for node in unsafe_material.node_tree.nodes
    )

    try:
        core.apply_speedtree_material_intents(
            [partial_object], contract([], intents=[])
        )
    except RuntimeError as exc:
        assert partial_material.name in str(exc), exc
    else:
        raise AssertionError("runtime texture mode hid unmatched material intent")

print("RUNTIME_TOLERANT_TEXTURE_SMOKE_OK")
