import gzip
import hashlib
import importlib.util
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path


MODULE_PATH = (
    Path(__file__).resolve().parents[1]
    / "addons"
    / "speedtree_bone_weight_repair"
    / "speedtree_cli.py"
)
SPEC = importlib.util.spec_from_file_location(
    "bwr_speedtree_cli_minimum_bones_test", MODULE_PATH
)
speedtree_cli = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(speedtree_cli)


def _property(name, value):
    return (
        "<Property><Name>"
        + name
        + "</Name><Value>"
        + str(value)
        + "</Value></Property>"
    )


def _generator(name, generator_type, bones, style, *, hidden=False):
    return (
        f'<Generator Type="{generator_type}">'
        f"<GUID>{name}-guid</GUID><Name>{name}</Name>"
        f"<Hidden>{str(hidden).lower()}</Hidden>"
        + _property("Physics:Bone style", style)
        + _property("Physics:Bones", bones)
        + "</Generator>"
    )


def _document():
    return (
        '<?xml version="1.0" encoding="utf-8"?>'
        "<SpeedTree><Generators>"
        + _generator("visible_zero", "Branch", 0, 2)
        + _generator("hidden_zero", "Spline Branch", 0.0, 1, hidden=True)
        + _generator("positive", "Branch", 3, 1)
        + _generator("leaf", "Leaf Mesh", 0, 2)
        + "</Generators><Nodes /></SpeedTree>"
    )


def _generator_values(raw):
    if raw.startswith(b"\x1f\x8b"):
        raw = gzip.decompress(raw)
    root = ET.fromstring(raw.decode("utf-8"))
    values = {}
    for generator in root.find("Generators").findall("Generator"):
        properties = {}
        for element in generator.iter("Property"):
            properties[element.findtext("Name")] = element.findtext("Value")
        values[generator.findtext("Name")] = properties
    return values


class MinimumAbsoluteBranchBonePolicyTests(unittest.TestCase):
    def test_plain_and_gzip_spm_repair_visible_and_hidden_zero_bone_branches(self):
        for compressed in (False, True):
            with self.subTest(compressed=compressed), tempfile.TemporaryDirectory() as td:
                spm = Path(td) / "SK_tree_test.spm"
                original_xml = _document().encode("utf-8")
                original = (
                    gzip.compress(original_xml, compresslevel=9, mtime=0)
                    if compressed
                    else original_xml
                )
                spm.write_bytes(original)

                report = speedtree_cli.ensure_minimum_absolute_branch_bones(spm)

                self.assertEqual(report["status"], "updated")
                self.assertEqual(report["changed_generator_count"], 2)
                self.assertEqual(
                    {row["name"] for row in report["changed_generators"]},
                    {"visible_zero", "hidden_zero"},
                )
                self.assertEqual(
                    {row["hidden"] for row in report["changed_generators"]},
                    {False, True},
                )
                backup = Path(report["backup"])
                self.assertEqual(backup.read_bytes(), original)
                self.assertEqual(
                    hashlib.sha256(backup.read_bytes()).hexdigest(),
                    report["source_sha256"],
                )
                values = _generator_values(spm.read_bytes())
                for name in ("visible_zero", "hidden_zero"):
                    self.assertEqual(values[name]["Physics:Bone style"], "0")
                    self.assertEqual(values[name]["Physics:Bones"], "1")
                self.assertEqual(values["positive"]["Physics:Bones"], "3")
                self.assertEqual(values["positive"]["Physics:Bone style"], "1")
                self.assertEqual(values["leaf"]["Physics:Bones"], "0")
                self.assertEqual(values["leaf"]["Physics:Bone style"], "2")

                second = speedtree_cli.ensure_minimum_absolute_branch_bones(spm)
                self.assertEqual(second["status"], "already_compliant")
                self.assertFalse(second["changed"])

    def test_nested_force_generators_do_not_hide_the_root_generator_section(self):
        with tempfile.TemporaryDirectory() as td:
            spm = Path(td) / "SK_weed_ivy_test.spm"
            document = _document().replace(
                "<SpeedTree>",
                "<SpeedTree><Forces><Force><Extra><Generators>"
                "<Generator><Name>force_generator</Name></Generator>"
                "</Generators></Extra></Force></Forces>",
                1,
            )
            spm.write_text(document, encoding="utf-8")

            report = speedtree_cli.ensure_minimum_absolute_branch_bones(spm)

            self.assertEqual(report["status"], "updated")
            self.assertEqual(report["changed_generator_count"], 2)
            root = ET.fromstring(spm.read_text(encoding="utf-8"))
            nested = root.find("Forces/Force/Extra/Generators/Generator")
            self.assertEqual(nested.findtext("Name"), "force_generator")
            values = _generator_values(spm.read_bytes())
            self.assertEqual(values["visible_zero"]["Physics:Bones"], "1")
            self.assertEqual(values["hidden_zero"]["Physics:Bones"], "1")

    def test_root_generators_indentation_tail_does_not_change_section_identity(self):
        with tempfile.TemporaryDirectory() as td:
            spm = Path(td) / "SK_tree_pretty.spm"
            document = _document().replace(
                "</Generators><Nodes />",
                "</Generators>\n\t<Nodes />",
            )
            spm.write_text(document, encoding="utf-8")

            report = speedtree_cli.ensure_minimum_absolute_branch_bones(spm)

            self.assertEqual(report["status"], "updated")
            self.assertEqual(report["changed_generator_count"], 2)

    def test_empty_self_closing_root_generators_is_already_compliant(self):
        with tempfile.TemporaryDirectory() as td:
            spm = Path(td) / "SK_tree_empty_generators.spm"
            original = b"<SpeedTree><Generators /><Nodes /></SpeedTree>"
            spm.write_bytes(original)

            report = speedtree_cli.ensure_minimum_absolute_branch_bones(spm)

            self.assertEqual(report["status"], "already_compliant")
            self.assertFalse(report["changed"])
            self.assertEqual(spm.read_bytes(), original)

    def test_cluster_folder_and_cluster_stem_are_excluded_without_writes(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            targets = [
                root / "cluster" / "provider.spm",
                root / "SK_cluster_provider.spm",
            ]
            original = _document().encode("utf-8")
            for spm in targets:
                spm.parent.mkdir(parents=True, exist_ok=True)
                spm.write_bytes(original)
                report = speedtree_cli.ensure_minimum_absolute_branch_bones(spm)
                self.assertEqual(report["status"], "excluded_cluster_source")
                self.assertFalse(report["changed"])
                self.assertEqual(spm.read_bytes(), original)
                self.assertFalse((spm.parent / "_spm_backups").exists())

    def test_invalid_zero_bone_branch_is_not_mutated_or_backed_up(self):
        with tempfile.TemporaryDirectory() as td:
            spm = Path(td) / "SK_tree_invalid.spm"
            original = (
                "<SpeedTree><Generators>"
                '<Generator Type="Branch"><Name>bad</Name>'
                + _property("Physics:Bones", 0)
                + "</Generator></Generators></SpeedTree>"
            ).encode("utf-8")
            spm.write_bytes(original)

            with self.assertRaisesRegex(RuntimeError, "no Physics:Bone style"):
                speedtree_cli.ensure_minimum_absolute_branch_bones(spm)

            self.assertEqual(spm.read_bytes(), original)
            self.assertFalse((spm.parent / "_spm_backups").exists())

    def test_non_finite_branch_bone_count_fails_closed_without_mutation(self):
        with tempfile.TemporaryDirectory() as td:
            spm = Path(td) / "SK_tree_nan.spm"
            original = (
                "<SpeedTree><Generators>"
                + _generator("invalid", "Branch", "NaN", 0)
                + "</Generators></SpeedTree>"
            ).encode("utf-8")
            spm.write_bytes(original)

            with self.assertRaisesRegex(RuntimeError, "non-finite"):
                speedtree_cli.ensure_minimum_absolute_branch_bones(spm)

            self.assertEqual(spm.read_bytes(), original)
            self.assertFalse((spm.parent / "_spm_backups").exists())


if __name__ == "__main__":
    unittest.main()
