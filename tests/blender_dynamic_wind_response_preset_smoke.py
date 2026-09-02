"""Blender-background smoke test for the shared DynamicWind preset contract."""

import sys
from pathlib import Path


ADDON_ROOT = Path(__file__).resolve().parents[1] / "addons"
if str(ADDON_ROOT) not in sys.path:
    sys.path.insert(0, str(ADDON_ROOT))

from speedtree_bone_weight_repair import (  # noqa: E402
    WIND_PRESETS,
    canonical_wind_preset_id,
    core,
)


def _bone_records():
    return [
        {
            "name": "Bone_1_Start",
            "bone_index": 0,
            "parent_index": -1,
            "group": 0,
        },
        {
            "name": "Bone_2_Start",
            "bone_index": 1,
            "parent_index": 0,
            "group": 1,
        },
        {
            "name": "Bone_3_Start",
            "bone_index": 2,
            "parent_index": 1,
            "group": 2,
        },
    ]


def _simulation_groups():
    return [
        {"index": 0, "mean_radius": 8.0, "is_trunk_group": True},
        {"index": 1, "mean_radius": 4.0, "is_trunk_group": False},
        {"index": 2, "mean_radius": 1.0, "is_trunk_group": False},
    ]


def _ground_cover_fan_records():
    return [
        {"name": "Root", "bone_index": 0, "parent_index": -1, "group": 0},
        {"name": "BladeA_Start", "bone_index": 1, "parent_index": 0, "group": 0},
        {"name": "BladeA_End", "bone_index": 2, "parent_index": 1, "group": 0},
        {"name": "BladeB_Start", "bone_index": 3, "parent_index": 0, "group": 0},
        {"name": "BladeB_End", "bone_index": 4, "parent_index": 3, "group": 0},
    ]


def _same_group_chains(joints):
    by_index = {joint["BoneIndex"]: joint for joint in joints}
    children = {bone_index: [] for bone_index in by_index}
    for joint in joints:
        parent = by_index.get(joint["ParentIndex"])
        if parent and parent["SimulationGroupIndex"] == joint["SimulationGroupIndex"]:
            children[parent["BoneIndex"]].append(joint["BoneIndex"])

    origins = {}
    for joint in joints:
        parent = by_index.get(joint["ParentIndex"])
        if parent and parent["SimulationGroupIndex"] == joint["SimulationGroupIndex"]:
            continue
        chain = []
        pending = [joint["BoneIndex"]]
        while pending:
            bone_index = pending.pop()
            chain.append(bone_index)
            pending.extend(children[bone_index])
        origins[joint["JointName"]] = sorted(chain)
    return origins


def main():
    assert canonical_wind_preset_id("GRASS") == "WEED"
    assert set(WIND_PRESETS) == {"TREE", "BUSH", "WEED", "NONE"}
    assert WIND_PRESETS["NONE"] == {
        "flexibility": 0.0,
        "gust_attenuation": 0.0,
        "ground_cover": False,
    }

    weed = core.build_dynamic_wind_data(
        _bone_records(),
        _simulation_groups(),
        gust_attenuation=0.6,
        ground_cover=True,
        flexibility=1.8,
        import_root_name="Root",
        wind_preset="GRASS",
    )
    contract = weed["WindResponsePresetContract"]
    assert contract["SchemaVersion"] == 1
    assert contract["Preset"] == "WEED"
    assert contract["DefaultProfile"] == {
        "Flexibility": 1.8,
        "GustAttenuation": 0.6,
        "bIsGroundCover": True,
    }
    assert contract["SimulationGroupBases"] == [
        {
            "SimulationGroupIndex": 0,
            "BaseFlexibility": 0.0,
            "bSourceTrunkGroup": True,
        },
        {
            "SimulationGroupIndex": 1,
            "BaseFlexibility": 0.0,
            "bSourceTrunkGroup": False,
        },
        {
            "SimulationGroupIndex": 2,
            "BaseFlexibility": 1.0,
            "bSourceTrunkGroup": False,
        },
    ]

    fan_groups = [{"index": 0, "mean_radius": 0.2, "is_trunk_group": True}]
    ground_cover_fan = core.build_dynamic_wind_data(
        _ground_cover_fan_records(),
        fan_groups,
        ground_cover=True,
        flexibility=3.0,
        wind_preset="WEED",
    )
    assert ground_cover_fan["SkeletonContract"]["BoneCount"] == 5
    assert ground_cover_fan["SkeletonContract"]["Bones"][0]["BoneName"] == "Root"
    assert [joint["JointName"] for joint in ground_cover_fan["Joints"]] == [
        "BladeA_Start",
        "BladeA_End",
        "BladeB_Start",
        "BladeB_End",
    ]
    assert _same_group_chains(ground_cover_fan["Joints"]) == {
        "BladeA_Start": [1, 2],
        "BladeB_Start": [3, 4],
    }

    for preset in ("TREE", "BUSH"):
        woody_fan = core.build_dynamic_wind_data(
            _ground_cover_fan_records(),
            fan_groups,
            ground_cover=False,
            flexibility=1.0,
            wind_preset=preset,
        )
        assert len(woody_fan["Joints"]) == 5
        assert woody_fan["Joints"][0]["JointName"] == "Root"
        assert woody_fan["SimulationGroups"] == core.build_dynamic_wind_groups(
            fan_groups, 1.0, False
        )

    none = core.build_dynamic_wind_data(
        _bone_records(),
        _simulation_groups(),
        gust_attenuation=0.0,
        ground_cover=False,
        flexibility=0.0,
        import_root_name="Root",
        wind_preset="NONE",
    )
    none_contract = none["WindResponsePresetContract"]
    assert none_contract["Preset"] == "NONE"
    assert none_contract["SimulationGroupBases"] == contract["SimulationGroupBases"]
    assert none["bIsEnabled"] is False
    assert all(
        not group["bIsTrunkGroup"]
        and group["Influence"] == 0.0
        and group["MinInfluence"] == 0.0
        and group["MaxInfluence"] == 0.0
        and group["ShiftTop"] == 0.0
        for group in none["SimulationGroups"]
    )

    try:
        core.build_dynamic_wind_data(
            _bone_records(),
            _simulation_groups(),
            import_root_name="Root",
            wind_preset="CUSTOM",
        )
    except RuntimeError as exc:
        assert "unsupported response preset" in str(exc)
    else:
        raise AssertionError("unsupported response preset did not fail closed")

    print("DynamicWind response preset contract smoke: PASS")


if __name__ == "__main__":
    main()
