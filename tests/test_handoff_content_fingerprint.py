import copy
import importlib.util
import unittest
from pathlib import Path


MODULE_PATH = (
    Path(__file__).resolve().parents[1]
    / "addons"
    / "speedtree_bone_weight_repair"
    / "handoff_contract.py"
)
SPEC = importlib.util.spec_from_file_location("bwr_handoff_contract_test", MODULE_PATH)
handoff_contract = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(handoff_contract)
CONTENT_FINGERPRINT_POLICY = handoff_contract.CONTENT_FINGERPRINT_POLICY
source_fingerprint = handoff_contract.source_fingerprint


class SourceFingerprintTests(unittest.TestCase):
    def setUp(self):
        self.source = {
            "spm": {
                "canonical_path": r"C:\Trees\SK_tree.spm",
                "sha256": "a" * 64,
                "size": 10,
                "mtime_ns": 20,
            },
            "stmat": [],
        }

    def test_content_policy_ignores_touch_only_metadata_drift(self):
        touched = copy.deepcopy(self.source)
        touched["spm"]["mtime_ns"] = 999

        self.assertEqual(
            source_fingerprint(self.source, CONTENT_FINGERPRINT_POLICY),
            source_fingerprint(touched, CONTENT_FINGERPRINT_POLICY),
        )

    def test_legacy_policy_keeps_metadata_in_fingerprint(self):
        touched = copy.deepcopy(self.source)
        touched["spm"]["mtime_ns"] = 999

        self.assertNotEqual(
            source_fingerprint(self.source),
            source_fingerprint(touched),
        )

    def test_content_policy_still_rejects_content_change(self):
        changed = copy.deepcopy(self.source)
        changed["spm"]["sha256"] = "b" * 64

        self.assertNotEqual(
            source_fingerprint(self.source, CONTENT_FINGERPRINT_POLICY),
            source_fingerprint(changed, CONTENT_FINGERPRINT_POLICY),
        )

    def test_unknown_policy_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "unsupported"):
            source_fingerprint(self.source, "future_unknown_policy")


if __name__ == "__main__":
    unittest.main()
