"""Blender smoke checks for pre-repair Default/empty face cleanup."""

from __future__ import annotations

import hashlib
import sys
import tempfile
from pathlib import Path

import bpy


REPO_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_DIR / "addons"))

from speedtree_bone_weight_repair import core


def mesh_object(name, materials, *, face_material_indices=None):
    mesh = bpy.data.meshes.new(name + "Mesh")
    if face_material_indices is None:
        face_material_indices = [
            min(index, len(materials) - 1) for index in range(2)
        ]
    face_count = len(face_material_indices)
    vertices = [(0.0, 0.0, 0.0)] + [
        (
            float(index + 1),
            float((index + 1) % 2),
            0.0,
        )
        for index in range(face_count + 1)
    ]
    faces = [
        (0, index + 1, index + 2) for index in range(face_count)
    ]
    mesh.from_pydata(
        vertices,
        [],
        faces,
    )
    for material in materials:
        mesh.materials.append(material)
    for polygon, material_index in zip(
        mesh.polygons, face_material_indices
    ):
        polygon.material_index = material_index
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


def contract(
    intents, *, runtime_tolerant=False, live_source_identity=None
):
    result = {
        "status": "ok",
        "strict_speedtree_pipeline_contract": True,
        "speedtree_pipeline_contract": {"material_intents": intents},
    }
    if live_source_identity is not None:
        result["live_source_identity"] = live_source_identity
        result["live_source_identity_validated"] = True
    if runtime_tolerant:
        result[core.handoff_contract.TEXTURE_CONTRACT_MODE_FIELD] = (
            core.handoff_contract.RUNTIME_TOLERANT_TEXTURE_MODE
        )
    return result


def validated_live_source_identity(
    root, *, spm_path=None, stmat_path=None
):
    def identity(path, payload):
        path.write_bytes(payload)
        return {
            "canonical_path": str(path.resolve()),
            "sha256": hashlib.sha256(payload).hexdigest(),
            "size": len(payload),
        }

    spm_path = Path(spm_path or root / "fixture.spm")
    stmat_path = Path(stmat_path or root / "fbx" / "fixture.stmat")
    stmat_path.parent.mkdir(parents=True, exist_ok=True)
    spm_payload = (
        b'<SpeedTreeModel><Generator Type="Tree" /></SpeedTreeModel>'
    )
    return {
        "spm": identity(spm_path, spm_payload),
        "stmat": [
            identity(stmat_path, b"live stmat fixture")
        ],
    }


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
    live_identity = validated_live_source_identity(texture_dir)
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
    default_material.use_nodes = True
    default_image_node = default_material.node_tree.nodes.new(
        "ShaderNodeTexImage"
    )
    default_image_node.image = bpy.data.images.new(
        "AuthoredDefaultStillPolicyOwned", width=1, height=1
    )
    bark_material = bpy.data.materials.new("M_bark_safe")
    bark_material[core.UNREAL_TREE_PART_PROPERTY] = "bark"
    strict_contract = contract(
        [default_intent, bark_intent],
        live_source_identity=live_identity,
    )
    live_spm_path = live_identity["spm"]["canonical_path"]
    fixture_source_fbx = str(
        Path(live_identity["stmat"][0]["canonical_path"]).with_suffix(
            ".fbx"
        )
    )

    def remove_unassigned_geometry(
        objects, texture_contract=strict_contract, **kwargs
    ):
        kwargs.setdefault("source_fbx_path", fixture_source_fbx)
        kwargs.setdefault("source_identity_path", live_spm_path)
        kwargs.setdefault("live_spm_path", live_spm_path)
        return core.remove_speedtree_unassigned_geometry(
            objects, texture_contract, **kwargs
        )

    def validate_assigned_materials(
        obj, texture_contract=strict_contract
    ):
        return core.validate_face_assigned_material_slots(
            obj,
            texture_contract=texture_contract,
            live_spm_path=live_spm_path,
            source_fbx_path=fixture_source_fbx,
        )

    # A mixed authored mesh loses only the exact Default face.  No bark is
    # guessed, and shared source mesh data remains untouched by copy-on-write.
    merged = mesh_object("Merged", [default_material, bark_material])
    merged.data.uv_layers.new(name="UVMap")
    for loop_index, loop_uv in enumerate(merged.data.uv_layers["UVMap"].data):
        loop_uv.uv = (loop_index / 10.0, loop_index / 20.0)
    color_attribute = merged.data.color_attributes.new(
        name="SpeedTreeAO", type="FLOAT_COLOR", domain="CORNER"
    )
    for color_index, color in enumerate(color_attribute.data):
        color.color = (
            color_index / 10.0,
            color_index / 20.0,
            color_index / 30.0,
            1.0,
        )
    merged.data.normals_split_custom_set(
        [
            (-1.0, 0.0, 0.0),
            (0.0, -1.0, 0.0),
            (0.0, 0.0, -1.0),
            (1.0, 0.0, 0.0),
            (0.0, 1.0, 0.0),
            (0.0, 0.0, 1.0),
        ]
    )
    merged.data.update()
    bark_loop_indices = list(merged.data.polygons[1].loop_indices)
    bark_corner_payload_before = sorted(
        (
            tuple(
                round(float(value), 6)
                for value in merged.data.vertices[
                    merged.data.loops[index].vertex_index
                ].co
            ),
            tuple(
                round(float(value), 6)
                for value in merged.data.uv_layers["UVMap"].data[index].uv
            ),
            tuple(
                round(float(value), 6)
                for value in color_attribute.data[index].color
            ),
            tuple(
                round(float(value), 6)
                for value in merged.data.corner_normals[index].vector
            ),
        )
        for index in bark_loop_indices
    )
    deform_group = merged.vertex_groups.new(name="Bone_1")
    for vertex_index, weight in enumerate((0.11, 0.22, 0.33, 0.44)):
        deform_group.add([vertex_index], weight, "REPLACE")
    bark_weights_before = {
        tuple(round(float(value), 6) for value in merged.data.vertices[index].co):
        deform_group.weight(index)
        for index in merged.data.polygons[1].vertices
    }
    shared = bpy.data.objects.new("SharedBeforeCopy", merged.data)
    bpy.context.scene.collection.objects.link(shared)
    result = remove_unassigned_geometry(
        [merged],
        strict_contract,
    )
    assert result["status"] == "applied", result
    assert result["removed_face_count"] == 1, result
    assert result["removed_vertex_count"] == 1, result
    assert result["removed_object_count"] == 0, result
    assert Path(result["source_identity"]) == Path(live_spm_path)
    assert result["live_source_identity"] == live_identity
    assert result["live_source_identity_validated"] is True
    assert len(result["live_source_identity_fingerprint"]) == 64
    identity_fingerprint = result["live_source_identity_fingerprint"]
    assert merged.data is not shared.data
    assert len(merged.data.polygons) == 1
    assert list(merged.data.materials) == [bark_material]
    assert [poly.material_index for poly in merged.data.polygons] == [0]
    assert "UVMap" in merged.data.uv_layers
    assert len(merged.data.uv_layers["UVMap"].data) == 3
    assert "SpeedTreeAO" in merged.data.color_attributes
    assert len(merged.data.color_attributes["SpeedTreeAO"].data) == 3
    bark_corner_payload_after = sorted(
        (
            tuple(
                round(float(value), 6)
                for value in merged.data.vertices[
                    merged.data.loops[index].vertex_index
                ].co
            ),
            tuple(
                round(float(value), 6)
                for value in merged.data.uv_layers["UVMap"].data[index].uv
            ),
            tuple(
                round(float(value), 6)
                for value in merged.data.color_attributes[
                    "SpeedTreeAO"
                ].data[index].color
            ),
            tuple(
                round(float(value), 6)
                for value in merged.data.corner_normals[index].vector
            ),
        )
        for index in merged.data.polygons[0].loop_indices
    )
    assert merged.data.has_custom_normals
    assert bark_corner_payload_after == bark_corner_payload_before
    deform_group = merged.vertex_groups["Bone_1"]
    for vertex in merged.data.vertices:
        coordinate = tuple(
            round(float(value), 6) for value in vertex.co
        )
        assert abs(
            deform_group.weight(vertex.index)
            - bark_weights_before[coordinate]
        ) < 1e-6
    assert len(shared.data.polygons) == 2
    assert list(shared.data.materials) == [default_material, bark_material]
    unchanged_result = remove_unassigned_geometry(
        [merged], strict_contract
    )
    assert unchanged_result["status"] == "not_applicable"
    assert (
        unchanged_result["live_source_identity_fingerprint"]
        == identity_fingerprint
    )

    # Actual None assignments are the same disposable input class.
    none_merged = mesh_object("NoneMerged", [None, bark_material])
    none_result = remove_unassigned_geometry(
        [none_merged], strict_contract
    )
    assert none_result["status"] == "applied", none_result
    assert none_result["removed_face_count"] == 1, none_result
    assert len(none_merged.data.polygons) == 1
    assert list(none_merged.data.materials) == [bark_material]
    assert validate_assigned_materials(none_merged)[
        "status"
    ] == "ok"

    edit_mode_mesh = mesh_object(
        "EditModeCleanup", [default_material, bark_material]
    )
    bpy.ops.object.select_all(action="DESELECT")
    edit_mode_mesh.select_set(True)
    bpy.context.view_layer.objects.active = edit_mode_mesh
    bpy.ops.object.mode_set(mode="EDIT")
    edit_mode_result = remove_unassigned_geometry(
        [edit_mode_mesh], strict_contract
    )
    assert edit_mode_result["removed_face_count"] == 1, edit_mode_result
    assert bpy.context.mode == "OBJECT"
    assert len(edit_mode_mesh.data.polygons) == 1

    # A Force/anchor object made entirely from Default geometry is removed.
    all_default = mesh_object("AllDefaultDummy", [default_material])
    all_default.location = (4.0, -3.0, 2.0)
    all_default.rotation_euler = (0.2, -0.3, 0.4)
    # Uniform parent scale avoids an unrepresentable shear when the child is
    # unparented, while still exercising translation/rotation/scale handling.
    all_default.scale = (1.5, 1.5, 1.5)
    dummy_child = bpy.data.objects.new("AllDefaultDummyChild", None)
    bpy.context.scene.collection.objects.link(dummy_child)
    dummy_child.parent = all_default
    dummy_child.location = (1.0, 2.0, -1.0)
    dummy_child.rotation_euler = (-0.1, 0.5, 0.25)
    dummy_child.scale = (0.8, 1.2, 1.1)
    bpy.context.view_layer.update()
    child_world_before = dummy_child.matrix_world.copy()
    all_default_result = remove_unassigned_geometry(
        [all_default], strict_contract
    )
    assert all_default_result["removed_object_count"] == 1, all_default_result
    assert all_default_result["removed_face_count"] == 2, all_default_result
    assert bpy.data.objects.get("AllDefaultDummy") is None
    bpy.context.view_layer.update()
    assert dummy_child.parent is None
    assert max(
        abs(
            float(dummy_child.matrix_world[row][column])
            - float(child_world_before[row][column])
        )
        for row in range(4)
        for column in range(4)
    ) < 1e-5

    no_material_slots = mesh_object("NoMaterialSlots", [])
    no_slots_result = remove_unassigned_geometry(
        [no_material_slots], strict_contract
    )
    assert no_slots_result["removed_object_count"] == 1, no_slots_result
    assert no_slots_result["removed_face_count"] == 2, no_slots_result
    assert no_slots_result["objects"][0]["removed_face_reasons"] == {
        "no_material_slots": 2
    }
    assert bpy.data.objects.get("NoMaterialSlots") is None

    # A wire/point anchor has no renderable faces but can still contaminate
    # bounds used by rigid fallback, so it is also removed at this boundary.
    wire_mesh = bpy.data.meshes.new("ZeroFaceAnchorMesh")
    wire_mesh.from_pydata(
        [(0.0, 0.0, 0.0), (1000.0, 0.0, 0.0)],
        [(0, 1)],
        [],
    )
    wire_anchor = bpy.data.objects.new("ZeroFaceAnchor", wire_mesh)
    bpy.context.scene.collection.objects.link(wire_anchor)
    wire_result = remove_unassigned_geometry(
        [wire_anchor], strict_contract
    )
    assert wire_result["removed_object_count"] == 1, wire_result
    assert wire_result["removed_face_count"] == 0, wire_result
    assert wire_result["removed_edge_count"] == 1, wire_result
    assert wire_result["removed_vertex_count"] == 2, wire_result
    assert wire_result["objects"][0]["object_removal_reason"] == (
        "zero_face_non_render_geometry"
    )
    assert bpy.data.objects.get("ZeroFaceAnchor") is None

    # Multiple or missing bark candidates no longer create a decision gate.
    second_binding = ready_binding(texture_dir, "T_bark_other")
    second_material = bpy.data.materials.new("M_bark_other")
    second_material[core.UNREAL_TREE_PART_PROPERTY] = "bark"
    second_intent = intent(
        api,
        2,
        "M_bark_other_Mat",
        tree_part="bark",
        mode="managed_texture_set",
        binding=second_binding,
    )
    multiple_bark = mesh_object(
        "MultipleBarkCandidates",
        [default_material, bark_material, second_material],
        face_material_indices=[0, 1, 2],
    )
    multiple_result = remove_unassigned_geometry(
        [multiple_bark],
        contract(
            [default_intent, bark_intent, second_intent],
            live_source_identity=live_identity,
        ),
    )
    assert multiple_result["status"] == "applied", multiple_result
    assert len(multiple_bark.data.polygons) == 2
    assert list(multiple_bark.data.materials) == [
        bark_material,
        second_material,
    ]
    assert [
        polygon.material_index for polygon in multiple_bark.data.polygons
    ] == [0, 1]

    missing_bark = mesh_object("MissingBarkCandidate", [default_material])
    missing_result = remove_unassigned_geometry(
        [missing_bark],
        contract([default_intent], live_source_identity=live_identity),
    )
    assert missing_result["removed_object_count"] == 1, missing_result
    assert bpy.data.objects.get("MissingBarkCandidate") is None

    # Exact canonical Default is policy-owned even if it has nodes.  Similar
    # production names are not the exact placeholder key and remain authored.
    similar_material = bpy.data.materials.new("M_Default_Bark")
    similar = mesh_object("SimilarDefaultName", [similar_material])
    similar_result = remove_unassigned_geometry(
        [similar], strict_contract
    )
    assert similar_result["status"] == "not_applicable", similar_result
    assert len(similar.data.polygons) == 2
    assert list(similar.data.materials) == [similar_material]

    assert core._is_exact_speedtree_default_placeholder("Default")
    assert core._is_exact_speedtree_default_placeholder("default_mat")
    assert core._is_exact_speedtree_default_placeholder("Default.001")
    assert core._is_exact_speedtree_default_placeholder("Default_Mat.927")
    assert not core._is_exact_speedtree_default_placeholder("De_fault")
    assert not core._is_exact_speedtree_default_placeholder("Default!")
    for authored_name in ("De_fault", "Default!"):
        authored_material = bpy.data.materials.new(authored_name)
        authored_object = mesh_object(
            "Authored" + authored_name.replace("!", "Bang"),
            [authored_material],
        )
        authored_result = remove_unassigned_geometry(
            [authored_object], strict_contract
        )
        assert authored_result["status"] == "not_applicable", (
            authored_name,
            authored_result,
        )
        assert len(authored_object.data.polygons) == 2
        assert list(authored_object.data.materials) == [authored_material]
        assert validate_assigned_materials(authored_object)["status"] == "ok"

    # An unused authored material is part of the source slot contract.  Only
    # the exact placeholder slot disappears when its face is discarded.
    unused_authored_material = bpy.data.materials.new("M_authored_unused")
    authored_unused = mesh_object(
        "AuthoredUnusedSlot",
        [default_material, bark_material, unused_authored_material],
        face_material_indices=[0, 1],
    )
    authored_unused_result = remove_unassigned_geometry(
        [authored_unused], strict_contract
    )
    assert authored_unused_result["removed_face_count"] == 1, (
        authored_unused_result
    )
    assert list(authored_unused.data.materials) == [
        bark_material,
        unused_authored_material,
    ]
    assert [
        polygon.material_index for polygon in authored_unused.data.polygons
    ] == [0]

    # Legacy/missing provenance is a pass-through, not authority to delete.
    no_identity_contract = contract([default_intent, bark_intent])
    legacy_default = mesh_object(
        "LegacyDefaultPassThrough", [default_material, bark_material]
    )
    legacy_mesh_pointer = legacy_default.data.as_pointer()
    legacy_materials_before = list(legacy_default.data.materials)
    legacy_faces_before = [
        tuple(polygon.vertices) for polygon in legacy_default.data.polygons
    ]
    legacy_result = remove_unassigned_geometry(
        [legacy_default], no_identity_contract
    )
    assert legacy_result["status"] == "not_applicable", legacy_result
    assert legacy_result["reason"] == (
        "validated_live_spm_stmat_identity_unavailable"
    )
    assert not legacy_result["cleanup_authorized"]
    assert legacy_result["live_source_identity"] == {}
    assert legacy_result["live_source_identity_validated"] is False
    assert legacy_default.data.as_pointer() == legacy_mesh_pointer
    assert list(legacy_default.data.materials) == legacy_materials_before
    assert [
        tuple(polygon.vertices) for polygon in legacy_default.data.polygons
    ] == legacy_faces_before
    legacy_validation = core.validate_face_assigned_material_slots(
        legacy_default, texture_contract=no_identity_contract
    )
    assert legacy_validation["status"] == "ok", legacy_validation
    assert not legacy_validation["placeholder_cleanup_authorized"]

    # A valid envelope from another asset cannot authorize this FBX mutation.
    identity_mismatch = mesh_object(
        "IdentityMismatchDefault", [default_material, bark_material]
    )
    mismatch_result = remove_unassigned_geometry(
        [identity_mismatch],
        strict_contract,
        source_fbx_path=str(texture_dir / "other_asset.fbx"),
    )
    assert mismatch_result["status"] == "not_applicable", mismatch_result
    assert mismatch_result["cleanup_authorized"] is False
    assert len(identity_mismatch.data.polygons) == 2
    assert list(identity_mismatch.data.materials) == [
        default_material,
        bark_material,
    ]

    legacy_empty = mesh_object("LegacyConcreteEmpty", [None, bark_material])
    try:
        core.validate_face_assigned_material_slots(
            legacy_empty, texture_contract=no_identity_contract
        )
    except RuntimeError as exc:
        assert "empty_slots" in str(exc), exc
    else:
        raise AssertionError("legacy face-assigned None slot was accepted")

    legacy_out_of_range = mesh_object(
        "LegacyConcreteOutOfRange", [bark_material]
    )
    legacy_out_of_range.data.polygons[0].material_index = 7
    try:
        core.validate_face_assigned_material_slots(
            legacy_out_of_range, texture_contract=no_identity_contract
        )
    except RuntimeError as exc:
        assert "empty_slots=[7]" in str(exc), exc
    else:
        raise AssertionError("legacy out-of-range face assignment was accepted")

    # Cleanup is a boundary operation, while the final validator remains a
    # postcondition: a later stage must not be able to reintroduce Default.
    post_cleanup_default = mesh_object(
        "PostCleanupDefault", [bark_material]
    )
    assert remove_unassigned_geometry(
        [post_cleanup_default], strict_contract
    )["status"] == "not_applicable"
    post_cleanup_default.data.materials.append(default_material)
    post_cleanup_default.data.polygons[0].material_index = 1
    try:
        validate_assigned_materials(post_cleanup_default)
    except RuntimeError as exc:
        assert "canonical_default_slots" in str(exc), exc
    else:
        raise AssertionError("final validator accepted reintroduced Default")

    unused_none = mesh_object("UnusedNone", [bark_material, None])
    for polygon in unused_none.data.polygons:
        polygon.material_index = 0
    unused_none_result = remove_unassigned_geometry(
        [unused_none], strict_contract
    )
    assert unused_none_result["status"] == "applied", unused_none_result
    assert unused_none_result["removed_face_count"] == 0
    assert unused_none_result["removed_material_slot_count"] == 1
    assert list(unused_none.data.materials) == [bark_material]

    out_of_range = mesh_object("OutOfRangeSlot", [bark_material])
    out_of_range.data.polygons[0].material_index = 7
    out_of_range_result = remove_unassigned_geometry(
        [out_of_range], strict_contract
    )
    assert out_of_range_result["removed_face_count"] == 1, (
        out_of_range_result
    )
    assert len(out_of_range.data.polygons) == 1
    assert validate_assigned_materials(out_of_range)[
        "status"
    ] == "ok"

    # Exercise the real FBX entry point: a far-away Default helper must be
    # gone before rigid fallback computes the armature bounds.
    import_default = bpy.data.materials.new("Default_ImportFixture")
    import_default.name = "Default"
    import_bark = bpy.data.materials.new("M_bark_import_fixture")
    import_dummy = mesh_object("ImportDefaultDummy", [import_default])
    import_dummy.location.x = 1000.0
    import_real = mesh_object("ImportRealGeometry", [import_bark])
    bpy.ops.object.select_all(action="DESELECT")
    import_dummy.select_set(True)
    import_real.select_set(True)
    bpy.context.view_layer.objects.active = import_real
    integration_fbx = texture_dir / "import_cleanup_fixture.fbx"
    bpy.ops.export_scene.fbx(
        filepath=str(integration_fbx),
        use_selection=True,
        add_leaf_bones=False,
        bake_anim=False,
    )
    core.remove_object_and_orphan_mesh(import_dummy)
    core.remove_object_and_orphan_mesh(import_real)
    integration_spm = texture_dir / "import_cleanup_fixture.spm"
    integration_live_identity = validated_live_source_identity(
        texture_dir,
        spm_path=integration_spm,
        stmat_path=integration_fbx.with_suffix(".stmat"),
    )
    integration_bark_intent = intent(
        api,
        1,
        "M_bark_import_fixture_Mat",
        tree_part="bark",
        mode="unresolved",
        binding={"status": "unassigned", "files": {}},
    )
    original_preflight = core.preflight_speedtree_material_texture_contracts

    def assert_cleanup_precedes_preflight(objects, *args, **kwargs):
        for candidate in objects:
            if candidate.type != "MESH":
                continue
            materials = list(candidate.data.materials)
            assert all(
                not core._unassigned_face_reason(
                    materials, int(polygon.material_index)
                )
                for polygon in candidate.data.polygons
            ), candidate.name
        return original_preflight(objects, *args, **kwargs)

    core.preflight_speedtree_material_texture_contracts = (
        assert_cleanup_precedes_preflight
    )
    try:
        integration_result = core.run_import_source_fbx(
            str(integration_fbx),
            source_collection_name="ImportCleanupFixture",
            rigid_fallback=True,
            texture_contract=contract(
                [default_intent, integration_bark_intent],
                runtime_tolerant=True,
                live_source_identity=integration_live_identity,
            ),
            spm_path=str(integration_spm),
            source_identity_path=str(integration_spm),
        )
    finally:
        core.preflight_speedtree_material_texture_contracts = (
            original_preflight
        )
    integration_cleanup = integration_result[
        "unassigned_geometry_cleanup"
    ]
    assert integration_cleanup["removed_object_count"] == 1, (
        integration_cleanup
    )
    assert integration_cleanup["removed_face_count"] == 2, (
        integration_cleanup
    )
    rigid_result = integration_result["rigid_fallback"]
    assert rigid_result is not None, integration_result
    fallback_armature = bpy.data.objects[rigid_result["armature"]]
    fallback_bone = fallback_armature.data.bones[rigid_result["bone"]]
    assert abs(fallback_bone.head_local.x) < 100.0, (
        fallback_bone.head_local
    )

    empty_import_material = bpy.data.materials.new("Default")
    empty_import_object = mesh_object(
        "ImportOnlyDefault", [empty_import_material]
    )
    bpy.ops.object.select_all(action="DESELECT")
    empty_import_object.select_set(True)
    bpy.context.view_layer.objects.active = empty_import_object
    empty_integration_fbx = texture_dir / "all_default_fixture.fbx"
    bpy.ops.export_scene.fbx(
        filepath=str(empty_integration_fbx),
        use_selection=True,
        add_leaf_bones=False,
        bake_anim=False,
    )
    core.remove_object_and_orphan_mesh(empty_import_object)
    empty_integration_spm = texture_dir / "all_default_fixture.spm"
    empty_live_identity = validated_live_source_identity(
        texture_dir,
        spm_path=empty_integration_spm,
        stmat_path=empty_integration_fbx.with_suffix(".stmat"),
    )
    try:
        core.run_import_source_fbx(
            str(empty_integration_fbx),
            source_collection_name="AllDefaultImportFixture",
            rigid_fallback=True,
            texture_contract=contract(
                [default_intent],
                runtime_tolerant=True,
                live_source_identity=empty_live_identity,
            ),
            spm_path=str(empty_integration_spm),
            source_identity_path=str(empty_integration_spm),
        )
    except RuntimeError as exc:
        assert "no renderable geometry after removing" in str(exc), exc
    else:
        raise AssertionError("all-Default FBX reached rigid fallback")

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
