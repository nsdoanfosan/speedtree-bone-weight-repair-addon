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
                "role": (
                    "SubsurfaceColor"
                    if role == "translucency"
                    else role
                ),
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
    physical_manifest_payload = {
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
    }
    physical_manifest.write_text(
        json.dumps(physical_manifest_payload),
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
        Path(legacy_proof["cluster_root"]).resolve()
        == cluster_root.resolve()
    ), legacy_proof
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

    nested_wrong_index = json.loads(json.dumps(exact_stmat_receipt))
    nested_wrong_index["slot_files"][0]["stmat_map_index"] = 99
    assert core._speedtree_preserved_cluster_sources(
        nested_source_fbx,
        owner,
        expected_binding={"origin_receipt": nested_wrong_index},
    ) is None

    nested_wrong_path = json.loads(json.dumps(exact_stmat_receipt))
    nested_wrong_path["slot_files"][0]["path"] = str(
        cluster_root / "wrong.tga"
    )
    assert core._speedtree_preserved_cluster_sources(
        nested_source_fbx,
        owner,
        expected_binding={"origin_receipt": nested_wrong_path},
    ) is None

    nested_wrong_hash = json.loads(json.dumps(exact_stmat_receipt))
    nested_wrong_hash["slot_files"][0]["sha256"] = "0" * 64
    assert core._speedtree_preserved_cluster_sources(
        nested_source_fbx,
        owner,
        expected_binding={"origin_receipt": nested_wrong_hash},
    ) is None

    outside_root = asset_root / "outside_capture"
    outside_root.mkdir()
    first_role, first_path = ordered_sources[0]
    outside_path = outside_root / Path(first_path).name
    outside_path.write_bytes(Path(first_path).read_bytes())
    outside_sources = list(ordered_sources)
    outside_sources[0] = (first_role, str(outside_path))
    write_source_stmat(outside_sources, target=nested_stmat)
    outside_receipt = json.loads(json.dumps(exact_stmat_receipt))
    outside_receipt["slot_files"][0]["path"] = str(outside_path)
    outside_receipt["slot_files"][0]["sha256"] = sha256(outside_path)
    outside_manifest = json.loads(json.dumps(physical_manifest_payload))
    outside_manifest["maps"][0]["path"] = str(outside_path)
    outside_manifest["maps"][0]["sha256"] = sha256(outside_path)
    physical_manifest.write_text(
        json.dumps(outside_manifest),
        encoding="utf-8",
    )
    assert core._speedtree_preserved_cluster_sources(
        nested_source_fbx,
        owner,
        expected_binding={"origin_receipt": outside_receipt},
    ) is None
    write_source_stmat(ordered_sources, target=nested_stmat)
    physical_manifest.write_text(
        json.dumps(physical_manifest_payload),
        encoding="utf-8",
    )

    wrong_path_receipt = json.loads(json.dumps(legacy_receipt))
    wrong_path_receipt["slot_files"][0]["path"] = str(
        cluster_root / "wrong.tga"
    )
    assert core._speedtree_preserved_cluster_sources(
        source_fbx,
        owner,
        expected_binding={"origin_receipt": wrong_path_receipt},
    ) is None

    fallback_sources = [
        (
            role,
            source_files["translucency"]
            if role == "subsurface_amount"
            else path,
        )
        for role, path in ordered_sources
    ]
    write_source_stmat(fallback_sources)
    fallback_expected_receipt = json.loads(json.dumps(
        exact_stmat_receipt
    ))
    for row in fallback_expected_receipt["slot_files"]:
        row["capture_role"] = core._texture_semantic_role(row["map"])
        if row["capture_role"] == "subsurfaceamount":
            row["path"] = source_files["translucency"]
            row["sha256"] = sha256(
                Path(source_files["translucency"])
            )
    fallback_expected_receipt[
        core.PREVIEW_ROLE_FALLBACKS_FIELD
    ] = [{
        "slot_role": "subsurfaceamount",
        "manifest_role": "subsurfacecolor",
        "usage": "speedtree_preview_only",
        "material_id": "7",
        "material_name": owner.name,
        "contract_hash": capture_hash,
        "map_index": 6,
        "map": "subsurface_amount",
        "path": str(Path(source_files["translucency"]).resolve()),
        "sha256": sha256(Path(source_files["translucency"])),
    }]
    fallback_expected_receipt = core.finalize_preview_receipt(
        fallback_expected_receipt
    )
    fallback_proof = core._speedtree_preserved_cluster_sources(
        source_fbx,
        owner,
        expected_binding={
            "origin_receipt": fallback_expected_receipt
        },
    )
    assert fallback_proof is not None, fallback_proof
    preview_fallbacks = fallback_proof.get(
        core.PREVIEW_ROLE_FALLBACKS_FIELD
    )
    assert len(preview_fallbacks) == 1, fallback_proof
    preview_fallback = preview_fallbacks[0]
    assert preview_fallback == {
        "slot_role": "subsurfaceamount",
        "manifest_role": "subsurfacecolor",
        "usage": "speedtree_preview_only",
        "material_id": "7",
        "material_name": owner.name,
        "contract_hash": capture_hash,
        "map_index": 6,
        "map": "subsurface_amount",
        "path": str(Path(source_files["translucency"]).resolve()),
        "sha256": sha256(Path(source_files["translucency"])),
    }, fallback_proof
    assert (
        fallback_proof["origin_receipt"][
            core.PREVIEW_ROLE_FALLBACKS_FIELD
        ]
        == preview_fallbacks
    ), fallback_proof
    assert fallback_proof["origin_receipt"]["version"] == 2
    assert fallback_proof["origin_receipt"]["receipt_claim"] == (
        "speedtree_preview_only"
    )
    assert core._speedtree_preserved_cluster_sources(
        source_fbx,
        owner,
        expected_binding={
            "origin_receipt": fallback_proof["origin_receipt"]
        },
    ) is not None
    try:
        core._validate_atlas_canonical_entry(
            {
                "material_name": owner.name,
                "contract": {
                    "origin_receipt": fallback_proof["origin_receipt"],
                },
            },
            manifest_path,
            source_fbx_path=source_fbx,
        )
    except RuntimeError as exc:
        assert "rejects preview-only" in str(exc), exc
    else:
        raise AssertionError(
            "Canonical consumer accepted a preview fallback receipt"
        )

    source_spm_fallback_receipt = json.loads(json.dumps(
        fallback_proof["origin_receipt"]
    ))
    source_spm_fallback_receipt["slot_index_space"] = (
        core.SOURCE_SPM_MAP_INDEX_SPACE
    )
    for row in source_spm_fallback_receipt["slot_files"]:
        row["spm_map_index"] = row["map_index"] + 100
        row["map_index"] = row["spm_map_index"]
    source_spm_fallback = source_spm_fallback_receipt[
        core.PREVIEW_ROLE_FALLBACKS_FIELD
    ][0]
    source_spm_fallback["spm_map_index"] = (
        source_spm_fallback["map_index"] + 100
    )
    source_spm_fallback["map_index"] = source_spm_fallback[
        "spm_map_index"
    ]
    source_spm_fallback.pop("spm_map_index")
    source_spm_fallback_receipt = core.finalize_preview_receipt(
        source_spm_fallback_receipt
    )
    assert core._speedtree_preserved_cluster_sources(
        source_fbx,
        owner,
        expected_binding={
            "origin_receipt": source_spm_fallback_receipt
        },
    ) is not None

    source_spm_amount_slot = next(
        row
        for row in source_spm_fallback_receipt["slot_files"]
        if row["capture_role"] == "subsurfaceamount"
    )
    source_spm_amount_slot["stmat_map_index"] += 1
    source_spm_fallback_receipt = core.finalize_preview_receipt(
        source_spm_fallback_receipt
    )
    assert core._speedtree_preserved_cluster_sources(
        source_fbx,
        owner,
        expected_binding={
            "origin_receipt": source_spm_fallback_receipt
        },
    ) is None

    wrong_hash_receipt = json.loads(json.dumps(
        fallback_proof["origin_receipt"]
    ))
    wrong_hash_receipt["physical_capture_contract_sha256"] = "0" * 64
    wrong_hash_receipt = core.finalize_preview_receipt(
        wrong_hash_receipt
    )
    assert core._speedtree_preserved_cluster_sources(
        source_fbx,
        owner,
        expected_binding={"origin_receipt": wrong_hash_receipt},
    ) is None

    wrong_root_receipt = json.loads(json.dumps(
        fallback_proof["origin_receipt"]
    ))
    wrong_root_receipt["physical_capture_manifest"] = str(
        asset_root / "other" / physical_manifest.name
    )
    wrong_root_receipt = core.finalize_preview_receipt(
        wrong_root_receipt
    )
    assert core._speedtree_preserved_cluster_sources(
        source_fbx,
        owner,
        expected_binding={"origin_receipt": wrong_root_receipt},
    ) is None

    wrong_material_receipt = json.loads(json.dumps(
        fallback_proof["origin_receipt"]
    ))
    wrong_material_receipt["material_name"] = "M_other_Mat"
    wrong_material_receipt["material_id"] = "99"
    wrong_material_receipt = core.finalize_preview_receipt(
        wrong_material_receipt
    )
    assert core._speedtree_preserved_cluster_sources(
        source_fbx,
        owner,
        expected_binding={"origin_receipt": wrong_material_receipt},
    ) is None

    wrong_index_receipt = json.loads(json.dumps(
        fallback_proof["origin_receipt"]
    ))
    amount_slot = next(
        row
        for row in wrong_index_receipt["slot_files"]
        if row["capture_role"] == "subsurfaceamount"
    )
    amount_slot["map_index"] += 1
    amount_slot["stmat_map_index"] += 1
    wrong_index_receipt = core.finalize_preview_receipt(
        wrong_index_receipt
    )
    assert core._speedtree_preserved_cluster_sources(
        source_fbx,
        owner,
        expected_binding={"origin_receipt": wrong_index_receipt},
    ) is None

    wrong_fallback_receipt = json.loads(json.dumps(
        fallback_proof["origin_receipt"]
    ))
    wrong_fallback_receipt[
        core.PREVIEW_ROLE_FALLBACKS_FIELD
    ][0]["usage"] = "production"
    assert core._speedtree_preserved_cluster_sources(
        source_fbx,
        owner,
        expected_binding={"origin_receipt": wrong_fallback_receipt},
    ) is None

    invalid_manifest_payload = json.loads(json.dumps(
        physical_manifest_payload
    ))
    next(
        row
        for row in invalid_manifest_payload["maps"]
        if row["role"] == "SubsurfaceColor"
    )["sha256"] = "0" * 64
    physical_manifest.write_text(
        json.dumps(invalid_manifest_payload),
        encoding="utf-8",
    )
    assert core._speedtree_preserved_cluster_sources(
        source_fbx,
        owner,
        expected_binding={
            "origin_receipt": fallback_expected_receipt
        },
    ) is None
    physical_manifest.write_text(
        json.dumps(physical_manifest_payload),
        encoding="utf-8",
    )

    unowned = cluster_root / "unowned.tga"
    unowned.write_bytes(b"unowned")
    write_source_stmat([
        (role, str(unowned) if role == "subsurface_amount" else path)
        for role, path in ordered_sources
    ])
    assert core._speedtree_preserved_cluster_sources(
        source_fbx,
        owner,
        expected_binding={
            "origin_receipt": fallback_expected_receipt
        },
    ) is None

    alias_manifest_payload = json.loads(json.dumps(
        physical_manifest_payload
    ))
    next(
        row
        for row in alias_manifest_payload["maps"]
        if row["role"] == "SubsurfaceColor"
    )["role"] = "translucency"
    physical_manifest.write_text(
        json.dumps(alias_manifest_payload),
        encoding="utf-8",
    )
    write_source_stmat(fallback_sources)
    assert core._speedtree_preserved_cluster_sources(
        source_fbx,
        owner,
        expected_binding={
            "origin_receipt": fallback_expected_receipt
        },
    ) is None
    physical_manifest.write_text(
        json.dumps(physical_manifest_payload),
        encoding="utf-8",
    )

    for swapped_role, swapped_path in (
        ("albedo", source_files["normal"]),
        ("normal", source_files["albedo"]),
        ("alpha", source_files["height"]),
        ("height", source_files["alpha"]),
    ):
        write_source_stmat([
            (role, swapped_path if role == swapped_role else path)
            for role, path in ordered_sources
        ])
        assert core._speedtree_preserved_cluster_sources(
            source_fbx,
            owner,
        ) is None

    write_source_stmat(ordered_sources + [ordered_sources[0]])
    assert core._speedtree_preserved_cluster_sources(
        source_fbx,
        owner,
        expected_binding={"origin_receipt": legacy_receipt},
    ) is None

print("BLENDER_CLUSTER_BAKE_MANIFEST_SMOKE_OK")
