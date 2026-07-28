import importlib.util
import tempfile
import unittest
from pathlib import Path


MODULE_PATH = (
    Path(__file__).resolve().parents[1]
    / "addons"
    / "speedtree_bone_weight_repair"
    / "export_options_contract.py"
)
SPEC = importlib.util.spec_from_file_location(
    "bwr_export_options_contract_test", MODULE_PATH
)
contract = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(contract)


class ExportOptionsContractTests(unittest.TestCase):
    @staticmethod
    def _write_preset(path, value):
        Path(path).write_text(
            "[Options]\n"
            "Filetype=Autodesk FBX (*.fbx)\n"
            f"TextureSkipWriting={value}\n",
            encoding="utf-8",
        )

    def test_true_is_required_without_mutating_preset(self):
        with tempfile.TemporaryDirectory() as temporary:
            preset = Path(temporary) / "Options_MA_Fbx.ini"
            self._write_preset(preset, "true")
            before = preset.read_bytes()

            inspected = contract.require_texture_skip_writing(
                preset, purpose="production FBX"
            )

            self.assertEqual(inspected["status"], "ok")
            self.assertIs(inspected["texture_skip_writing"], True)
            self.assertEqual(preset.read_bytes(), before)

    def test_false_missing_and_copied_presets_are_fail_closed(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "Options_MA_Fbx.ini"
            copied = root / "_temporary" / "Options_MA_Fbx.ini"
            copied.parent.mkdir()
            self._write_preset(source, "false")
            copied.write_bytes(source.read_bytes())

            for preset in (source, copied):
                before = preset.read_bytes()
                inspected = contract.inspect_speedtree_export_options(preset)
                self.assertEqual(
                    inspected["status"], "texture_writing_enabled"
                )
                with self.assertRaisesRegex(
                    contract.SpeedTreeExportOptionsError,
                    "TextureSkipWriting=false",
                ):
                    contract.require_texture_skip_writing(preset)
                self.assertEqual(preset.read_bytes(), before)

            missing = contract.inspect_speedtree_export_options(
                root / "missing.ini"
            )
            self.assertEqual(missing["status"], "missing")


if __name__ == "__main__":
    unittest.main()
