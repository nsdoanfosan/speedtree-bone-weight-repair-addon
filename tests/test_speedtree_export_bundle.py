import importlib.util
import codecs
import json
import subprocess
import tempfile
import unittest
from pathlib import Path


MODULE_PATH = (
    Path(__file__).resolve().parents[1]
    / "addons"
    / "speedtree_bone_weight_repair"
    / "speedtree_cli.py"
)
SPEC = importlib.util.spec_from_file_location("bwr_speedtree_cli_bundle_test", MODULE_PATH)
speedtree_cli = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(speedtree_cli)


class SpeedTreeExportBundleTests(unittest.TestCase):
    @staticmethod
    def _write_native_receipt(path, spm):
        stat = spm.stat()
        Path(path).write_text(json.dumps({
            "schema_version": 2,
            "kind": "speedtree_native_export_receipt",
            "status": "ready",
            "source": {
                "path": str(spm.resolve()),
                "size": stat.st_size,
                "last_write_time_100ns": (
                    stat.st_mtime_ns // 100 + 116444736000000000
                ),
            },
            "geometry_count": 1,
            "geometries": [{"ordinal": 0, "vertex_count": 3}],
        }), encoding="utf-8")

    @staticmethod
    def _write_synthetic_native_receipt(path, spm, *, include_ordinary_gap=False):
        stat = spm.stat()
        bones = [{
            "id": 1,
            "parent_id": 0,
            "start_native": [-4.7353949546813965, 2.007727861404419, -6.475102424621582],
            "end_native": [4.935113906860352, 2.338667154312134, -1.0238579511642456],
            "source_rtti": ".?AVCBranchNode@@",
        }]
        if include_ordinary_gap:
            bones.append({
                "id": 2,
                "parent_id": 1,
                "start_native": [4.935113906860352, 2.338667154312134, -1.0238579511642456],
                "end_native": [5.0, 3.0, 0.0],
                "source_rtti": ".?AVCBranchNode@@",
            })
        bones.append({
            "id": 10000,
            "parent_id": 0,
            "start_native": [0.0, 0.0, 0.0],
            "end_native": [0.0, 0.0, 1.0],
            "source_rtti": ".?AVCLeafMeshNode@@",
        })
        Path(path).write_text(json.dumps({
            "schema_version": 5,
            "kind": "speedtree_native_export_receipt",
            "status": "ready",
            "source": {
                "path": str(spm.resolve()),
                "size": stat.st_size,
                "last_write_time_100ns": (
                    stat.st_mtime_ns // 100 + 116444736000000000
                ),
            },
            "coordinate_contract": {"native_unit_to_solver": 30.48},
            "geometry_count": 2,
            "geometries": [
                {"ordinal": 0, "vertex_count": 36},
                {"ordinal": 1, "vertex_count": 3},
            ],
            "bones": bones,
            "generated_instances": [{
                "geometry_ordinal": 0,
                "source_bone_id": 10000,
                "source_rtti": ".?AVCLeafMeshNode@@",
                "generator_guid": "Y2mFg3XRRE62pjqfX1LreA==",
            }],
        }), encoding="utf-8")

    @staticmethod
    def _root_like_xml_bytes():
        return codecs.BOM_UTF8 + (
            '<?xml version="1.0" encoding="UTF-8"?>\r\n'
            '<SpeedTreeRaw>\r\n'
            '\t<Materials Count="1">\r\n'
            '\t\t<Material ID="4" Name="leaf" UserData="{&quot;generator&quot;:&quot;Atlas Leaf Mesh Builder&quot;}" />\r\n'
            '\t</Materials>\r\n'
            '\t<Bones Count="1">\r\n'
            '\t\t<Bone ID="0" ParentID="-1" Radius="82.296" StartX="-144.335" StartY="61.1955" StartZ="-197.361" EndX="150.422" EndY="71.2826" EndZ="-31.2072" Mass="272.64" Generator="Branch 10" />\r\n'
            '\t</Bones>\r\n'
            '</SpeedTreeRaw>\r\n'
        ).encode("utf-8")

    def test_reconcile_adds_only_reserved_receipt_bone_and_is_idempotent(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            spm = root / "tree.spm"
            xml = root / "tree.xml"
            receipt = root / "tree.speedtree_native_receipt.json"
            spm.write_bytes(b"spm")
            original = self._root_like_xml_bytes()
            original_bone = (
                b'<Bone ID="0" ParentID="-1" Radius="82.296" '
                b'StartX="-144.335" StartY="61.1955" StartZ="-197.361" '
                b'EndX="150.422" EndY="71.2826" EndZ="-31.2072" '
                b'Mass="272.64" Generator="Branch 10" />'
            )
            xml.write_bytes(original)
            self._write_synthetic_native_receipt(receipt, spm)

            first = speedtree_cli.reconcile_xml_with_native_receipt(
                xml, receipt, spm, create_backup=True
            )
            first_bytes = xml.read_bytes()
            second = speedtree_cli.reconcile_xml_with_native_receipt(
                xml, receipt, spm, create_backup=True
            )

            self.assertTrue(first["changed"])
            self.assertEqual(first["added_xml_ids"], [9999])
            self.assertEqual(
                first["provenance"][0]["generator"],
                "Atlas Leaf Mesh Builder",
            )
            self.assertEqual(
                first["provenance"][0]["proof"],
                "receipt_generator_guid_and_unique_xml_material_intent",
            )
            self.assertTrue(first_bytes.startswith(codecs.BOM_UTF8))
            self.assertIn(original_bone, first_bytes)
            self.assertIn(b'<Bones Count="2">', first_bytes)
            self.assertIn(
                b'<Bone ID="9999" ParentID="-1" Radius="0" '
                b'StartX="0" StartY="0" StartZ="0" EndX="0" '
                b'EndY="0" EndZ="30.48" Mass="0" '
                b'Generator="Atlas Leaf Mesh Builder" />',
                first_bytes,
            )
            self.assertEqual(Path(first["backup"]).read_bytes(), original)
            self.assertFalse(second["changed"])
            self.assertEqual(second["status"], "already_reconciled")
            self.assertEqual(xml.read_bytes(), first_bytes)

    def test_reconcile_fails_closed_for_an_ordinary_missing_bone(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            spm = root / "tree.spm"
            xml = root / "tree.xml"
            receipt = root / "tree.speedtree_native_receipt.json"
            spm.write_bytes(b"spm")
            original = self._root_like_xml_bytes()
            xml.write_bytes(original)
            self._write_synthetic_native_receipt(
                receipt, spm, include_ordinary_gap=True
            )

            with self.assertRaisesRegex(RuntimeError, "ordinary native receipt"):
                speedtree_cli.reconcile_xml_with_native_receipt(
                    xml, receipt, spm, create_backup=True
                )
            self.assertEqual(xml.read_bytes(), original)
            self.assertFalse(
                (root / ".speedtree_xml_reconcile_backups").exists()
            )

    def test_bundle_reconciles_before_promotion_and_caches_final_xml(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            exe = root / "speedtree_collision_cli.exe"
            spm = root / "tree.spm"
            fbx_options = root / "fbx.ini"
            xml_options = root / "xml.ini"
            fbx = root / "out" / "fbx" / "tree.fbx"
            xml = root / "out" / "xml" / "tree.xml"
            receipt = fbx.with_name("tree.speedtree_native_receipt.json")
            exe.write_bytes(b"exe")
            exe.with_name("speedtree_collision_hook.dll").write_bytes(b"hook")
            spm.write_bytes(b"spm")
            preset = "[Options]\nTextureSkipWriting=true\n"
            fbx_options.write_text(preset, encoding="utf-8")
            xml_options.write_text(preset, encoding="utf-8")
            calls = []
            original_run = speedtree_cli._run_process

            def fake_run(command, cwd, timeout_seconds):
                calls.append(list(command))
                primary = Path(command[command.index("-export") + 1])
                secondary = Path(
                    command[command.index("--secondary-export") + 1]
                )
                staged_receipt = Path(
                    command[command.index("--native-receipt") + 1]
                )
                primary.write_bytes(b"fbx")
                primary.with_suffix(".stmat").write_text(
                    "<Materials />", encoding="utf-8"
                )
                secondary.write_bytes(self._root_like_xml_bytes())
                self._write_synthetic_native_receipt(staged_receipt, spm)
                return 0, "ok", ""

            speedtree_cli._run_process = fake_run
            try:
                first = speedtree_cli.export_bundle(
                    exe,
                    spm,
                    [
                        ("fbx", fbx, fbx_options),
                        ("xml", xml, xml_options),
                    ],
                    native_receipt=receipt,
                )
                second = speedtree_cli.export_bundle(
                    exe,
                    spm,
                    [
                        ("fbx", fbx, fbx_options),
                        ("xml", xml, xml_options),
                    ],
                    native_receipt=receipt,
                )
            finally:
                speedtree_cli._run_process = original_run

            self.assertEqual(len(calls), 1)
            self.assertEqual(
                first["xml"]["native_receipt_xml_reconciliation"][
                    "added_xml_ids"
                ],
                [9999],
            )
            self.assertIn(b'<Bones Count="2">', xml.read_bytes())
            cache = json.loads(
                speedtree_cli._cache_path(xml).read_text(encoding="utf-8")
            )
            self.assertEqual(cache["version"], 3)
            self.assertEqual(
                cache["artifacts"][0]["sha256"],
                speedtree_cli._sha256_file(xml),
            )
            self.assertTrue(second["fbx"]["cache_hit"])
            self.assertTrue(second["xml"]["cache_hit"])

    def test_export_fingerprint_changes_with_the_loaded_hook_binary(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            exe = root / "speedtree_collision_cli.exe"
            hook = root / "speedtree_collision_hook.dll"
            spm = root / "tree.spm"
            options = root / "fbx.ini"
            target = root / "tree.fbx"
            exe.write_bytes(b"exe")
            hook.write_bytes(b"hook-v1")
            spm.write_bytes(b"spm")
            options.write_text(
                "[Options]\nTextureSkipWriting=true\n",
                encoding="utf-8",
            )

            first, first_inputs = speedtree_cli._input_fingerprint(
                exe, spm, options, "fbx", target
            )
            hook.write_bytes(b"hook-v2")
            second, second_inputs = speedtree_cli._input_fingerprint(
                exe, spm, options, "fbx", target
            )

            self.assertNotEqual(first, second)
            self.assertNotEqual(
                first_inputs["speedtree_hook"]["sha256"],
                second_inputs["speedtree_hook"]["sha256"],
            )

    def test_zero_geometry_native_receipt_is_valid(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            spm = root / "tree.spm"
            spm.write_bytes(b"spm")
            stat = spm.stat()
            receipt = root / "tree.speedtree_native_receipt.json"
            receipt.write_text(
                json.dumps({
                    "schema_version": 2,
                    "kind": "speedtree_native_export_receipt",
                    "status": "ready",
                    "source": {
                        "path": str(spm.resolve()),
                        "size": stat.st_size,
                        "last_write_time_100ns": (
                            stat.st_mtime_ns // 100
                            + 116444736000000000
                        ),
                    },
                    "geometry_count": 0,
                    "geometries": [],
                }),
                encoding="utf-8",
            )

            self.assertTrue(
                speedtree_cli._native_receipt_is_valid(receipt, spm)
            )

    def test_native_receipt_is_promoted_and_required_by_fbx_cache(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            exe = root / "speedtree_collision_cli.exe"
            spm = root / "tree.spm"
            fbx_options = root / "fbx.ini"
            xml_options = root / "xml.ini"
            exe.write_bytes(b"exe")
            exe.with_name("speedtree_collision_hook.dll").write_bytes(b"hook")
            spm.write_bytes(b"spm")
            preset = "[Options]\nTextureSkipWriting=true\n"
            fbx_options.write_text(preset, encoding="utf-8")
            xml_options.write_text(preset, encoding="utf-8")
            fbx = root / "out" / "fbx" / "tree.fbx"
            xml = root / "out" / "xml" / "tree.xml"
            receipt = fbx.with_name("tree.speedtree_native_receipt.json")
            calls = []
            original = speedtree_cli._run_process

            def fake_run(command, cwd, timeout_seconds):
                calls.append(list(command))
                primary = Path(command[command.index("-export") + 1])
                secondary = Path(command[command.index("--secondary-export") + 1])
                staged_receipt = Path(
                    command[command.index("--native-receipt") + 1]
                )
                primary.write_bytes(b"fbx")
                primary.with_suffix(".stmat").write_text(
                    "<Materials />", encoding="utf-8"
                )
                secondary.write_text("<SpeedTreeRaw />", encoding="utf-8")
                self._write_native_receipt(staged_receipt, spm)
                return 0, "ok", ""

            speedtree_cli._run_process = fake_run
            try:
                first = speedtree_cli.export_bundle(
                    exe,
                    spm,
                    [("fbx", fbx, fbx_options), ("xml", xml, xml_options)],
                    native_receipt=receipt,
                )
                second = speedtree_cli.export_bundle(
                    exe,
                    spm,
                    [("fbx", fbx, fbx_options), ("xml", xml, xml_options)],
                    native_receipt=receipt,
                )
            finally:
                speedtree_cli._run_process = original

            self.assertEqual(len(calls), 1)
            self.assertTrue(receipt.is_file())
            self.assertEqual(first["fbx"]["native_receipt"], str(receipt))
            self.assertTrue(second["fbx"]["cache_hit"])
            self.assertTrue(second["xml"]["cache_hit"])

    def test_two_misses_share_one_process_then_hit_independent_caches(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            exe = root / "speedtree_collision_cli.exe"
            spm = root / "tree.spm"
            fbx_options = root / "fbx.ini"
            xml_options = root / "xml.ini"
            exe.write_bytes(b"exe")
            exe.with_name("speedtree_collision_hook.dll").write_bytes(b"hook")
            spm.write_bytes(b"spm")
            preset = "[Options]\nTextureSkipWriting=true\n"
            fbx_options.write_text(preset, encoding="utf-8")
            xml_options.write_text(preset, encoding="utf-8")
            fbx = root / "out" / "fbx" / "tree.fbx"
            xml = root / "out" / "xml" / "tree.xml"
            calls = []

            original = speedtree_cli._run_process

            def fake_run(command, cwd, timeout_seconds):
                calls.append(list(command))
                primary = Path(command[command.index("-export") + 1])
                secondary = Path(command[command.index("--secondary-export") + 1])
                primary.parent.mkdir(parents=True, exist_ok=True)
                secondary.parent.mkdir(parents=True, exist_ok=True)
                primary.write_bytes(b"fbx")
                primary.with_suffix(".stmat").write_text(
                    "<Materials />", encoding="utf-8"
                )
                secondary.write_text("<SpeedTreeRaw />", encoding="utf-8")
                return 0, "ok", ""

            speedtree_cli._run_process = fake_run
            try:
                first = speedtree_cli.export_bundle(
                    exe,
                    spm,
                    [
                        ("fbx", fbx, fbx_options),
                        ("xml", xml, xml_options),
                    ],
                )
                second = speedtree_cli.export_bundle(
                    exe,
                    spm,
                    [
                        ("fbx", fbx, fbx_options),
                        ("xml", xml, xml_options),
                    ],
                )
            finally:
                speedtree_cli._run_process = original

            self.assertEqual(len(calls), 1)
            self.assertIn("--secondary-export-options", calls[0])
            self.assertTrue(first["fbx"]["bundled_process"])
            self.assertTrue(first["xml"]["bundled_process"])
            self.assertTrue(second["fbx"]["cache_hit"])
            self.assertTrue(second["xml"]["cache_hit"])
            self.assertEqual(fbx.read_bytes(), b"fbx")
            self.assertEqual(xml.read_text(encoding="utf-8"), "<SpeedTreeRaw />")

    def test_bundle_timeout_falls_back_to_independent_exports(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            exe = root / "speedtree_collision_cli.exe"
            spm = root / "tree.spm"
            fbx_options = root / "fbx.ini"
            xml_options = root / "xml.ini"
            exe.write_bytes(b"exe")
            exe.with_name("speedtree_collision_hook.dll").write_bytes(b"hook")
            spm.write_bytes(b"spm")
            preset = "[Options]\nTextureSkipWriting=true\n"
            fbx_options.write_text(preset, encoding="utf-8")
            xml_options.write_text(preset, encoding="utf-8")
            fbx = root / "out" / "fbx" / "tree.fbx"
            xml = root / "out" / "xml" / "tree.xml"
            calls = []

            original = speedtree_cli._run_process

            def fake_run(command, cwd, timeout_seconds):
                calls.append(list(command))
                if "--secondary-export" in command:
                    raise subprocess.TimeoutExpired(command, timeout_seconds)
                self.assertIn("--verification-only", command)
                target = Path(command[command.index("-export") + 1])
                target.parent.mkdir(parents=True, exist_ok=True)
                if target.suffix.lower() == ".fbx":
                    target.write_bytes(b"fbx")
                    target.with_suffix(".stmat").write_text(
                        "<Materials />", encoding="utf-8"
                    )
                else:
                    target.write_text("<SpeedTreeRaw />", encoding="utf-8")
                return 0, "ok", ""

            speedtree_cli._run_process = fake_run
            try:
                result = speedtree_cli.export_bundle(
                    exe,
                    spm,
                    [
                        ("fbx", fbx, fbx_options),
                        ("xml", xml, xml_options),
                    ],
                )
            finally:
                speedtree_cli._run_process = original

            self.assertEqual(len(calls), 3)
            self.assertTrue(result["fbx"]["bundle_fallback"])
            self.assertTrue(result["xml"]["bundle_fallback"])
            self.assertFalse(result["fbx"]["bundled_process"])
            self.assertFalse(result["xml"]["bundled_process"])
            self.assertTrue(result["fbx"]["verification_only"])
            self.assertTrue(result["xml"]["verification_only"])
            self.assertEqual(fbx.read_bytes(), b"fbx")
            self.assertEqual(xml.read_text(encoding="utf-8"), "<SpeedTreeRaw />")

    def test_single_target_timeout_falls_back_without_collision_bake(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            exe = root / "speedtree_collision_cli.exe"
            spm = root / "tree.spm"
            options = root / "fbx.ini"
            target = root / "out" / "tree.fbx"
            exe.write_bytes(b"exe")
            exe.with_name("speedtree_collision_hook.dll").write_bytes(b"hook")
            spm.write_bytes(b"spm")
            options.write_text(
                "[Options]\nTextureSkipWriting=true\n", encoding="utf-8"
            )
            calls = []
            original = speedtree_cli._run_process

            def fake_run(command, cwd, timeout_seconds):
                calls.append(list(command))
                if "--verification-only" not in command:
                    raise subprocess.TimeoutExpired(command, timeout_seconds)
                staged = Path(command[command.index("-export") + 1])
                staged.write_bytes(b"fbx")
                staged.with_suffix(".stmat").write_text(
                    "<Materials />", encoding="utf-8"
                )
                return 0, "ok", ""

            speedtree_cli._run_process = fake_run
            try:
                result = speedtree_cli.export_target(
                    exe, spm, options, "fbx", target
                )
            finally:
                speedtree_cli._run_process = original

            self.assertEqual(len(calls), 2)
            self.assertTrue(result["collision_fallback"])
            self.assertTrue(result["verification_only"])
            self.assertEqual(target.read_bytes(), b"fbx")

    def test_bundle_failure_preserves_existing_independent_outputs(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            exe = root / "speedtree_collision_cli.exe"
            spm = root / "tree.spm"
            fbx_options = root / "fbx.ini"
            xml_options = root / "xml.ini"
            fbx = root / "out" / "fbx" / "tree.fbx"
            xml = root / "out" / "xml" / "tree.xml"
            exe.write_bytes(b"exe")
            exe.with_name("speedtree_collision_hook.dll").write_bytes(b"hook")
            spm.write_bytes(b"spm")
            preset = "[Options]\nTextureSkipWriting=true\n"
            fbx_options.write_text(preset, encoding="utf-8")
            xml_options.write_text(preset, encoding="utf-8")
            fbx.parent.mkdir(parents=True)
            xml.parent.mkdir(parents=True)
            fbx.write_bytes(b"previous-fbx")
            fbx.with_suffix(".stmat").write_text(
                "<Materials />", encoding="utf-8"
            )
            xml.write_text("<SpeedTreeRaw />", encoding="utf-8")
            for output in (fbx, xml):
                cache = speedtree_cli._cache_path(output)
                cache.parent.mkdir(parents=True, exist_ok=True)
                cache.write_text("{}", encoding="utf-8")
            calls = []
            original = speedtree_cli._run_process

            def fake_run(command, cwd, timeout_seconds):
                calls.append(list(command))
                raise subprocess.TimeoutExpired(command, timeout_seconds)

            speedtree_cli._run_process = fake_run
            try:
                result = speedtree_cli.export_bundle(
                    exe,
                    spm,
                    [
                        ("fbx", fbx, fbx_options),
                        ("xml", xml, xml_options),
                    ],
                )
            finally:
                speedtree_cli._run_process = original

            self.assertEqual(len(calls), 1)
            self.assertTrue(result["fbx"]["preserved_existing_output"])
            self.assertTrue(result["xml"]["preserved_existing_output"])
            self.assertEqual(fbx.read_bytes(), b"previous-fbx")
            self.assertEqual(xml.read_text(encoding="utf-8"), "<SpeedTreeRaw />")


if __name__ == "__main__":
    unittest.main()
