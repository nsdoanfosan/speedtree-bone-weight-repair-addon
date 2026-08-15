import importlib.util
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


if __name__ == "__main__":
    unittest.main()
