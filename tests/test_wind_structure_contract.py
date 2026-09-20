import copy
import unittest
import importlib.util
from pathlib import Path

_source = Path(__file__).resolve().parents[1] / 'addons/speedtree_bone_weight_repair/wind_structure_contract.py'
_spec = importlib.util.spec_from_file_location('wind_structure_contract_under_test', _source)
_contract = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_contract)
KEY = _contract.KEY
attach_candidate = _contract.attach_candidate
source_digest = _contract.source_digest
topology_digest = _contract.topology_digest


def fixture():
    bones = [{"BoneName": "Root", "BoneIndex": 0, "ParentIndex": -1},
             {"BoneName": "Support", "BoneIndex": 1, "ParentIndex": 0},
             {"BoneName": "Rigid", "BoneIndex": 2, "ParentIndex": 1}]
    wind = {"SkeletonContract": {"Bones": bones, "BoneNameIndexParentSha1": topology_digest(bones)},
            "Joints": [{"BoneIndex": 1, "JointName": "Support", "SimulationGroupIndex": 0}],
            "SimulationGroups": [{"Influence": .5}], "GustAttenuation": .25,
            "WindResponsePresetContract": {"Preset": "WEED"}, "bIsEnabled": True}
    candidate = {"recipe_id": "generic_v1", "recipe_sha256": "a"*64,
                 "source_hashes": {"spm": {"sha256": "b"*64}, "wind": {"sha256": "c"*64}},
                 "bones": [{"name": b["BoneName"], "bone_index": b["BoneIndex"],
                            "parent_index": b["ParentIndex"], "art_bend_gain": 1.2,
                            "expected_bind_position_ue_cm": [0., 0., float(b["BoneIndex"])]} for b in bones]}
    return wind, candidate


class ContractTests(unittest.TestCase):
    def test_sibling_keeps_base_and_input_unchanged(self):
        wind, candidate = fixture(); original = copy.deepcopy(wind)
        result = attach_candidate(wind, candidate)
        self.assertEqual(wind, original)
        self.assertEqual({k: v for k, v in result.items() if k != KEY}, original)
        self.assertEqual([b["BendGain"] for b in result[KEY]["Bones"]], [1., 1.2, 1.])
        self.assertTrue(result[KEY]["ValidationOnly"])
        self.assertEqual(result[KEY]["Mode"], "BoundedArtApproximation")
        self.assertEqual([result[KEY][k] for k in ("BendRateScale", "TorsionGain", "FlutterGain")], [1., 1., 1.])
    def test_idempotent_reexport_does_not_multiply(self):
        wind, candidate = fixture()
        first = attach_candidate(wind, candidate)
        self.assertEqual(attach_candidate(first, candidate), first)
    def test_self_hash_excluded_digest_canonical(self):
        wind, candidate = fixture(); c = attach_candidate(wind, candidate)[KEY]
        self.assertEqual(c["SourceHashes"], {"spm": "b"*64})
        self.assertEqual(c["SourceDigest"], source_digest(c["SourceHashes"]))
    def test_disabled_is_identity(self):
        wind, candidate = fixture(); wind["bIsEnabled"] = False
        self.assertTrue(all(b["BendGain"] == 1 for b in attach_candidate(wind,candidate)[KEY]["Bones"]))
    def test_renamed_asset_and_response_category_do_not_change_gain(self):
        wind, candidate = fixture(); first = attach_candidate(wind,candidate)[KEY]
        wind["WindResponsePresetContract"]["Preset"] = "TREE"; wind["Asset"] = "ArbitraryPlant"
        self.assertEqual(attach_candidate(wind,candidate)[KEY], first)
    def test_partial_candidate_rejected(self):
        wind,candidate=fixture(); candidate["bones"].pop()
        with self.assertRaises(ValueError): attach_candidate(wind,candidate)
    def test_mismatched_identity_rejected(self):
        wind,candidate=fixture(); candidate["bones"][1]["name"]="Other"
        with self.assertRaises(ValueError): attach_candidate(wind,candidate)
    def test_missing_bind_rejected(self):
        wind,candidate=fixture(); candidate["bones"][0]["expected_bind_position_ue_cm"]=None
        with self.assertRaises(ValueError): attach_candidate(wind,candidate)
    def test_nan_out_of_range_rejected(self):
        for value in (float('nan'),float('inf'),-0.1,3.01,True):
            wind,candidate=fixture(); candidate["bones"][1]["art_bend_gain"]=value
            with self.assertRaises(ValueError): attach_candidate(wind,candidate)
    def test_topology_digest_mismatch_rejected(self):
        wind,candidate=fixture(); wind["SkeletonContract"]["BoneNameIndexParentSha1"]="0"*40
        with self.assertRaises(ValueError): attach_candidate(wind,candidate)


if __name__ == "__main__": unittest.main()
