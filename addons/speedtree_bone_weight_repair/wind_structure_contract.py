"""Independent structural wind JSON contract. No Blender/Unreal imports.

This module never alters group/category inputs. It validates a current export
candidate and adds one sibling contract to a copied Wind JSON document.
"""
from __future__ import annotations

import copy
import hashlib
import json
import math
import re


KEY = "WindStructureModifierContract"
SCHEMA_VERSION = 1
HEX64 = re.compile(r"^[0-9a-fA-F]{64}$")
SELF_HASH_KEYS = {"wind", "source_wind", "wind_json", "dynamic_wind_json"}


def canonical_json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False).encode("utf-8")


def source_digest(source_hashes):
    return hashlib.sha256(canonical_json(source_hashes)).hexdigest()


def topology_digest(bones):
    digest = hashlib.sha1()
    for bone in bones:
        digest.update((f"{bone['BoneIndex']}\0{bone['BoneName']}\0"
                       f"{bone['ParentIndex']}\n").encode("utf-8"))
    return digest.hexdigest()


def _topology(wind_document):
    skeleton = wind_document.get("SkeletonContract", {})
    bones = skeleton.get("Bones", [])
    if not bones:
        raise ValueError("Final SkeletonContract has no bones")
    indices = [b["BoneIndex"] for b in bones]
    if indices != list(range(len(bones))):
        raise ValueError("Final skeleton must be sorted, contiguous and complete")
    if len({b["BoneName"] for b in bones}) != len(bones):
        raise ValueError("Final skeleton has duplicate bone names")
    for b in bones:
        parent = b["ParentIndex"]
        if isinstance(parent, bool) or not isinstance(parent, int):
            raise ValueError("ParentIndex must be an integer")
        if (b["BoneIndex"] == 0 and parent != -1) or (b["BoneIndex"] > 0 and not 0 <= parent < b["BoneIndex"]):
            raise ValueError("Final skeleton hierarchy is not parent-before-child")
    expected = topology_digest(bones)
    if skeleton.get("BoneNameIndexParentSha1") != expected:
        raise ValueError("Final SkeletonContract identity digest is inconsistent")
    return bones, expected


def _hashes(candidate):
    output = {}
    for kind, value in candidate.get("source_hashes", {}).items():
        if kind.casefold() in SELF_HASH_KEYS or value is None:
            continue
        digest = value.get("sha256") if isinstance(value, dict) else value
        if not isinstance(digest, str) or not HEX64.fullmatch(digest):
            raise ValueError(f"Invalid source SHA256: {kind}")
        output[kind] = digest.lower()
    if not output:
        raise ValueError("Structural candidate has no immutable source hashes")
    return dict(sorted(output.items()))


def build_contract(wind_document, candidate):
    final_bones, topology_hash = _topology(wind_document)
    source_bones = candidate.get("bones", [])
    if len(source_bones) != len(final_bones):
        raise ValueError("Structural candidate requires complete final skeleton coverage")
    recipe_id = candidate.get("recipe_id")
    recipe_hash = candidate.get("recipe_sha256")
    if not isinstance(recipe_id, str) or not recipe_id.strip():
        raise ValueError("Missing recipe ID")
    if not isinstance(recipe_hash, str) or not HEX64.fullmatch(recipe_hash):
        raise ValueError("Missing or invalid recipe SHA256")
    hashes = _hashes(candidate)
    joint_indices = {j["BoneIndex"] for j in wind_document.get("Joints", [])}
    enabled = bool(wind_document.get("bIsEnabled", True))
    bones = []
    for final, source in zip(final_bones, source_bones):
        identity = (final["BoneName"], final["BoneIndex"], final["ParentIndex"])
        actual = (source["name"], source["bone_index"], source["parent_index"])
        if identity != actual:
            raise ValueError(f"Structural candidate does not match final identity: {identity} != {actual}")
        position = source.get("expected_bind_position_ue_cm")
        if not isinstance(position, (list, tuple)) or len(position) != 3:
            raise ValueError(f"Missing final Unreal-component bind position: {identity}")
        if any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) for v in position):
            raise ValueError(f"Invalid bind position: {identity}")
        gain = source.get("art_bend_gain")
        if isinstance(gain, bool) or not isinstance(gain, (int, float)) or not math.isfinite(gain) or not 0 <= gain <= 3:
            raise ValueError(f"BendGain is outside finite native safety envelope [0,3]: {identity}")
        if final["BoneIndex"] == 0 or not enabled or final["BoneIndex"] not in joint_indices:
            gain = 1.0
        bones.append({"BoneName": final["BoneName"], "BoneIndex": final["BoneIndex"],
                      "ParentIndex": final["ParentIndex"], "BendGain": float(gain),
                      "BindPositionCm": [float(v) for v in position]})
    return {
        "SchemaVersion": SCHEMA_VERSION, "RecipeId": recipe_id,
        "RecipeSha256": recipe_hash.lower(), "SourceHashes": hashes,
        "SourceDigest": source_digest(hashes),
        "BoneNameIndexParentSha1": topology_hash,
        "BindSpace": "unreal_component_cm", "Bones": bones,
        "Mode": "BoundedArtApproximation", "ValidationOnly": True,
        "BendRateScale": 1.0,
        "TorsionGain": 1.0, "FlutterGain": 1.0,
        "SourceValidationStatus": candidate.get("status", "candidate"),
    }


def attach_candidate(wind_document, candidate):
    result = copy.deepcopy(wind_document)
    result[KEY] = build_contract(wind_document, candidate)
    return result


def attach_from_export(settings, paths, rich_data, wind_document, armature=None):
    """BWR final-export hook. Evaluator is shipped beside this module.

    The evaluator consumes current in-memory rich/wind documents and current
    native source bundle; it must not read a stale previous Wind JSON.
    """
    from . import wind_structure_evaluator
    candidate = wind_structure_evaluator.evaluate_export(
        settings=settings, paths=paths, rich_data=rich_data,
        wind_document=wind_document, armature=armature)
    result = attach_candidate(wind_document, candidate)
    contract = result[KEY]
    summary = {"recipe_id": contract["RecipeId"], "recipe_sha256": contract["RecipeSha256"],
               "source_digest": contract["SourceDigest"], "bone_count": len(contract["Bones"]),
               "modified_bone_count": sum(b["BendGain"] != 1.0 for b in contract["Bones"]),
               "status": candidate.get("status", "candidate"), "candidate": candidate}
    return result, summary
