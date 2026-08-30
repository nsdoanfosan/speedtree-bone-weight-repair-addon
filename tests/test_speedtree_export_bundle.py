import importlib.util
import ast
import codecs
import hashlib
import json
import subprocess
import tempfile
import unittest
from contextlib import contextmanager
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
    def test_core_exposes_a_separate_explicit_gateway_operation(self):
        core_path = MODULE_PATH.with_name("core.py")
        tree = ast.parse(core_path.read_text(encoding="utf-8"))
        functions = {
            node.name: node
            for node in tree.body
            if isinstance(node, ast.FunctionDef)
        }
        normal = functions["run_speedtree_cli_export"]
        fresh = functions["run_fresh_verification_only_export"]
        self.assertEqual(
            [argument.arg for argument in normal.args.args],
            [argument.arg for argument in fresh.args.args],
        )

        def selected_mode(function):
            calls = [
                node
                for node in ast.walk(function)
                if isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "_run_speedtree_cli_export"
            ]
            self.assertEqual(len(calls), 1)
            keyword = next(
                row
                for row in calls[0].keywords
                if row.arg == "fresh_verification_only"
            )
            self.assertIsInstance(keyword.value, ast.Constant)
            return keyword.value.value

        self.assertIs(selected_mode(normal), False)
        self.assertIs(selected_mode(fresh), True)
        fresh_source = ast.get_source_segment(
            core_path.read_text(encoding="utf-8"),
            fresh,
        )
        self.assertIn("if force_reexport is not True:", fresh_source)
        self.assertIn(
            "if export_fbx is not True or export_xml is not True:",
            fresh_source,
        )

    @staticmethod
    def _policy_export_fixture(root):
        exe = root / "speedtree_collision_cli.exe"
        hook = root / "speedtree_collision_hook.dll"
        spm = root / "tree.spm"
        options = root / "fbx.ini"
        target = root / "out" / "tree.fbx"
        exe.write_bytes(b"exe")
        hook.write_bytes(b"hook")
        spm.write_text(
            "<SpeedTreeModel><Generators /></SpeedTreeModel>",
            encoding="utf-8",
        )
        options.write_text(
            "[Options]\nTextureSkipWriting=true\n",
            encoding="utf-8",
        )
        return exe, spm, options, target

    def test_minimum_bone_export_transaction_seals_policy_before_export(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            exe, spm, options, target = self._policy_export_fixture(root)
            original = spm.read_bytes()
            updated = original.replace(b" />", b"></Generators>")
            backup = root / "tree.spm.backup"
            events = []
            originals = (
                speedtree_cli.speedtree_export_gate,
                speedtree_cli.ensure_minimum_absolute_branch_bones,
                speedtree_cli.export_bundle,
            )

            @contextmanager
            def gate():
                events.append("gate_enter")
                yield
                events.append("gate_exit")

            def ensure(path):
                events.append("policy")
                backup.write_bytes(original)
                Path(path).write_bytes(updated)
                return {
                    "status": "updated",
                    "spm": str(Path(path).resolve()),
                    "changed": True,
                    "changed_generator_count": 1,
                    "changed_generators": [{"name": "Branch"}],
                    "backup": str(backup),
                    "source_sha256": hashlib.sha256(original).hexdigest(),
                    "updated_sha256": hashlib.sha256(updated).hexdigest(),
                    "policy": (
                        "non_cluster_zero_bone_branch_to_absolute_one_v1"
                    ),
                }

            def export(**_kwargs):
                events.append("export")
                self.assertEqual(spm.read_bytes(), updated)
                return {"fbx": {"status": "ok"}}

            speedtree_cli.speedtree_export_gate = gate
            speedtree_cli.ensure_minimum_absolute_branch_bones = ensure
            speedtree_cli.export_bundle = export
            policy_report = {}
            try:
                result = (
                    speedtree_cli.export_bundle_with_minimum_bone_policy(
                        exe,
                        spm,
                        [("fbx", target, options)],
                        policy_report=policy_report,
                    )
                )
            finally:
                (
                    speedtree_cli.speedtree_export_gate,
                    speedtree_cli.ensure_minimum_absolute_branch_bones,
                    speedtree_cli.export_bundle,
                ) = originals

            self.assertEqual(
                events,
                ["gate_enter", "policy", "export", "gate_exit"],
            )
            self.assertEqual(result["fbx"]["status"], "ok")
            self.assertEqual(
                policy_report["spm_bone_policy"]["updated_sha256"],
                hashlib.sha256(updated).hexdigest(),
            )

    def test_minimum_bone_export_transaction_rejects_export_time_spm_drift(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            exe, spm, options, target = self._policy_export_fixture(root)
            digest = hashlib.sha256(spm.read_bytes()).hexdigest()
            originals = (
                speedtree_cli.ensure_minimum_absolute_branch_bones,
                speedtree_cli.export_bundle,
            )

            def ensure(path):
                return {
                    "status": "already_compliant",
                    "spm": str(Path(path).resolve()),
                    "changed": False,
                    "changed_generator_count": 0,
                    "changed_generators": [],
                    "backup": "",
                    "source_sha256": digest,
                    "policy": (
                        "non_cluster_zero_bone_branch_to_absolute_one_v1"
                    ),
                }

            def export(**_kwargs):
                spm.write_bytes(spm.read_bytes() + b"external-drift")
                return {"fbx": {"status": "ok"}}

            speedtree_cli.ensure_minimum_absolute_branch_bones = ensure
            speedtree_cli.export_bundle = export
            try:
                with self.assertRaisesRegex(
                    RuntimeError,
                    "changed during the sealed export transaction",
                ):
                    speedtree_cli.export_bundle_with_minimum_bone_policy(
                        exe,
                        spm,
                        [("fbx", target, options)],
                    )
            finally:
                (
                    speedtree_cli.ensure_minimum_absolute_branch_bones,
                    speedtree_cli.export_bundle,
                ) = originals

    def test_minimum_bone_export_transaction_rejects_malformed_policy(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            exe, spm, options, target = self._policy_export_fixture(root)
            original = speedtree_cli.ensure_minimum_absolute_branch_bones
            speedtree_cli.ensure_minimum_absolute_branch_bones = (
                lambda _path: {"status": "failed"}
            )
            try:
                with self.assertRaisesRegex(
                    RuntimeError,
                    "unsupported status",
                ):
                    speedtree_cli.export_bundle_with_minimum_bone_policy(
                        exe,
                        spm,
                        [("fbx", target, options)],
                    )
            finally:
                speedtree_cli.ensure_minimum_absolute_branch_bones = original

    def test_fresh_verification_only_is_one_sealed_bundle_without_collision(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            exe, spm, fbx_options, fbx = self._policy_export_fixture(root)
            xml_options = root / "xml.ini"
            xml_options.write_text(
                "[Options]\nTextureSkipWriting=true\n",
                encoding="utf-8",
            )
            xml = root / "xml" / "tree.xml"
            receipt = fbx.parent / "tree.speedtree_native_receipt.json"
            calls = []
            original = speedtree_cli._run_process

            def fake_run(command, cwd, timeout_seconds):
                calls.append(list(command))
                self.assertIn("--verification-only", command)
                primary = Path(command[command.index("-export") + 1])
                secondary = Path(
                    command[command.index("--secondary-export") + 1]
                )
                staged_receipt = Path(
                    command[command.index("--native-receipt") + 1]
                )
                primary.parent.mkdir(parents=True, exist_ok=True)
                secondary.parent.mkdir(parents=True, exist_ok=True)
                primary.write_bytes(b"fresh-fbx")
                primary.with_suffix(".stmat").write_text(
                    "<Materials />",
                    encoding="utf-8",
                )
                secondary.write_text("<SpeedTreeRaw />", encoding="utf-8")
                self._write_native_receipt(staged_receipt, spm)
                return (
                    0,
                    speedtree_cli.FRESH_VERIFICATION_SEALED_MARKER,
                    "",
                )

            speedtree_cli._run_process = fake_run
            policy_report = {}
            try:
                result = (
                    speedtree_cli
                    .export_fresh_verification_bundle_with_minimum_bone_policy(
                        exe,
                        spm,
                        [
                            ("fbx", fbx, fbx_options),
                            ("xml", xml, xml_options),
                        ],
                        native_receipt=receipt,
                        force_reexport=True,
                        policy_report=policy_report,
                    )
                )
            finally:
                speedtree_cli._run_process = original

            self.assertEqual(len(calls), 1)
            self.assertTrue(result["fbx"]["verification_only"])
            self.assertTrue(result["xml"]["verification_only"])
            self.assertTrue(result["fbx"]["bundled_process"])
            self.assertFalse(result["fbx"]["bundle_fallback"])
            evidence = policy_report["fresh_verification_only_export"]
            self.assertEqual(
                evidence["collision_prune_bundle_attempt_count"], 0
            )
            self.assertEqual(evidence["verification_bundle_attempt_count"], 1)
            self.assertEqual(evidence["independent_fallback_attempt_count"], 0)
            self.assertEqual(
                evidence["launcher_sealed_completion"]["status"],
                "observed",
            )
            self.assertEqual(fbx.read_bytes(), b"fresh-fbx")
            self.assertTrue(receipt.is_file())
            self.assertTrue(xml.is_file())

    def test_fresh_verification_only_missing_sealed_marker_fails_without_retry(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            exe, spm, fbx_options, fbx = self._policy_export_fixture(root)
            xml_options = root / "xml.ini"
            xml_options.write_text(
                "[Options]\nTextureSkipWriting=true\n",
                encoding="utf-8",
            )
            xml = root / "xml" / "tree.xml"
            receipt = fbx.parent / "tree.speedtree_native_receipt.json"
            calls = []
            original = speedtree_cli._run_process

            def fake_run(command, cwd, timeout_seconds):
                calls.append(list(command))
                primary = Path(command[command.index("-export") + 1])
                secondary = Path(
                    command[command.index("--secondary-export") + 1]
                )
                staged_receipt = Path(
                    command[command.index("--native-receipt") + 1]
                )
                primary.parent.mkdir(parents=True, exist_ok=True)
                secondary.parent.mkdir(parents=True, exist_ok=True)
                primary.write_bytes(b"unsealed-fbx")
                primary.with_suffix(".stmat").write_text(
                    "<Materials />",
                    encoding="utf-8",
                )
                secondary.write_text("<SpeedTreeRaw />", encoding="utf-8")
                self._write_native_receipt(staged_receipt, spm)
                return 0, "missing marker", ""

            speedtree_cli._run_process = fake_run
            try:
                with self.assertRaisesRegex(
                    RuntimeError,
                    "launcher_sealed_completion_marker",
                ):
                    speedtree_cli.export_fresh_verification_bundle_with_minimum_bone_policy(
                        exe,
                        spm,
                        [
                            ("fbx", fbx, fbx_options),
                            ("xml", xml, xml_options),
                        ],
                        native_receipt=receipt,
                        force_reexport=True,
                    )
            finally:
                speedtree_cli._run_process = original

            self.assertEqual(len(calls), 1)
            self.assertFalse(fbx.exists())
            self.assertFalse(xml.exists())
            self.assertFalse(receipt.exists())

    def test_fresh_verification_only_timeout_fails_without_retry_or_promotion(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            exe, spm, fbx_options, fbx = self._policy_export_fixture(root)
            xml_options = root / "xml.ini"
            xml_options.write_text(
                "[Options]\nTextureSkipWriting=true\n",
                encoding="utf-8",
            )
            xml = root / "xml" / "tree.xml"
            receipt = fbx.parent / "tree.speedtree_native_receipt.json"
            calls = []
            original = speedtree_cli._run_process

            def fake_run(command, cwd, timeout_seconds):
                calls.append(list(command))
                raise subprocess.TimeoutExpired(command, timeout_seconds)

            speedtree_cli._run_process = fake_run
            try:
                with self.assertRaisesRegex(
                    RuntimeError,
                    "timed out before launcher-sealed completion",
                ):
                    speedtree_cli.export_fresh_verification_bundle_with_minimum_bone_policy(
                        exe,
                        spm,
                        [
                            ("fbx", fbx, fbx_options),
                            ("xml", xml, xml_options),
                        ],
                        native_receipt=receipt,
                        force_reexport=True,
                    )
            finally:
                speedtree_cli._run_process = original

            self.assertEqual(len(calls), 1)
            self.assertIn("--verification-only", calls[0])
            self.assertFalse(fbx.exists())
            self.assertFalse(xml.exists())
            self.assertFalse(receipt.exists())

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

    def test_reconcile_accepts_modeler_decimal_comma_coordinates(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            spm = root / "tree.spm"
            xml = root / "tree.xml"
            receipt = root / "tree.speedtree_native_receipt.json"
            spm.write_bytes(b"spm")
            xml.write_bytes(self._root_like_xml_bytes())
            self._write_synthetic_native_receipt(receipt, spm)
            speedtree_cli.reconcile_xml_with_native_receipt(
                xml, receipt, spm, create_backup=False
            )
            xml.write_bytes(
                xml.read_bytes().replace(b'EndZ="30.48"', b'EndZ="30,48"')
            )

            result = speedtree_cli.reconcile_xml_with_native_receipt(
                xml, receipt, spm, create_backup=False
            )

            self.assertEqual(result["status"], "already_reconciled")
            self.assertFalse(result["changed"])

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

    def test_force_reexport_bypasses_valid_bundle_cache(self):
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
            calls = []
            original = speedtree_cli._run_process

            def fake_run(command, cwd, timeout_seconds):
                calls.append(list(command))
                primary = Path(command[command.index("-export") + 1])
                secondary = Path(
                    command[command.index("--secondary-export") + 1]
                )
                marker = str(len(calls)).encode("ascii")
                primary.parent.mkdir(parents=True, exist_ok=True)
                secondary.parent.mkdir(parents=True, exist_ok=True)
                primary.write_bytes(b"fbx-" + marker)
                primary.with_suffix(".stmat").write_text(
                    "<Materials />", encoding="utf-8"
                )
                secondary.write_text(
                    f'<SpeedTreeRaw run="{len(calls)}" />',
                    encoding="utf-8",
                )
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
                cached = speedtree_cli.export_bundle(
                    exe,
                    spm,
                    [
                        ("fbx", fbx, fbx_options),
                        ("xml", xml, xml_options),
                    ],
                )
                forced = speedtree_cli.export_bundle(
                    exe,
                    spm,
                    [
                        ("fbx", fbx, fbx_options),
                        ("xml", xml, xml_options),
                    ],
                    force_reexport=True,
                )
            finally:
                speedtree_cli._run_process = original

            self.assertEqual(len(calls), 2)
            self.assertFalse(first["fbx"]["cache_hit"])
            self.assertTrue(cached["fbx"]["cache_hit"])
            self.assertTrue(cached["xml"]["cache_hit"])
            self.assertFalse(forced["fbx"]["cache_hit"])
            self.assertFalse(forced["xml"]["cache_hit"])
            self.assertTrue(forced["fbx"]["force_reexport_requested"])
            self.assertTrue(forced["xml"]["force_reexport_requested"])
            self.assertEqual(fbx.read_bytes(), b"fbx-2")
            self.assertEqual(
                xml.read_text(encoding="utf-8"),
                '<SpeedTreeRaw run="2" />',
            )

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

    def test_force_reexport_failure_preserves_outputs_and_cache(self):
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
            original = speedtree_cli._run_process

            def successful_run(command, cwd, timeout_seconds):
                primary = Path(command[command.index("-export") + 1])
                secondary = Path(
                    command[command.index("--secondary-export") + 1]
                )
                primary.parent.mkdir(parents=True, exist_ok=True)
                secondary.parent.mkdir(parents=True, exist_ok=True)
                primary.write_bytes(b"known-good-fbx")
                primary.with_suffix(".stmat").write_text(
                    "<Materials />", encoding="utf-8"
                )
                secondary.write_text(
                    "<SpeedTreeRaw />", encoding="utf-8"
                )
                return 0, "ok", ""

            speedtree_cli._run_process = successful_run
            try:
                speedtree_cli.export_bundle(
                    exe,
                    spm,
                    [
                        ("fbx", fbx, fbx_options),
                        ("xml", xml, xml_options),
                    ],
                )
                protected = {
                    path: path.read_bytes()
                    for path in (
                        fbx,
                        fbx.with_suffix(".stmat"),
                        xml,
                        speedtree_cli._cache_path(fbx),
                        speedtree_cli._cache_path(xml),
                    )
                }

                def failing_run(command, cwd, timeout_seconds):
                    raise subprocess.TimeoutExpired(command, timeout_seconds)

                speedtree_cli._run_process = failing_run
                with self.assertRaises(RuntimeError):
                    speedtree_cli.export_bundle(
                        exe,
                        spm,
                        [
                            ("fbx", fbx, fbx_options),
                            ("xml", xml, xml_options),
                        ],
                        force_reexport=True,
                    )
            finally:
                speedtree_cli._run_process = original

            for path, expected in protected.items():
                self.assertEqual(path.read_bytes(), expected)

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
