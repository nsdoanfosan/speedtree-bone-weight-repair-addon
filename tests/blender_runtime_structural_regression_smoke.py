"""Blender regressions for structural handoff with optional textures."""

from __future__ import annotations

import json
import sys
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path

import bpy


REPO_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_DIR / "addons"))

from speedtree_bone_weight_repair import core


def write_image(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    image = bpy.data.images.new(path.stem + "_source", width=1, height=1)
    image.filepath_raw = str(path)
    image.file_format = "PNG"
    image.save()
    bpy.data.images.remove(image)


def write_stmat_sources(source_fbx, material_name, files):
    root = ET.Element("Materials")
    material = ET.SubElement(root, "Material", Name=material_name + "_Mat")
    for role, path in files.items():
        ET.SubElement(
            material,
            "Map",
            Name=role.title(),
            Source="../texture/" + path.name,
        )
    ET.ElementTree(root).write(
        source_fbx.with_suffix(".stmat"),
        encoding="utf-8",
        xml_declaration=True,
    )


def make_mesh_object(name, materials):
    vertices = []
    faces = []
    for index in range(max(1, len(materials))):
        offset = len(vertices)
        vertices.extend(
            (
                (index * 2.0, 0.0, 0.0),
                (index * 2.0 + 1.0, 0.0, 0.0),
                (index * 2.0, 1.0, 0.0),
            )
        )
        faces.append((offset, offset + 1, offset + 2))
    mesh = bpy.data.meshes.new(name + "Mesh")
    mesh.from_pydata(vertices, [], faces)
    for material in materials:
        mesh.materials.append(material)
    for index, polygon in enumerate(mesh.polygons):
        polygon.material_index = min(index, len(materials) - 1)
    obj = bpy.data.objects.new(name, mesh)
    bpy.context.scene.collection.objects.link(obj)
    return obj


def make_intent(
    name,
    index,
    *,
    tree_part="leaf",
    tree_shading="foliage",
    files=None,
):
    files = dict(files or {})
    api = core.handoff_contract.central_contract_api()
    intent = api.build_material_intent(
        name,
        explicit_tree_part=tree_part,
        explicit_tree_shading=tree_shading,
    )
    status = "partial" if files else "unassigned"
    intent.update(
        {
            "stmat_material_index": index,
            "stmat_material_id": str(index + 1),
            "material_name": name,
            "texture_source_mode": (
                "managed_texture_set" if files else "unresolved"
            ),
            "texture_binding": {
                "status": status,
                "binding_disposition": (
                    "bind_available" if files else "leave_unassigned"
                ),
                "set_key": name.casefold(),
                "texture_base": "T_" + name.removeprefix("M_"),
                "texture_dir": (
                    str(next(iter(files.values())).parent) if files else ""
                ),
                "files": {
                    role: str(path) for role, path in files.items()
                },
                "available_roles": sorted(files),
                "missing_roles": sorted(
                    set(core.SPEEDTREE_TEXTURE_ROLES) - set(files)
                ),
            },
        }
    )
    return intent


def strict_runtime_contract(intents):
    envelope = {"material_intents": list(intents)}
    return {
        "status": "ok",
        "strict_speedtree_pipeline_contract": True,
        core.handoff_contract.TEXTURE_CONTRACT_MODE_FIELD: (
            core.handoff_contract.RUNTIME_TOLERANT_TEXTURE_MODE
        ),
        "atlas_manifest_prevalidated": True,
        "atlas_manifest_statuses": [],
        "speedtree_pipeline_contract": envelope,
        "bindings": core.handoff_contract.texture_bindings_from_envelope(
            envelope
        ),
    }


def material_names(obj):
    return [material.name if material else None for material in obj.data.materials]


bpy.ops.wm.read_factory_settings(use_empty=True)

boneless_material = bpy.data.materials.new("M_NativeBoneless")
boneless_mesh = make_mesh_object("NativeBoneless", [boneless_material])
boneless_imported = [boneless_mesh]
boneless_result = core.build_native_boneless_rigid_armature(
    boneless_imported,
    {
        "id_zero_cluster_write": "not_applicable_boneless_export",
        "bones": [],
    },
)
assert boneless_result["status"] == "applied", boneless_result
assert boneless_result["binding_policy"] == "native_boneless_rigid_axis_v1"
boneless_armature = bpy.data.objects[boneless_result["armature"]]
assert [bone.name for bone in boneless_armature.data.bones] == [
    "Bone_1_Start"
]
boneless_group = boneless_mesh.vertex_groups["Bone_1_Start"]
assert all(
    boneless_group.weight(vertex.index) == 1.0
    for vertex in boneless_mesh.data.vertices
)
assert boneless_mesh.parent == boneless_armature
assert [
    modifier.object
    for modifier in boneless_mesh.modifiers
    if modifier.type == "ARMATURE"
] == [boneless_armature]
boneless_records, boneless_info = core.build_boneless_rigid_metadata(
    "boneless.xml",
    boneless_armature,
    "No <Bone> entries found",
)
assert boneless_records == [{
    "name": "Bone_1_Start",
    "bone_index": 0,
    "parent_index": -1,
    "xml_id": None,
    "generator": "NativeBonelessRigid",
    "mass": 0.0,
    "radius": 0.0,
    "group": 0,
    "native_role": "boneless_rigid_axis",
}]
assert boneless_info["mapping_contract"] == "native_boneless_rigid_axis_v1"

bpy.ops.wm.read_factory_settings(use_empty=True)

with tempfile.TemporaryDirectory(prefix="bwr_runtime_structural_") as temporary:
    root = Path(temporary)
    source_fbx = root / "shared" / "fbx" / "SK_Runtime.fbx"
    source_fbx.parent.mkdir(parents=True)
    source_fbx.write_bytes(b"fixture")
    source_fbx.with_suffix(".stmat").write_text(
        "<Materials />", encoding="utf-8"
    )
    source_spm = root / "shared" / "SK_Runtime.spm"
    source_spm.write_bytes(b"current-spm-fixture")

    # Exact material-less Default/Material FBX objects are dummy geometry when
    # one current unmanaged, source-empty STMAT intent proves that identity.
    # A differently named no-slot authored mesh remains preserved.
    cleanup_material = bpy.data.materials.new("M_cluster_Runtime_01")
    cleanup_renderable = make_mesh_object(
        "CleanupRenderable", [cleanup_material]
    )
    cleanup_unassigned = make_mesh_object("Default", [])
    cleanup_authored_no_slots = make_mesh_object("AuthoredPlant", [])
    cleanup_contract = strict_runtime_contract(
        [
            make_intent("Default", 0, tree_part=None),
            make_intent("M_cluster_Runtime_01", 1),
        ]
    )
    cleanup_result = core.discard_unassigned_geometry_before_assembly(
        [cleanup_renderable, cleanup_unassigned, cleanup_authored_no_slots],
        texture_contract=cleanup_contract,
        spm_path=str(source_spm),
        source_fbx_path=str(source_fbx),
    )
    assert cleanup_result["status"] == "applied", cleanup_result
    assert cleanup_result["cleanup_contract_version"] == 5, cleanup_result
    assert cleanup_result["inspected_mesh_object_count"] == 3, cleanup_result
    assert cleanup_result["changed_object_count"] == 1, cleanup_result
    assert cleanup_result["removed_object_count"] == 1, cleanup_result
    assert cleanup_result["removed_face_count"] == 1, cleanup_result
    assert cleanup_result["removed_objects"] == ["Default"], cleanup_result
    assert bpy.data.objects.get("Default") is None
    assert bpy.data.objects.get("AuthoredPlant") is cleanup_authored_no_slots
    assert len(cleanup_renderable.data.polygons) == 1
    assert core.renderable_geometry_evidence([cleanup_renderable]) == {
        "status": "ok",
        "mesh_object_count": 1,
        "face_count": 1,
    }
    assert core.authorized_dummy_cleanup_left_no_renderable_geometry({
        "unassigned_geometry_cleanup": {
            "cleanup_authorized": True,
            "removed_face_count": 6,
        },
        "renderable_geometry": {
            "status": "empty",
            "mesh_object_count": 0,
            "face_count": 0,
        },
    })
    assert not core.authorized_dummy_cleanup_left_no_renderable_geometry({
        "unassigned_geometry_cleanup": {
            "cleanup_authorized": True,
            "removed_face_count": 0,
        },
        "renderable_geometry": {
            "status": "empty",
            "mesh_object_count": 0,
            "face_count": 0,
        },
    })
    cleanup_recheck = core.discard_unassigned_geometry_before_assembly(
        [cleanup_renderable],
        texture_contract=cleanup_contract,
        spm_path=str(source_spm),
        source_fbx_path=str(source_fbx),
    )
    assert cleanup_recheck["status"] == "not_applicable", cleanup_recheck

    # Cleanup authority is a material-slot property.  A many-face mesh whose
    # sole slot is not a proven placeholder must resolve that slot once and
    # avoid touching polygon RNA entirely.
    scan_mesh = bpy.data.meshes.new("CleanupSlotMemoizationMesh")
    scan_vertices = []
    scan_faces = []
    for index in range(50):
        offset = len(scan_vertices)
        scan_vertices.extend(
            ((float(index), 0.0, 0.0), (float(index), 1.0, 0.0), (float(index), 0.0, 1.0))
        )
        scan_faces.append((offset, offset + 1, offset + 2))
    scan_mesh.from_pydata(scan_vertices, [], scan_faces)
    scan_mesh.materials.append(cleanup_material)
    scan_object = bpy.data.objects.new("CleanupSlotMemoization", scan_mesh)
    bpy.context.scene.collection.objects.link(scan_object)
    original_cleanup_key = core._cleanup_placeholder_material_key
    cleanup_key_calls = []

    def counted_cleanup_key(material, texture_contract):
        cleanup_key_calls.append(material.name if material else None)
        return original_cleanup_key(material, texture_contract)

    core._cleanup_placeholder_material_key = counted_cleanup_key
    try:
        scan_result = core.discard_unassigned_geometry_before_assembly(
            [scan_object],
            texture_contract=cleanup_contract,
            spm_path=str(source_spm),
            source_fbx_path=str(source_fbx),
        )
    finally:
        core._cleanup_placeholder_material_key = original_cleanup_key
    assert scan_result["status"] == "not_applicable", scan_result
    assert cleanup_key_calls == [cleanup_material.name], cleanup_key_calls

    cleanup_default_material = bpy.data.materials.new("Default")
    cleanup_mixed = make_mesh_object(
        "CleanupMixed",
        [cleanup_default_material, cleanup_material],
    )
    mixed_result = core.discard_unassigned_geometry_before_assembly(
        [cleanup_mixed],
        texture_contract=cleanup_contract,
        spm_path=str(source_spm),
        source_fbx_path=str(source_fbx),
    )
    assert mixed_result["status"] == "applied", mixed_result
    assert mixed_result["removed_object_count"] == 0, mixed_result
    assert mixed_result["removed_face_count"] == 1, mixed_result
    assert len(cleanup_mixed.data.polygons) == 1
    assert material_names(cleanup_mixed) == ["M_cluster_Runtime_01"]
    assert cleanup_mixed.data.polygons[0].material_index == 0

    # A SpeedTree dummy can also be assigned to the generic STMAT ``Material``
    # entry.  Admit it only when the exact current intent is unmanaged and has
    # no texture files; this removes the dummy mesh before the Blender join.
    generic_dummy_material = bpy.data.materials.new("Material")
    generic_dummy = make_mesh_object(
        "GenericMaterialDummy",
        [generic_dummy_material],
    )
    generic_dummy_contract = strict_runtime_contract(
        [
            make_intent("Material", 0, tree_part=None),
            make_intent("M_cluster_Runtime_01", 1),
        ]
    )
    generic_dummy_result = core.discard_unassigned_geometry_before_assembly(
        [cleanup_renderable, generic_dummy],
        texture_contract=generic_dummy_contract,
        spm_path=str(source_spm),
        source_fbx_path=str(source_fbx),
    )
    assert generic_dummy_result["status"] == "applied", generic_dummy_result
    assert generic_dummy_result["removed_objects"] == [
        "GenericMaterialDummy"
    ], generic_dummy_result
    assert generic_dummy_result["objects"][0]["removed_face_reasons"] == {
        "canonical_unmanaged_material_placeholder": 1
    }, generic_dummy_result
    assert bpy.data.objects.get("GenericMaterialDummy") is None

    # The native collision exporter can put Material_Mat on a dummy FBX mesh
    # without writing that generic slot to STMAT at all.  Delete it only when
    # the validated current preflight envelope proves that exact omission.
    omitted_generic_material = make_mesh_object(
        "OmittedGenericMaterialDummy",
        [generic_dummy_material],
    )
    omitted_generic_contract = strict_runtime_contract(
        [make_intent("M_cluster_Runtime_01", 1)]
    )
    omitted_envelope = omitted_generic_contract["speedtree_pipeline_contract"]
    omitted_envelope.update(
        {
            "kind": "speedtree_material_preflight",
            "outcome": "ok",
            "source": {"stmat": [{"canonical_path": str(source_fbx.with_suffix('.stmat'))}]},
        }
    )
    omitted_generic_result = core.discard_unassigned_geometry_before_assembly(
        [cleanup_renderable, omitted_generic_material],
        texture_contract=omitted_generic_contract,
        spm_path=str(source_spm),
        source_fbx_path=str(source_fbx),
    )
    assert omitted_generic_result["status"] == "applied", omitted_generic_result
    assert omitted_generic_result["removed_objects"] == [
        "OmittedGenericMaterialDummy"
    ], omitted_generic_result
    assert bpy.data.objects.get("OmittedGenericMaterialDummy") is None

    # The same generic name without exact current STMAT omission evidence must
    # remain untouched.
    unproven_generic_material = make_mesh_object(
        "UnprovenGenericMaterial",
        [generic_dummy_material],
    )
    unproven_generic_contract = strict_runtime_contract(
        [make_intent("M_cluster_Runtime_01", 1)]
    )
    unproven_generic_result = core.discard_unassigned_geometry_before_assembly(
        [unproven_generic_material],
        texture_contract=unproven_generic_contract,
        spm_path=str(source_spm),
        source_fbx_path=str(source_fbx),
    )
    assert unproven_generic_result["status"] == "not_applicable"
    assert bpy.data.objects.get("UnprovenGenericMaterial") is unproven_generic_material

    # The generic name is not deletion authority when the current intent owns
    # a managed texture source.
    managed_generic = make_mesh_object(
        "ManagedGenericMaterial",
        [generic_dummy_material],
    )
    managed_generic_contract = strict_runtime_contract(
        [
            make_intent(
                "Material",
                0,
                files={"color": root / "texture" / "Material_color.tga"},
            ),
        ]
    )
    managed_generic_result = core.discard_unassigned_geometry_before_assembly(
        [managed_generic],
        texture_contract=managed_generic_contract,
        spm_path=str(source_spm),
        source_fbx_path=str(source_fbx),
    )
    assert managed_generic_result["status"] == "not_applicable"
    assert bpy.data.objects.get("ManagedGenericMaterial") is managed_generic

    # A source FBX may use only one Atlas Builder child collection.  The
    # strict intent still proves that its suffix is a production-group token,
    # so the dummy child name must collapse to the suffix-free atlas base.
    single_name = "M_leaf_Weeping_Willow_atlas_01_green"
    single_material = bpy.data.materials.new(single_name)
    single_material["codex_source_fbx"] = str(source_fbx)
    single_object = make_mesh_object(
        "SingleAtlasGroupObject", [single_material]
    )
    single_contract = strict_runtime_contract(
        [make_intent(single_name, 0)]
    )
    single_result = core.consolidate_speedtree_group_materials(
        [single_object], texture_contract=single_contract
    )
    assert material_names(single_object) == [
        "M_leaf_Weeping_Willow_atlas_01"
    ], single_result
    single_groups = [
        row
        for row in single_result["groups"]
        if row.get("mode") == "production_group_suffix"
    ]
    assert len(single_groups) == 1, single_result
    assert single_groups[0]["group_tokens"] == ["green"]
    assert single_groups[0]["changed_faces"] == 1

    # Separate provider contracts in one Assembly build must reuse the exact
    # strict production target instead of creating a Blender ``.001`` name.
    second_single_material = bpy.data.materials.new(single_name)
    second_single_object = make_mesh_object(
        "SecondSingleAtlasGroupObject", [second_single_material]
    )
    second_single_result = core.consolidate_speedtree_group_materials(
        [second_single_object], texture_contract=single_contract
    )
    assert material_names(second_single_object) == [
        "M_leaf_Weeping_Willow_atlas_01"
    ], second_single_result
    assert (
        second_single_object.data.materials[0]
        is single_object.data.materials[0]
    ), second_single_result

    # Texture completeness is not structural consolidation evidence. Two
    # exact, same-semantic intents still merge when one is partial and the
    # other has no usable textures at all.
    partial_color = root / "intent_texture" / "T_Intent_color.png"
    write_image(partial_color)
    tolerant_names = (
        "M_Leaf_runtime_group_01_green",
        "M_Leaf_runtime_group_01_dead",
    )
    tolerant_materials = [
        bpy.data.materials.new(name) for name in tolerant_names
    ]
    for material in tolerant_materials:
        material["codex_source_fbx"] = str(source_fbx)
    tolerant_object = make_mesh_object(
        "StrictPartialEmptyObject", tolerant_materials
    )
    tolerant_contract = strict_runtime_contract(
        [
            make_intent(
                tolerant_names[0], 0, files={"color": partial_color}
            ),
            make_intent(tolerant_names[1], 1),
        ]
    )
    tolerant_result = core.consolidate_speedtree_group_materials(
        [tolerant_object], texture_contract=tolerant_contract
    )
    assert material_names(tolerant_object) == [
        "M_Leaf_runtime_group_01"
    ], tolerant_result
    tolerant_groups = [
        row
        for row in tolerant_result["groups"]
        if row.get("mode") == "production_group_suffix"
    ]
    assert len(tolerant_groups) == 1, tolerant_result
    assert tolerant_groups[0]["provenance_type"] == "material_intent"

    # Equal numeric-boundary names do not merge across Unreal semantics.
    semantic_names = (
        "M_Mixed_runtime_group_02_green",
        "M_Mixed_runtime_group_02_dead",
    )
    semantic_materials = [
        bpy.data.materials.new(name) for name in semantic_names
    ]
    for material in semantic_materials:
        material["codex_source_fbx"] = str(source_fbx)
    semantic_object = make_mesh_object(
        "DifferentSemanticObject", semantic_materials
    )
    semantic_contract = strict_runtime_contract(
        [
            make_intent(
                semantic_names[0], 10, tree_part="leaf", tree_shading="foliage"
            ),
            make_intent(
                semantic_names[1], 11, tree_part="bark", tree_shading="wood"
            ),
        ]
    )
    semantic_result = core.consolidate_speedtree_group_materials(
        [semantic_object], texture_contract=semantic_contract
    )
    assert material_names(semantic_object) == list(semantic_names), semantic_result
    assert not any(
        row.get("mode") == "production_group_suffix"
        for row in semantic_result["groups"]
    ), semantic_result

    # An exact target intent is authoritative; a semantic conflict fails
    # before any material slot is mutated.
    guard_names = (
        "M_Leaf_target_guard_03_green",
        "M_Leaf_target_guard_03_dead",
    )
    guard_materials = [bpy.data.materials.new(name) for name in guard_names]
    for material in guard_materials:
        material["codex_source_fbx"] = str(source_fbx)
    guard_object = make_mesh_object("TargetIntentGuardObject", guard_materials)
    guard_contract = strict_runtime_contract(
        [
            make_intent(guard_names[0], 20),
            make_intent(guard_names[1], 21),
            make_intent(
                "M_Leaf_target_guard_03",
                22,
                tree_part="bark",
                tree_shading="wood",
            ),
        ]
    )
    try:
        core.consolidate_speedtree_group_materials(
            [guard_object], texture_contract=guard_contract
        )
    except RuntimeError as exc:
        assert "target intent conflicts" in str(exc), exc
    else:
        raise AssertionError("conflicting exact target intent was accepted")
    assert material_names(guard_object) == list(guard_names)

    # None is an authored empty slot, not a wildcard target. Slot and polygon
    # indices must survive consolidation exactly.
    none_names = (
        "M_Leaf_none_slot_04_green",
        "M_Leaf_none_slot_04_dead",
    )
    none_materials = [bpy.data.materials.new(name) for name in none_names]
    for material in none_materials:
        material["codex_source_fbx"] = str(source_fbx)
    none_object = make_mesh_object(
        "NoneSlotObject", [none_materials[0], none_materials[1], None]
    )
    none_contract = strict_runtime_contract(
        [make_intent(name, 30 + index) for index, name in enumerate(none_names)]
    )
    core.consolidate_speedtree_group_materials(
        [none_object], texture_contract=none_contract
    )
    assert len(none_object.data.materials) == 3
    assert none_object.data.materials[0] == none_object.data.materials[1]
    assert none_object.data.materials[2] is None
    assert [polygon.material_index for polygon in none_object.data.polygons] == [
        0,
        1,
        2,
    ]

    # Key-present null is a malformed new envelope, not absent legacy data.
    null_envelope_path = root / "null_envelope.json"
    null_envelope_path.write_text(
        json.dumps({core.handoff_contract.PIPELINE_ENVELOPE_FIELD: None}),
        encoding="utf-8",
    )
    try:
        core.load_speedtree_runtime_texture_contract(
            null_envelope_path,
            spm_path=root / "unused.spm",
            source_fbx_path=source_fbx,
        )
    except RuntimeError as exc:
        assert "envelope must be a JSON object" in str(exc), exc
    else:
        raise AssertionError("null pipeline envelope was treated as legacy data")

    # A live, unambiguous STMAT subset is safe to bind without all roles.
    local_root = root / "local_partial"
    local_fbx = local_root / "fbx" / "SK_LocalPartial.fbx"
    local_fbx.parent.mkdir(parents=True)
    local_fbx.write_bytes(b"fixture")
    local_material = bpy.data.materials.new("M_Leaf_local_partial_05")
    local_material["codex_source_fbx"] = str(local_fbx)
    local_files = {}
    for role in ("color", "normal"):
        path = local_root / "texture" / f"T_Leaf_local_partial_05_{role}.png"
        write_image(path)
        local_files[role] = path
    stmat_root = ET.Element("Materials")
    stmat_material = ET.SubElement(
        stmat_root, "Material", Name=local_material.name + "_Mat"
    )
    for role, path in local_files.items():
        ET.SubElement(
            stmat_material,
            "Map",
            Name=role.title(),
            Source="../texture/" + path.name,
        )
    ET.ElementTree(stmat_root).write(
        local_fbx.with_suffix(".stmat"),
        encoding="utf-8",
        xml_declaration=True,
    )
    local_object = make_mesh_object("LocalPartialObject", [local_material])
    local_preflight = core.preflight_speedtree_material_texture_contracts(
        [local_object],
        core._bat_runtime_texture_contract(None),
        source_fbx_override=str(local_fbx),
    )
    assert local_preflight["status"] == "ok", local_preflight
    # Preflight has no declared binding to validate; operational wiring then
    # performs the STMAT/local lookup and binds its one safe subset.
    assert local_preflight["texture_outcome"] == "unassigned", local_preflight
    assert local_preflight["blocking"] == [], local_preflight
    local_result = core.normalize_speedtree_material_textures(
        [local_object], texture_contract=local_preflight["texture_contract"]
    )
    local_row = local_result["materials"][0]
    assert local_row["status"] == "partial", local_row
    assert set(local_row["available_roles"]) == {"color", "normal"}, local_row
    assert local_result["blocking"] == [], local_result
    assert len(
        [
            node
            for node in local_material.node_tree.nodes
            if node.type == "TEX_IMAGE" and node.image
        ]
    ) == 2

    # A declared unresolved runtime binding authorizes no path, but it also
    # must not suppress an independent exact STMAT/local candidate.
    unresolved_root = root / "unresolved_local"
    unresolved_fbx = unresolved_root / "fbx" / "SK_UnresolvedLocal.fbx"
    unresolved_fbx.parent.mkdir(parents=True)
    unresolved_fbx.write_bytes(b"fixture")
    unresolved_material = bpy.data.materials.new(
        "M_Leaf_unresolved_local_09"
    )
    unresolved_material["codex_source_fbx"] = str(unresolved_fbx)
    unresolved_files = {}
    for role in ("color", "normal"):
        path = (
            unresolved_root
            / "texture"
            / f"T_Leaf_unresolved_local_09_{role}.png"
        )
        write_image(path)
        unresolved_files[role] = path
    write_stmat_sources(
        unresolved_fbx, unresolved_material.name, unresolved_files
    )
    unresolved_object = make_mesh_object(
        "UnresolvedLocalObject", [unresolved_material]
    )
    unresolved_contract = strict_runtime_contract(
        [make_intent(unresolved_material.name, 40)]
    )
    unresolved_preflight = core.preflight_speedtree_material_texture_contracts(
        [unresolved_object],
        unresolved_contract,
        source_fbx_override=str(unresolved_fbx),
    )
    unresolved_binding = unresolved_preflight["texture_contract"]["bindings"][0]
    assert unresolved_binding["status"] == "unassigned", unresolved_binding
    assert unresolved_binding["allow_local_search"] is True, unresolved_binding
    unresolved_result = core.normalize_speedtree_material_textures(
        [unresolved_object],
        texture_contract=unresolved_preflight["texture_contract"],
    )
    unresolved_row = unresolved_result["materials"][0]
    assert unresolved_row["status"] == "partial", unresolved_row
    assert set(unresolved_row["available_roles"]) == {
        "color",
        "normal",
    }, unresolved_row

    # Preview-only receipt authority is quarantined, then an independent live
    # local set is allowed to satisfy the runtime handoff.
    preview_root = root / "preview_quarantine_local"
    preview_fbx = preview_root / "fbx" / "SK_PreviewLocal.fbx"
    preview_fbx.parent.mkdir(parents=True)
    preview_fbx.write_bytes(b"fixture")
    preview_material = bpy.data.materials.new(
        "M_Leaf_preview_quarantine_10"
    )
    preview_material["codex_source_fbx"] = str(preview_fbx)
    preview_local_files = {}
    for role in ("color", "normal"):
        path = (
            preview_root
            / "texture"
            / f"T_Leaf_preview_quarantine_10_{role}.png"
        )
        write_image(path)
        preview_local_files[role] = path
    write_stmat_sources(
        preview_fbx, preview_material.name, preview_local_files
    )
    preview_cache_color = root / "preview_cache" / "Preview_color.png"
    write_image(preview_cache_color)
    preview_intent = make_intent(
        preview_material.name,
        41,
        files={"color": preview_cache_color},
    )
    preview_binding = preview_intent["texture_binding"]
    preview_binding.update(
        {
            "status": "ok",
            "origin_state": "canonical_t",
            "texture_contract_status": core.ATLAS_CANONICAL_TEXTURE_STATUS,
            "origin_receipt": {
                core.PREVIEW_ROLE_FALLBACKS_FIELD: [
                    {"usage": core.PREVIEW_ONLY_USAGE}
                ]
            },
        }
    )
    preview_object = make_mesh_object(
        "PreviewQuarantineLocalObject", [preview_material]
    )
    preview_contract = strict_runtime_contract([preview_intent])
    preview_preflight = core.preflight_speedtree_material_texture_contracts(
        [preview_object],
        preview_contract,
        source_fbx_override=str(preview_fbx),
    )
    assert "preview_receipt_not_production_capable" in {
        row["code"] for row in preview_preflight["diagnostics"]
    }, preview_preflight
    preview_quarantine = preview_preflight["texture_contract"]["bindings"][0]
    assert preview_quarantine["status"] == "unassigned", preview_quarantine
    assert preview_quarantine["allow_local_search"] is True, preview_quarantine
    preview_result = core.normalize_speedtree_material_textures(
        [preview_object], texture_contract=preview_preflight["texture_contract"]
    )
    preview_row = preview_result["materials"][0]
    assert preview_row["status"] == "partial", preview_row
    assert set(preview_row["available_roles"]) == {
        "color",
        "normal",
    }, preview_row

    # A malformed authoritative manifest invalidates its stale cached binding,
    # but remains an informational texture diagnostic at the runtime boundary.
    stale_root = root / "stale_manifest"
    stale_fbx = stale_root / "fbx" / "SK_Stale.fbx"
    stale_fbx.parent.mkdir(parents=True)
    stale_fbx.write_bytes(b"fixture")
    stale_fbx.with_suffix(".stmat").write_text(
        "<Materials />", encoding="utf-8"
    )
    manifest_path = stale_root / "speedtree_import_manifest.json"
    manifest_path.write_text("{malformed", encoding="utf-8")
    stale_material = bpy.data.materials.new("M_Leaf_stale_manifest_06")
    stale_material["codex_source_fbx"] = str(stale_fbx)
    stale_object = make_mesh_object("StaleManifestObject", [stale_material])
    orphan_color = root / "orphan" / "T_Stale_color.png"
    write_image(orphan_color)
    stale_binding = {
        "material": stale_material.name,
        "material_key": core._speedtree_material_name_key(stale_material.name),
        "production_group_base": stale_material.name,
        "status": "ok",
        "binding_disposition": "bind_available",
        "texture_source_mode": "managed_texture_set",
        "texture_contract_status": core.ATLAS_CANONICAL_TEXTURE_STATUS,
        "manifest_path": str(manifest_path),
        "texture_base": "T_Stale",
        "texture_dir": str(orphan_color.parent),
        "files": {"color": str(orphan_color)},
        "available_roles": ["color"],
        "missing_roles": sorted(
            set(core.SPEEDTREE_TEXTURE_ROLES) - {"color"}
        ),
    }
    stale_contract = core._bat_runtime_texture_contract(
        {"bindings": [stale_binding]}
    )
    stale_preflight = core.preflight_speedtree_material_texture_contracts(
        [stale_object],
        stale_contract,
        source_fbx_override=str(stale_fbx),
    )
    assert stale_preflight["status"] == "ok", stale_preflight
    assert stale_preflight["blocking"] == [], stale_preflight
    assert "atlas_manifest_binding_rejected" in {
        row["code"] for row in stale_preflight["diagnostics"]
    }, stale_preflight
    quarantined = stale_preflight["texture_contract"]["bindings"][0]
    assert quarantined["status"] == "unassigned", quarantined
    assert quarantined.get("files") == {}, quarantined
    stale_result = core.normalize_speedtree_material_textures(
        [stale_object], texture_contract=stale_preflight["texture_contract"]
    )
    assert stale_result["materials"][0]["status"] == "unassigned", stale_result
    assert not any(
        node.type == "TEX_IMAGE" for node in stale_material.node_tree.nodes
    )

    # The same malformed optional manifest must not crash the legacy
    # consolidation pass or revive name-only suffix merging.
    legacy_names = (
        "M_Leaf_malformed_manifest_07_green",
        "M_Leaf_malformed_manifest_07_dead",
    )
    legacy_materials = [bpy.data.materials.new(name) for name in legacy_names]
    for material in legacy_materials:
        material["codex_source_fbx"] = str(stale_fbx)
    legacy_object = make_mesh_object(
        "MalformedManifestConsolidationObject", legacy_materials
    )
    legacy_result = core.consolidate_speedtree_group_materials(
        [legacy_object],
        texture_contract=core._bat_runtime_texture_contract(None),
    )
    assert material_names(legacy_object) == list(legacy_names), legacy_result
    assert legacy_result["status"] == "skipped", legacy_result

    # Duplicate canonical datablocks from one source and one file signature
    # are one consensus candidate, not an artificial ambiguity.
    consensus_root = root / "rebind_consensus"
    consensus_fbx = consensus_root / "fbx" / "SK_Consensus.fbx"
    consensus_fbx.parent.mkdir(parents=True)
    consensus_fbx.write_bytes(b"fixture")
    consensus_fbx.with_suffix(".stmat").write_text(
        "<Materials />", encoding="utf-8"
    )
    consensus_base = "T_Leaf_rebind_consensus_08"
    consensus_files = {}
    for role in ("color", "normal"):
        path = consensus_root / "texture" / f"{consensus_base}_{role}.png"
        write_image(path)
        consensus_files[role] = path
    consensus_identity = str(consensus_root / "SK_Consensus.spm")
    canonical_candidates = [
        bpy.data.materials.new("M_Leaf_rebind_consensus_08"),
        bpy.data.materials.new("M_Leaf_rebind_consensus_08"),
    ]
    for material in canonical_candidates:
        material["codex_source_fbx"] = str(consensus_fbx)
        material["codex_source_identity"] = consensus_identity
        material["codex_speedtree_texture_base"] = consensus_base

    unsafe_fbx = (
        consensus_root
        / ".sk_batch_isolated_bark"
        / "hash"
        / "fbx"
        / "SK_Consensus.fbx"
    )
    unsafe_fbx.parent.mkdir(parents=True)
    unsafe_fbx.write_bytes(b"isolated")
    unsafe_image_path = unsafe_fbx.parent / "isolated_color.png"
    write_image(unsafe_image_path)

    # The operational batch intentionally imports its FBX from the isolated
    # bark workspace after preflight has rebound the material to production
    # T_* images.  Source provenance alone must not make those safe images
    # unassigned (the Weeping Willow regression).
    safe_isolated_source = bpy.data.materials.new(
        "M_Bark_safe_isolated_source_13"
    )
    safe_isolated_source.use_nodes = True
    safe_isolated_source["codex_source_fbx"] = str(unsafe_fbx)
    safe_isolated_source["codex_source_identity"] = str(
        consensus_root / "SK_SafeIsolatedSource.spm"
    )
    safe_isolated_source["codex_speedtree_texture_base"] = consensus_base
    safe_source_node = safe_isolated_source.node_tree.nodes.new(
        "ShaderNodeTexImage"
    )
    safe_source_node.image = bpy.data.images.load(
        str(consensus_files["color"])
    )
    safe_source_object = make_mesh_object(
        "SafeIsolatedSourceObject", [safe_isolated_source]
    )
    safe_source_signature = core.material_texture_signature(
        safe_isolated_source
    )
    safe_source_result = core.rebind_blocked_speedtree_group_variants(
        [safe_source_object]
    )
    assert safe_source_result["status"] == "ok", safe_source_result
    assert safe_source_result["texture_outcome"] == "complete", (
        safe_source_result
    )
    assert safe_source_result["materials"] == [], safe_source_result
    assert (
        core.material_texture_signature(safe_isolated_source)
        == safe_source_signature
    )
    assert safe_isolated_source["codex_source_fbx"] == str(unsafe_fbx)

    unsafe_variant = bpy.data.materials.new(
        "M_Leaf_rebind_consensus_08_green"
    )
    unsafe_variant.use_nodes = True
    unsafe_variant["codex_source_fbx"] = str(unsafe_fbx)
    unsafe_node = unsafe_variant.node_tree.nodes.new("ShaderNodeTexImage")
    unsafe_node.image = bpy.data.images.load(str(unsafe_image_path))
    unsafe_variant_object = make_mesh_object(
        "ConsensusRebindObject", [unsafe_variant]
    )
    consensus_result = core.rebind_blocked_speedtree_group_variants(
        [unsafe_variant_object]
    )
    assert consensus_result["status"] == "ok", consensus_result
    assert consensus_result["texture_outcome"] == "partial", consensus_result
    assert len(consensus_result["materials"]) == 1, consensus_result
    consensus_row = consensus_result["materials"][0]
    assert consensus_row["status"] == "partial", consensus_row
    assert consensus_row["canonical_material"] in {
        material.name for material in canonical_candidates
    }, consensus_row
    assert unsafe_variant["codex_source_fbx"] == str(consensus_fbx)
    assert {
        Path(path).name.casefold()
        for path in core.material_texture_signature(unsafe_variant)
    } == {path.name.casefold() for path in consensus_files.values()}

    # A genuinely different safe signature under the same source identity is
    # still ambiguous and must remain unassigned.
    conflict_base = "T_Leaf_rebind_consensus_08_conflict"
    conflict_files = {}
    for role in ("color", "normal"):
        path = consensus_root / "texture" / f"{conflict_base}_{role}.png"
        write_image(path)
        conflict_files[role] = path
    conflict_candidate = bpy.data.materials.new(
        "M_Leaf_rebind_consensus_08"
    )
    conflict_candidate["codex_source_fbx"] = str(consensus_fbx)
    conflict_candidate["codex_source_identity"] = consensus_identity
    conflict_candidate["codex_speedtree_texture_base"] = conflict_base
    unsafe_conflict = bpy.data.materials.new(
        "M_Leaf_rebind_consensus_08_dead"
    )
    unsafe_conflict.use_nodes = True
    unsafe_conflict["codex_source_fbx"] = str(unsafe_fbx)
    unsafe_conflict_node = unsafe_conflict.node_tree.nodes.new(
        "ShaderNodeTexImage"
    )
    unsafe_conflict_node.image = bpy.data.images.load(str(unsafe_image_path))
    unsafe_conflict_object = make_mesh_object(
        "ConflictingConsensusObject", [unsafe_conflict]
    )
    conflict_result = core.rebind_blocked_speedtree_group_variants(
        [unsafe_conflict_object]
    )
    assert conflict_result["status"] == "ok", conflict_result
    assert conflict_result["texture_outcome"] == "unassigned", conflict_result
    assert len(conflict_result["materials"]) == 1, conflict_result
    conflict_row = conflict_result["materials"][0]
    assert conflict_row["status"] == "unassigned", conflict_row
    assert conflict_row["candidate_count"] == 2, conflict_row
    assert (
        conflict_row["raw_candidate_count"]
        > conflict_row["candidate_count"]
    ), conflict_row
    assert not any(
        node.type == "TEX_IMAGE" for node in unsafe_conflict.node_tree.nodes
    )

    # Safe FBX ownership cannot launder a blocked/cache STMAT texture path into
    # a canonical rebind candidate.
    blocked_candidate_root = root / "blocked_candidate"
    blocked_candidate_fbx = (
        blocked_candidate_root / "fbx" / "SK_BlockedCandidate.fbx"
    )
    blocked_candidate_fbx.parent.mkdir(parents=True)
    blocked_candidate_fbx.write_bytes(b"fixture")
    blocked_candidate_base = "T_Leaf_blocked_candidate_12"
    blocked_candidate_color = (
        blocked_candidate_root
        / ".sk_batch_isolated_bark"
        / "cache"
        / "texture"
        / f"{blocked_candidate_base}_color.png"
    )
    write_image(blocked_candidate_color)
    blocked_stmat_root = ET.Element("Materials")
    blocked_stmat_material = ET.SubElement(
        blocked_stmat_root,
        "Material",
        Name="M_Leaf_blocked_candidate_12_Mat",
    )
    ET.SubElement(
        blocked_stmat_material,
        "Map",
        Name="Color",
        Source=str(blocked_candidate_color),
    )
    ET.ElementTree(blocked_stmat_root).write(
        blocked_candidate_fbx.with_suffix(".stmat"),
        encoding="utf-8",
        xml_declaration=True,
    )
    blocked_canonical = bpy.data.materials.new(
        "M_Leaf_blocked_candidate_12"
    )
    blocked_canonical["codex_source_fbx"] = str(blocked_candidate_fbx)
    blocked_canonical["codex_speedtree_texture_base"] = (
        blocked_candidate_base
    )
    blocked_target = bpy.data.materials.new(
        "M_Leaf_blocked_candidate_12_green"
    )
    blocked_target.use_nodes = True
    blocked_target["codex_source_fbx"] = str(blocked_candidate_fbx)
    blocked_target_node = blocked_target.node_tree.nodes.new(
        "ShaderNodeTexImage"
    )
    blocked_target_node.image = bpy.data.images.load(
        str(blocked_candidate_color)
    )
    blocked_target_object = make_mesh_object(
        "BlockedCandidateObject", [blocked_target]
    )
    blocked_candidate_result = core.rebind_blocked_speedtree_group_variants(
        [blocked_target_object]
    )
    assert blocked_candidate_result["status"] == "ok", blocked_candidate_result
    assert blocked_candidate_result["texture_outcome"] == "unassigned", (
        blocked_candidate_result
    )
    blocked_candidate_row = blocked_candidate_result["materials"][0]
    assert blocked_candidate_row["status"] == "unassigned", blocked_candidate_row
    assert blocked_candidate_row["candidate_count"] == 0, blocked_candidate_row
    assert not any(
        node.type == "TEX_IMAGE" for node in blocked_target.node_tree.nodes
    )

    # Extension preference is not a one-shot admission decision. If the
    # preferred TGA cannot decode, the next same-role local candidate is used.
    decode_root = root / "decode_retry"
    decode_fbx = decode_root / "fbx" / "SK_DecodeRetry.fbx"
    decode_fbx.parent.mkdir(parents=True)
    decode_fbx.write_bytes(b"fixture")
    decode_fbx.with_suffix(".stmat").write_text(
        "<Materials />", encoding="utf-8"
    )
    decode_material = bpy.data.materials.new("M_Leaf_decode_retry_11")
    decode_material["codex_source_fbx"] = str(decode_fbx)
    decode_texture_dir = decode_root / "texture"
    decode_texture_dir.mkdir(parents=True)
    corrupt_preferred = (
        decode_texture_dir / "T_Leaf_decode_retry_11_color.tga"
    )
    corrupt_preferred.write_bytes(b"not-a-decodable-image")
    valid_color = decode_texture_dir / "T_Leaf_decode_retry_11_color.png"
    valid_normal = decode_texture_dir / "T_Leaf_decode_retry_11_normal.png"
    write_image(valid_color)
    write_image(valid_normal)
    decode_object = make_mesh_object("DecodeRetryObject", [decode_material])
    decode_result = core.normalize_speedtree_material_textures(
        [decode_object],
        texture_contract=core._bat_runtime_texture_contract(None),
    )
    decode_row = decode_result["materials"][0]
    assert decode_result["status"] == "ok", decode_result
    assert decode_result["warnings"] == [], decode_result
    assert decode_row["status"] == "partial", decode_row
    assert set(decode_row["available_roles"]) == {"color", "normal"}
    assert Path(decode_row["files"]["color"]).resolve() == valid_color.resolve()
    assert {
        Path(path).resolve()
        for path in core.material_texture_signature(decode_material)
    } == {valid_color.resolve(), valid_normal.resolve()}

    # A scan-authored trunk can use M_tree_*/M_bark_* names rather than the
    # branch/leaf conventions. The independent geometry gate catches any
    # authored face that is missing from the assembled mesh.
    trunk_material = bpy.data.materials.new("M_tree_hybrid_scan_01")
    stitch_material = bpy.data.materials.new("M_tree_hybrid_scan_stitch_01")
    foreign_material = bpy.data.materials.new("M_unrelated_scene_mesh")
    hybrid_trunk = make_mesh_object("M_tree_hybrid_scan_01", [trunk_material])
    hybrid_stitch = make_mesh_object(
        "M_tree_hybrid_scan_stitch_01", [stitch_material]
    )
    unrelated = make_mesh_object("UnrelatedSceneMesh", [foreign_material])
    for obj in (hybrid_trunk, hybrid_stitch, unrelated):
        obj.data.uv_layers.new(name="uv0")
        obj.data.uv_layers.new(name="blend_ao")

    hybrid_output = hybrid_trunk.copy()
    hybrid_output.data = hybrid_trunk.data.copy()
    hybrid_output.name = "HybridAssembledGeometry"
    bpy.context.scene.collection.objects.link(hybrid_output)
    coverage = core.validate_source_geometry_coverage(
        [hybrid_trunk], hybrid_output
    )
    assert coverage["status"] == "ok", coverage
    assert coverage["expected_faces"] == len(hybrid_trunk.data.polygons)
    hybrid_output.data.materials[0] = stitch_material
    material_drift = core.validate_source_geometry_coverage(
        [hybrid_trunk], hybrid_output
    )
    assert material_drift["status"] == "ok", material_drift
    assert material_drift["material_histogram_status"] == "diagnostic_drift", (
        material_drift
    )
    try:
        core.validate_source_geometry_coverage(
            [hybrid_trunk, hybrid_stitch], hybrid_output
        )
    except RuntimeError as exc:
        assert "M_tree_hybrid_scan_stitch_01" in str(exc), exc
    else:
        raise AssertionError("Missing hybrid stitch geometry was not blocked")
    assert unrelated.hide_render is False

print("RUNTIME_STRUCTURAL_REGRESSION_SMOKE_OK")
