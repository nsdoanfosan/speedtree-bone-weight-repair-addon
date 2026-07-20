import copy
import hashlib
import importlib.util
import os
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
ADAPTER_PATH = (
    REPO_ROOT
    / "addons"
    / "speedtree_bone_weight_repair"
    / "handoff_contract.py"
)
SPEC = importlib.util.spec_from_file_location("bwr_handoff_contract_test", ADAPTER_PATH)
adapter = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(adapter)


def identity(path):
    path = Path(path).resolve()
    stat = path.stat()
    return {
        "canonical_path": str(path),
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "size": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
    }


class BwrHandoffContractTests(unittest.TestCase):
    def make_envelope(self, root):
        spm = root / "SK_Weed_Common_grass_c_01.spm"
        stmat = root / "fbx" / "SK_Weed_Common_grass_c_01.stmat"
        textures = root / "texture"
        spm.write_bytes(b"speedtree-spm")
        stmat.parent.mkdir(parents=True, exist_ok=True)
        stmat.write_text("<SpeedTreeMaterials />", encoding="utf-8")
        textures.mkdir(exist_ok=True)
        files = {}
        for role in adapter.central_contract_api().required_texture_roles():
            path = textures / f"T_Leaf_grass_Atlas_01_{role}.png"
            path.write_bytes(role.encode("ascii"))
            files[role] = str(path.resolve())

        source = {"spm": identity(spm), "stmat": [identity(stmat)]}
        api = adapter.central_contract_api()
        intents = []
        for index, token in enumerate(("green", "dead")):
            material_name = f"M_Leaf_common_grass_01_{token}"
            intent = api.build_material_intent(
                material_name, instance_profile="dead"
            )
            intent.update(
                {
                    "stmat_material_index": index,
                    "stmat_material_id": str(index + 1),
                    "material_name": material_name,
                    "texture_source_mode": "managed_texture_set",
                    "texture_binding": {
                        "status": "ok",
                        "set_key": "leafgrassatlas01",
                        "texture_base": "T_Leaf_grass_Atlas_01",
                        "texture_dir": str(textures.resolve()),
                        "files": files,
                        "missing_roles": [],
                    },
                }
            )
            intents.append(intent)
        envelope = {
            "kind": "speedtree_material_preflight",
            "schema_version": 1,
            "speedtree_handoff_contract": api.build_sidecar_descriptor(
                spm.stem, source=source
            ),
            "outcome": "ok",
            "source": source,
            "source_fingerprint": adapter.source_fingerprint(source),
            "instance_profile": "dead",
            "tree_user_data": {
                "property": "SpeedTree SDK:User data",
                "raw": "Dead",
                "status": "ok",
            },
            "material_intents": intents,
            "dynamic_wind": {},
            "issues": [],
        }
        return spm, stmat, envelope

    def test_live_provenance_and_shared_binding_are_accepted(self):
        with tempfile.TemporaryDirectory() as temporary:
            spm, stmat, envelope = self.make_envelope(Path(temporary))
            validated, live = adapter.validate_live_preflight_envelope(
                envelope,
                spm_path=spm,
                stmat_paths=[stmat],
                expected_mesh_name=spm.stem,
            )
            bindings = adapter.texture_bindings_from_envelope(validated)

            self.assertEqual(live["spm"]["sha256"], identity(spm)["sha256"])
            self.assertEqual(len(bindings), 2)
            self.assertEqual(
                {row["texture_base"] for row in bindings},
                {"T_Leaf_grass_Atlas_01"},
            )

    def test_stale_spm_and_wrong_contract_fingerprint_fail_closed(self):
        with tempfile.TemporaryDirectory() as temporary:
            spm, stmat, envelope = self.make_envelope(Path(temporary))
            spm.write_bytes(b"changed-after-preflight")
            with self.assertRaisesRegex(RuntimeError, "hash mismatch"):
                adapter.validate_live_preflight_envelope(
                    envelope, spm_path=spm, stmat_paths=[stmat]
                )

            spm, stmat, envelope = self.make_envelope(Path(temporary))
            broken = copy.deepcopy(envelope)
            broken["speedtree_handoff_contract"]["fingerprint"] = "0" * 64
            with self.assertRaisesRegex(ValueError, "fingerprint mismatch"):
                adapter.validate_live_preflight_envelope(
                    broken, spm_path=spm, stmat_paths=[stmat]
                )

    def test_material_intent_uses_exact_key_then_production_group_base(self):
        with tempfile.TemporaryDirectory() as temporary:
            _spm, _stmat, envelope = self.make_envelope(Path(temporary))
            exact = adapter.resolve_material_intent(
                "M_Leaf_common_grass_01_dead", envelope
            )
            consolidated = adapter.resolve_material_intent(
                "M_Leaf_common_grass_01", envelope
            )

            self.assertEqual(exact["match_mode"], "exact_material_key")
            self.assertEqual(
                consolidated["match_mode"], "production_group_base"
            )
            self.assertEqual(consolidated["tree_part"], "leaf")
            self.assertEqual(consolidated["tree_shading"], "foliage")
            self.assertEqual(consolidated["instance_profile"], "dead")

    def test_arbitrary_collection_suffix_uses_numeric_boundary(self):
        self.assertEqual(
            adapter.production_group_base_name(
                "M_Leaf_common_grass_01_winter_dry"
            ),
            "M_Leaf_common_grass_01",
        )
        self.assertEqual(
            adapter.production_group_tokens(
                "M_Leaf_common_grass_01_winter_dry"
            ),
            ["winter_dry"],
        )
        self.assertEqual(
            adapter.production_group_base_name("M_stem_common_01"),
            "M_stem_common_01",
        )
        self.assertEqual(
            adapter.production_group_tokens("M_stem_common_01"), []
        )


if __name__ == "__main__":
    unittest.main()
