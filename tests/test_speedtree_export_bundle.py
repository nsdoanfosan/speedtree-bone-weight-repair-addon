import importlib.util
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
    def test_two_misses_share_one_process_then_hit_independent_caches(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            exe = root / "speedtree_collision_cli.exe"
            spm = root / "tree.spm"
            fbx_options = root / "fbx.ini"
            xml_options = root / "xml.ini"
            exe.write_bytes(b"exe")
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
