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
