"""Pure-Python tests for the physical-capture preview fallback schema."""

from __future__ import annotations

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path


REPO_DIR = Path(__file__).resolve().parents[1]
MODULE_PATH = (
    REPO_DIR
    / "addons"
    / "speedtree_bone_weight_repair"
    / "preview_texture_contract.py"
)
FIXTURE_PATH = (
    REPO_DIR / "tests" / "fixtures" / "preview_role_fallback_v1.json"
)
SPEC = importlib.util.spec_from_file_location(
    "preview_texture_contract_test_target",
    MODULE_PATH,
)
contract = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(contract)


class PreviewTextureContractTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.color = self.root / "leaf_Subsurface.tga"
        self.amount = self.root / "leaf_SubsurfaceAmount.tga"
        self.unowned = self.root / "unowned.tga"
        self.declared = {
            "subsurfacecolor": {
                "role": "subsurfacecolor",
                "raw_role": "subsurfacecolor",
                "path": str(self.color),
                "sha256": "a" * 64,
            },
            "subsurfaceamount": {
                "role": "subsurfaceamount",
                "raw_role": "subsurfaceamount",
                "path": str(self.amount),
                "sha256": "b" * 64,
            },
        }

    def tearDown(self):
        self.temporary.cleanup()

    def build(self, **overrides):
        values = {
            "slot_role": "subsurfaceamount",
            "slot_path": str(self.color),
            "selected_rows": [self.declared["subsurfacecolor"]],
            "material_id": "7",
            "material_name": "M_leaf_test_Mat",
            "contract_hash": "c" * 64,
            "map_index": 5,
            "map_name": "SubsurfaceAmount",
            "workflow_mode": "PHYSICAL_DIRECT_CAPTURE",
            "direct_uv_source": (
                "same_blender_physical_capture_projection"
            ),
        }
        values.update(overrides)
        return contract.build_preview_role_fallback(**values)

    def test_builds_the_single_documented_preview_fallback(self):
        row = self.build()

        self.assertIsNotNone(row)
        self.assertEqual(row["slot_role"], "subsurfaceamount")
        self.assertEqual(row["manifest_role"], "subsurfacecolor")
        self.assertEqual(row["usage"], "speedtree_preview_only")
        self.assertEqual(row["material_id"], "7")
        self.assertEqual(row["material_name"], "M_leaf_test_Mat")
        self.assertEqual(row["contract_hash"], "c" * 64)
        self.assertEqual(row["path"], str(self.color.resolve()))
        self.assertEqual(set(row), {
            "slot_role",
            "manifest_role",
            "usage",
            "material_id",
            "material_name",
            "contract_hash",
            "map_index",
            "map",
            "path",
            "sha256",
        })
        self.assertIsNotNone(
            contract.preview_role_fallback_signature(row)
        )
        self.assertIsNone(
            contract.preview_role_fallback_signature(
                dict(
                    row,
                    spm_map_index=7,
                    stmat_map_index=5,
                )
            )
        )
        reordered = {
            key: row[key]
            for key in reversed(contract.FALLBACK_CANONICAL_FIELDS)
        }
        canonical = contract.canonical_preview_role_fallbacks(
            [reordered]
        )
        self.assertEqual(
            tuple(canonical[0]),
            contract.FALLBACK_CANONICAL_FIELDS,
        )

    def test_rejects_non_preview_workflow_and_arbitrary_role_swaps(self):
        self.assertIsNone(self.build(workflow_mode="CANONICAL_OUTPUT"))
        self.assertIsNone(self.build(slot_role="color", map_name="Color"))
        self.assertIsNone(
            self.build(
                slot_role="normal",
                map_name="Normal",
                slot_path=str(self.color),
            )
        )
        translucency = dict(
            self.declared["subsurfacecolor"],
            raw_role="translucency",
        )
        self.assertIsNone(self.build(selected_rows=[translucency]))

    def test_rejects_unowned_or_ambiguous_selected_entry(self):
        self.assertIsNone(
            self.build(
                slot_path=str(self.unowned),
                selected_rows=[],
            )
        )
        self.assertIsNone(
            self.build(
                selected_rows=[
                    self.declared["subsurfacecolor"],
                    dict(self.declared["subsurfacecolor"]),
                ],
            )
        )

    def test_does_not_search_an_alternate_amount_candidate(self):
        self.assertIsNotNone(self.build())

    def test_rejects_bad_hash_and_index_receipt_fields(self):
        row = self.build()
        bad_hash = dict(row, sha256="0" * 63)
        bad_index = dict(row, map_index=-1)
        bad_usage = dict(row, usage="production")

        self.assertIsNone(
            contract.preview_role_fallback_signature(bad_hash)
        )
        self.assertIsNone(
            contract.preview_role_fallback_signature(bad_index)
        )
        self.assertIsNone(
            contract.preview_role_fallback_signature(bad_usage)
        )

    def preview_receipt(self):
        row = self.build()
        return contract.finalize_preview_receipt({
            "kind": "blender_cluster_bake_texture_origin_receipt",
            "version": 1,
            "source_origin": "blender_cluster_bake",
            "material_id": "7",
            "material_name": "M_leaf_test_Mat",
            "slot_index_space": "stmat_xml_map_order_v1",
            "physical_capture_manifest": str(
                self.root / "leaf_auto_capture_manifest.json"
            ),
            "physical_capture_contract_sha256": "c" * 64,
            "slot_files": [{
                "map_index": 5,
                "stmat_map_index": 5,
                "map": "SubsurfaceAmount",
                "capture_role": "subsurfaceamount",
                "path": str(self.color),
                "sha256": "a" * 64,
            }],
            contract.PREVIEW_ROLE_FALLBACKS_FIELD: [row],
        })

    def test_receipt_digest_cache_claim_and_production_rejection(self):
        receipt = self.preview_receipt()

        contract.validate_preview_receipt(
            receipt,
            requested_usage="speedtree_preview_only",
        )
        self.assertTrue(
            receipt[contract.RECEIPT_CACHE_KEY_FIELD].endswith(
                receipt[contract.RECEIPT_CORE_SHA256_FIELD]
            )
        )
        with self.assertRaisesRegex(ValueError, "forbidden"):
            contract.validate_preview_receipt(
                receipt,
                requested_usage="production_canonical",
            )

        changed = self.preview_receipt()
        changed[contract.PREVIEW_ROLE_FALLBACKS_FIELD][0]["sha256"] = (
            "d" * 64
        )
        changed["slot_files"][0]["sha256"] = "d" * 64
        changed = contract.finalize_preview_receipt(changed)
        self.assertNotEqual(
            receipt[contract.RECEIPT_CORE_SHA256_FIELD],
            changed[contract.RECEIPT_CORE_SHA256_FIELD],
        )
        self.assertNotEqual(
            receipt[contract.RECEIPT_CACHE_KEY_FIELD],
            changed[contract.RECEIPT_CACHE_KEY_FIELD],
        )

    def test_unknown_schema_capability_and_tampering_fail_closed(self):
        receipt = self.preview_receipt()
        cases = []
        bad_schema = dict(receipt)
        bad_schema[contract.PREVIEW_FALLBACK_SCHEMA_FIELD] = 99
        cases.append(bad_schema)
        bad_capability = dict(receipt)
        bad_capability[contract.RECEIPT_CAPABILITIES_FIELD] = ["unknown"]
        cases.append(bad_capability)
        bad_digest = dict(receipt)
        bad_digest[contract.RECEIPT_CORE_SHA256_FIELD] = "0" * 64
        cases.append(bad_digest)
        bad_cache = dict(receipt)
        bad_cache[contract.RECEIPT_CACHE_KEY_FIELD] = "wrong"
        cases.append(bad_cache)

        for candidate in cases:
            with self.subTest(candidate=candidate):
                with self.assertRaises(ValueError):
                    contract.validate_preview_receipt(
                        candidate,
                        requested_usage="speedtree_preview_only",
                    )

        extra_row_field = self.preview_receipt()
        extra_row_field[contract.PREVIEW_ROLE_FALLBACKS_FIELD][0][
            "stmat_map_index"
        ] = 5
        with self.assertRaises(ValueError):
            contract.validate_preview_receipt(
                extra_row_field,
                requested_usage="speedtree_preview_only",
            )

        reordered_row = self.preview_receipt()
        row = reordered_row[contract.PREVIEW_ROLE_FALLBACKS_FIELD][0]
        reordered_row[contract.PREVIEW_ROLE_FALLBACKS_FIELD][0] = {
            key: row[key]
            for key in reversed(contract.FALLBACK_CANONICAL_FIELDS)
        }
        with self.assertRaisesRegex(ValueError, "fields"):
            contract.validate_preview_receipt(
                reordered_row,
                requested_usage="speedtree_preview_only",
            )

    def test_shared_cross_reader_fixture_and_canonical_order(self):
        receipt = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))

        contract.validate_preview_receipt(
            receipt,
            requested_usage="speedtree_preview_only",
        )
        self.assertEqual(
            list(receipt[contract.PREVIEW_ROLE_FALLBACKS_FIELD][0]),
            list(contract.FALLBACK_CANONICAL_FIELDS),
        )
        self.assertEqual(
            receipt[contract.RECEIPT_CORE_SHA256_FIELD],
            "475084cc428b608b45f6124195d7fe85adf655acc2511109932678e9fd9b7fed",
        )
        self.assertEqual(
            contract.finalize_preview_receipt(receipt),
            receipt,
        )


if __name__ == "__main__":
    unittest.main()
