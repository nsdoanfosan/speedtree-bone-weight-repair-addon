"""Exercise the actual source audit helpers without a live Blender session."""
import ast
import hashlib
import json
from pathlib import Path
import pathlib
from types import SimpleNamespace
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
COMMON = ROOT / "addons/speedtree_bone_weight_repair/debris_terrain_groups.py"
GENERIC = ROOT / "addons/speedtree_bone_weight_repair/debris_terrain_prefab.py"


def actual_helpers(filepath):
    tree = ast.parse(COMMON.read_text(encoding="utf-8"))
    selected = [node for node in tree.body if
                (isinstance(node, ast.ClassDef) and node.name == "TerrainGroupError") or
                (isinstance(node, ast.FunctionDef) and node.name in
                 {"_require", "_source_wind_audit", "_wind_none_contract"})]
    scope = {"hashlib": hashlib, "json": json, "pathlib": pathlib,
             "bpy": SimpleNamespace(data=SimpleNamespace(filepath=str(filepath)))}
    exec(compile(ast.Module(body=selected, type_ignores=[]), str(COMMON), "exec"), scope)
    return scope


def context(preset="TREE"):
    settings = SimpleNamespace(wind_preset=preset, dynamic_wind_flexibility=1.0,
                               dynamic_wind_gust_attenuation=0.25,
                               dynamic_wind_ground_cover=False, write_dynamic_wind_json=True,
                               is_property_set=lambda name: True)
    return SimpleNamespace(scene=SimpleNamespace(speedtree_bwr_settings=settings))


class SourceWindAuditTests(unittest.TestCase):
    def test_missing_sidecar_and_any_scene_preset_are_auditable(self):
        with tempfile.TemporaryDirectory() as temp:
            functions = actual_helpers(Path(temp) / "RenamedSource.blend")
            for preset in ("TREE", "BUSH", "WEED", "NONE", ""):
                with self.subTest(preset=preset):
                    receipt = functions["_source_wind_audit"](context(preset), {})
                    self.assertEqual(receipt["saved_contracts"], [])
                    self.assertEqual(receipt["scene_preset"], preset)
                    self.assertTrue(receipt["audit_only"])
                    self.assertEqual(len(receipt["snapshot_sha256"]), 64)

    def test_enabled_wind_is_recorded_without_source_or_settings_mutation(self):
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / "ArbitrarySource.blend"
            sidecar = source.parent / "JSON" / f"{source.stem}_dynamic_wind_import_from_megaplant_groups.json"
            sidecar.parent.mkdir()
            raw = b'{"WindResponsePresetContract":{"Preset":"TREE"},"bIsEnabled":true}'
            sidecar.write_bytes(raw)
            functions = actual_helpers(source)
            scene = context("TREE")
            before = dict(vars(scene.scene.speedtree_bwr_settings))
            receipt = functions["_source_wind_audit"](scene, {})
            self.assertEqual(receipt["saved_contracts"][0]["preset"], "TREE")
            self.assertIs(receipt["saved_contracts"][0]["enabled"], True)
            self.assertEqual(receipt["saved_contracts"][0]["sha256"], hashlib.sha256(raw).hexdigest())
            self.assertEqual(sidecar.read_bytes(), raw)
            self.assertEqual(vars(scene.scene.speedtree_bwr_settings), before)
            self.assertEqual(receipt, functions["_source_wind_audit"](scene, {}))
            scene.scene.speedtree_bwr_settings.dynamic_wind_flexibility = 2.0
            changed = functions["_source_wind_audit"](scene, {})
            self.assertNotEqual(receipt["snapshot_sha256"], changed["snapshot_sha256"])

    def test_malformed_wind_is_provenance_not_an_activation_condition(self):
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / "ArbitrarySource.blend"
            sidecar = source.parent / "JSON" / f"{source.stem}_dynamic_wind_import_from_megaplant_groups.json"
            sidecar.parent.mkdir()
            sidecar.write_bytes(b"invalid-existing-wind-json")
            receipt = actual_helpers(source)["_source_wind_audit"](context(), {})
            self.assertIn("parse_error", receipt["saved_contracts"][0])
            self.assertIsNone(receipt["saved_contracts"][0]["enabled"])

    def test_historical_none_experiment_retains_its_explicit_legacy_gate(self):
        with tempfile.TemporaryDirectory() as temp:
            functions = actual_helpers(Path(temp) / "OriginalExperiment.blend")
            with self.assertRaises(RuntimeError):
                functions["_wind_none_contract"](context("TREE"), {})
            self.assertEqual(functions["_wind_none_contract"](context("NONE"), {})["scene_preset"], "NONE")

    def test_generic_routes_use_audit_only_and_preservation_check(self):
        tree = ast.parse(GENERIC.read_text(encoding="utf-8"))
        extract = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "extract_source_parts")
        calls = [n for n in ast.walk(extract) if isinstance(n, ast.Call)]
        self.assertTrue(any(isinstance(n.func, ast.Attribute) and n.func.attr == "_source_wind_audit" for n in calls))
        self.assertFalse(any(isinstance(n.func, ast.Attribute) and n.func.attr == "_wind_none_contract" for n in calls))
        builder = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "build_generic_terrain_groups")
        call = next(n for n in ast.walk(builder) if isinstance(n, ast.Call)
                    and isinstance(n.func, ast.Attribute) and n.func.attr == "build_terrain_groups")
        self.assertTrue(any(k.arg == "source_wind_policy" and k.value.value == "audit_only" for k in call.keywords))
        self.assertIn("Terrain grouping changed source scene or saved Wind data", COMMON.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
