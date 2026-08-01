"""Focused Blender regression checks for physical Cluster bake manifests."""

from __future__ import annotations

import hashlib
import json
import sys
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path

import bpy


REPO_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_DIR / "addons"))

from speedtree_bone_weight_repair import core


def sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


bpy.ops.wm.read_factory_settings(use_empty=True)

with tempfile.TemporaryDirectory(
    prefix="bwr_cluster_bake_manifest_"
) as temp_root:
    asset_root = Path(temp_root) / "weed_test"
    source_fbx = asset_root / "fbx" / "SK_weed_test_01.fbx"
    source_fbx.parent.mkdir(parents=True)
    source_fbx.write_bytes(b"fbx")
    cluster_root = asset_root / "cluster"
    cluster_root.mkdir()

    material_name = "M_cluster_test_01"
    source_files = {}
    capture_maps = []
    for role, suffix in (
        ("albedo", ""),
        ("alpha", "_Opacity"),
        ("ao", "_AO"),
        ("gloss", "_Gloss"),
        ("height", "_Height"),
        ("normal", "_Normal"),
        ("subsurface_amount", "_SubsurfaceAmount"),
        ("translucency", "_Subsurface"),
    ):
        path = cluster_root / f"cluster_test_01{suffix}.tga"
        path.write_bytes(role.encode("ascii"))
        source_files[role] = str(path)
        capture_maps.append(
            {
                "role": role,
                "path": str(path),
                "sha256": sha256(path),
            }
        )

    origin_receipt = {
        "kind": core.ATLAS_CLUSTER_RECEIPT_KIND,
        "version": 1,
        "source_origin": core.ATLAS_BLENDER_CLUSTER_BAKE_STATUS,
        "material": material_name,
        "physical_capture_contract_sha256": hashlib.sha256(
            b"physical-capture-contract"
        ).hexdigest(),
        "source_roles": sorted(source_files),
        "capture_maps": capture_maps,
    }
    bake_contract = {
        "kind": "blender_cluster_bake_texture_contract",
        "version": 1,
        "texture_contract_status": (
            core.ATLAS_BLENDER_CLUSTER_BAKE_STATUS
        ),
        "material": material_name,
        "source_origin": core.ATLAS_BLENDER_CLUSTER_BAKE_STATUS,
        "files": source_files,
        "source_roles": sorted(source_files),
        "origin_receipt": origin_receipt,
        "warning": None,
        "remediation": None,
    }
    manifest = {
        "atlas_asset_name": material_name,
        "texture_contract_status": (
            core.ATLAS_BLENDER_CLUSTER_BAKE_STATUS
        ),
        "blender_cluster_bake_textures": [bake_contract],
        "material_groups": [
            {
                "material": material_name,
                "texture_contract_status": (
                    core.ATLAS_BLENDER_CLUSTER_BAKE_STATUS
                ),
                "blender_cluster_bake_texture": bake_contract,
            }
        ],
    }
    manifest_path = asset_root / "speedtree_import_manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, indent=2),
        encoding="utf-8",
    )

    entries = core._manifest_texture_entries(manifest)
    assert len(entries) == 1, entries
    assert (
        entries[0]["status"]
        == core.ATLAS_BLENDER_CLUSTER_BAKE_STATUS
    ), entries

    owner = bpy.data.materials.new(material_name + "_Mat")
    owner_binding = core._speedtree_manifest_texture_binding(
        source_fbx,
        owner,
        stmat_data={"materials": {}},
        manifest_cache={},
    )
    assert owner_binding is not None, owner_binding
    assert owner_binding["status"] == "ok", owner_binding
    assert (
        owner_binding["texture_contract_status"]
        == core.ATLAS_BLENDER_CLUSTER_BAKE_STATUS
    ), owner_binding
    assert set(owner_binding["source_paths"]) == {
        core._texture_semantic_role(role)
        for role in source_files
    }, owner_binding

    unrelated = bpy.data.materials.new("M_leaf_unrelated_01_Mat")
    unrelated_binding = core._speedtree_manifest_texture_binding(
        source_fbx,
        unrelated,
        stmat_data={"materials": {}},
        manifest_cache={},
    )
    assert unrelated_binding is None, unrelated_binding

    unknown_manifest = dict(manifest)
    unknown_manifest["texture_contract_status"] = "unknown_state"
    try:
        core._manifest_texture_entries(unknown_manifest)
    except RuntimeError as exc:
        assert "unknown texture_contract_status" in str(exc), exc
    else:
        raise AssertionError("Unknown texture contract status was accepted")

    bad_contract = json.loads(json.dumps(bake_contract))
    bad_contract["origin_receipt"]["capture_maps"][0]["sha256"] = "0" * 64
    bad_entry = {
        "status": core.ATLAS_BLENDER_CLUSTER_BAKE_STATUS,
        "material_name": material_name,
        "contract": bad_contract,
    }
    try:
        core._validate_atlas_manifest_entry(
            bad_entry,
            manifest_path,
            source_fbx_path=source_fbx,
        )
    except RuntimeError as exc:
        assert "sha256_mismatch" in str(exc), exc
    else:
        raise AssertionError("Tampered Cluster bake receipt was accepted")

    capture_hash = hashlib.sha256(
        b"physical-capture-contract"
    ).hexdigest()
    physical_manifest = (
        cluster_root / "cluster_test_auto_capture_manifest.json"
    )
    physical_manifest.write_text(
        json.dumps({
            "workflow_mode": "PHYSICAL_DIRECT_CAPTURE",
            "direct_uv_source":
                "same_blender_physical_capture_projection",
            "physical_capture_contract_sha256": capture_hash,
            "physical_capture_contract": {
                "contract_sha256": capture_hash,
            },
            "material_name": owner.name,
            "material_id": "7",
            "maps": capture_maps,
        }),
        encoding="utf-8",
    )
    stmat = source_fbx.with_suffix(".stmat")

    def write_source_stmat(rows, target=stmat):
        root = ET.Element("SpeedTreeMaterials")
        material = ET.SubElement(
            root,
            "Material",
            ID="7",
            Name=owner.name,
        )
        for map_name, source in rows:
            ET.SubElement(
                material,
                "Map",
                Name=map_name,
                Source=str(source),
            )
        ET.ElementTree(root).write(
            target,
            encoding="utf-8",
            xml_declaration=True,
        )

    ordered_sources = list(source_files.items())
    write_source_stmat(ordered_sources)
    legacy_slots = [
        {
            # This is deliberately an SPM index, not an STMat index.
            "map_index": index + 100,
            "spm_map_index": index + 100,
            "map": role,
            "path": path,
            "sha256": sha256(Path(path)),
        }
        for index, (role, path) in enumerate(ordered_sources)
    ]
    legacy_receipt = {
        "kind": core.ATLAS_CLUSTER_RECEIPT_KIND,
        "version": 1,
        "source_origin": core.ATLAS_BLENDER_CLUSTER_BAKE_STATUS,
        "material_id": "7",
        "material_name": owner.name,
        "physical_capture_manifest": str(physical_manifest),
        "physical_capture_contract_sha256": capture_hash,
        "slot_files": legacy_slots,
    }

    legacy_proof = core._speedtree_preserved_cluster_sources(
        source_fbx,
        owner,
        expected_binding={"origin_receipt": legacy_receipt},
    )
    assert legacy_proof is not None, legacy_proof
    assert (
        legacy_proof["origin_receipt"]["slot_index_space"]
        == core.STMAT_MAP_INDEX_SPACE
    ), legacy_proof
    assert sorted([
        row["stmat_map_index"]
        for row in legacy_proof["origin_receipt"]["slot_files"]
    ]) == list(range(len(ordered_sources))), legacy_proof

    source_spm_receipt = dict(legacy_receipt)
    source_spm_receipt["slot_index_space"] = (
        core.SOURCE_SPM_MAP_INDEX_SPACE
    )
    assert core._speedtree_preserved_cluster_sources(
        source_fbx,
        owner,
        expected_binding={"origin_receipt": source_spm_receipt},
    ) is not None

    wrong_stmat_receipt = dict(legacy_receipt)
    wrong_stmat_receipt["slot_index_space"] = (
        core.STMAT_MAP_INDEX_SPACE
    )
    assert core._speedtree_preserved_cluster_sources(
        source_fbx,
        owner,
        expected_binding={"origin_receipt": wrong_stmat_receipt},
    ) is None

    exact_stmat_receipt = json.loads(json.dumps(legacy_receipt))
    exact_stmat_receipt["slot_index_space"] = (
        core.STMAT_MAP_INDEX_SPACE
    )
    for index, row in enumerate(exact_stmat_receipt["slot_files"]):
        row["map_index"] = index
        row["stmat_map_index"] = index
    assert core._speedtree_preserved_cluster_sources(
        source_fbx,
        owner,
        expected_binding={"origin_receipt": exact_stmat_receipt},
    ) is not None

    nested_source_fbx = (
        cluster_root / "fbx" / "SK_cluster_test_01.fbx"
    )
    nested_source_fbx.parent.mkdir()
    nested_source_fbx.write_bytes(b"nested-cluster-fbx")
    nested_stmat = nested_source_fbx.with_suffix(".stmat")
    write_source_stmat(ordered_sources, target=nested_stmat)
    nested_proof = core._speedtree_preserved_cluster_sources(
        nested_source_fbx,
        owner,
        expected_binding={"origin_receipt": exact_stmat_receipt},
    )
    assert nested_proof is not None, nested_proof
    assert (
        Path(nested_proof["cluster_root"]).resolve()
        == cluster_root.resolve()
    ), nested_proof

    wrong_path_receipt = json.loads(json.dumps(legacy_receipt))
    wrong_path_receipt["slot_files"][0]["path"] = str(
        cluster_root / "wrong.tga"
    )
    assert core._speedtree_preserved_cluster_sources(
        source_fbx,
        owner,
        expected_binding={"origin_receipt": wrong_path_receipt},
    ) is None

    write_source_stmat(ordered_sources + [ordered_sources[0]])
    assert core._speedtree_preserved_cluster_sources(
        source_fbx,
        owner,
        expected_binding={"origin_receipt": legacy_receipt},
    ) is None

print("BLENDER_CLUSTER_BAKE_MANIFEST_SMOKE_OK")
