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
SPEC = importlib.util.spec_from_file_location("bwr_relative_one_test", MODULE_PATH)
speedtree_cli = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(speedtree_cli)


def prop(name, value):
    return f"<Property><Name>{name}</Name><Value>{value}</Value></Property>"


def generator(name, kind, style, bones, hidden=False):
    return (
        f'<Generator Type="{kind}"><GUID>{name}-guid</GUID><Name>{name}</Name>'
        f"<Hidden>{str(hidden).lower()}</Hidden>"
        + prop("Physics:Bone style", style)
        + prop("Physics:Bones", bones)
        + "</Generator>"
    )


def document():
    return (
        "<SpeedTree><Generators>"
        + generator("relative_high", "Branch", 1, 3.5)
        + generator("relative_low", "Spline Branch", 1, 0.4)
        + generator("relative_one", "Branch", 1, 1)
        + generator("relative_hidden", "Branch", 1, 8, hidden=True)
        + generator("absolute", "Branch", 0, 7)
        + generator("other", "Branch", 2, 9)
        + generator("leaf", "Leaf Mesh", 1, 6)
        + "</Generators></SpeedTree>"
    )


def values(raw):
    if raw.startswith(b"\x1f\x8b"):
        raw = gzip.decompress(raw)
    root = ET.fromstring(raw)
    result = {}
    for item in root.find("Generators").findall("Generator"):
        props = {
            row.findtext("Name"): row.findtext("Value")
            for row in item.findall("Property")
        }
        result[item.findtext("Name")] = props
    return result


class RelativeBoneOnePolicyTests(unittest.TestCase):
    def test_plan_is_read_only_and_includes_every_relative_branch(self):
        with tempfile.TemporaryDirectory() as td:
            spm = Path(td) / "SK_tree_test.spm"
            original = document().encode("utf-8")
            spm.write_bytes(original)

            plan = speedtree_cli.plan_relative_branch_bones_one(spm)

            self.assertEqual(plan["status"], "planned")
            self.assertEqual(plan["relative_generator_count"], 4)
            self.assertEqual(plan["changed_generator_count"], 3)
            self.assertEqual(
                {row["name"] for row in plan["changed_generators"]},
                {"relative_high", "relative_low", "relative_hidden"},
            )
            self.assertEqual(spm.read_bytes(), original)
            self.assertFalse((spm.parent / "_spm_backups").exists())

    def test_plain_and_gzip_apply_exactly_one_with_verified_backup(self):
        for compressed in (False, True):
            with self.subTest(compressed=compressed), tempfile.TemporaryDirectory() as td:
                spm = Path(td) / "SK_tree_test.spm"
                xml = document().encode("utf-8")
                original = (
                    gzip.compress(xml, compresslevel=9, mtime=0)
                    if compressed
                    else xml
                )
                spm.write_bytes(original)

                receipt = speedtree_cli.apply_relative_branch_bones_one(spm)

                self.assertEqual(receipt["status"], "updated")
                self.assertEqual(receipt["changed_generator_count"], 3)
                backup = Path(receipt["backup"])
                self.assertEqual(backup.read_bytes(), original)
                self.assertEqual(
                    hashlib.sha256(backup.read_bytes()).hexdigest(),
                    receipt["source_sha256"],
                )
                current = values(spm.read_bytes())
                for name in (
                    "relative_high",
                    "relative_low",
                    "relative_one",
                    "relative_hidden",
                ):
                    self.assertEqual(current[name]["Physics:Bone style"], "1")
                    self.assertEqual(current[name]["Physics:Bones"], "1")
                self.assertEqual(current["absolute"]["Physics:Bones"], "7")
                self.assertEqual(current["other"]["Physics:Bones"], "9")
                self.assertEqual(current["leaf"]["Physics:Bones"], "6")

                second = speedtree_cli.apply_relative_branch_bones_one(spm)
                self.assertEqual(second["status"], "already_compliant")
                self.assertFalse(second["changed"])
                self.assertEqual(second["backup"], "")

    def test_invalid_relative_value_fails_without_writes(self):
        with tempfile.TemporaryDirectory() as td:
            spm = Path(td) / "SK_tree_invalid.spm"
            original = (
                "<SpeedTree><Generators>"
                + generator("bad", "Branch", 1, "NaN")
                + "</Generators></SpeedTree>"
            ).encode("utf-8")
            spm.write_bytes(original)

            with self.assertRaisesRegex(RuntimeError, "non-finite"):
                speedtree_cli.apply_relative_branch_bones_one(spm)

            self.assertEqual(spm.read_bytes(), original)
            self.assertFalse((spm.parent / "_spm_backups").exists())


if __name__ == "__main__":
    unittest.main()
