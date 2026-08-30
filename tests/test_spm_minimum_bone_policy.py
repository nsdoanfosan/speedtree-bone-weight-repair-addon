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


def _node(
    name,
    node_type,
    generator_name,
    parent="",
    *,
    hidden=False,
    deleted=False,
    culled=False,
):
    generator_guid = f"{generator_name}-guid" if generator_name else ""
    return (
        f'<Node Type="{node_type}">'
        f"<GUID>{name}-node-guid</GUID><Name>{name}</Name>"
        f"<GeneratorGUID>{generator_guid}</GeneratorGUID>"
        f"<ParentGUID>{parent}</ParentGUID>"
        f"<Hidden>{str(hidden).lower()}</Hidden>"
        "<Extra>"
        f"<m_bDeleted>{str(deleted).lower()}</m_bDeleted>"
        f"<m_bCulled>{str(culled).lower()}</m_bCulled>"
        "</Extra>"
        "</Node>"
    )


def _link(source_generator_name, target_generator_name):
    return (
        "<Link>"
        f"<SourceGUID>{source_generator_name}-guid</SourceGUID>"
        f"<TargetGUID>{target_generator_name}-guid</TargetGUID>"
        "</Link>"
    )


def _document():
    return (
        '<?xml version="1.0" encoding="utf-8"?>'
        "<SpeedTree><Generators>"
        + _generator("visible_zero", "Branch", 0, 2)
        + _generator("hidden_zero", "Spline Branch", 0.0, 1, hidden=True)
        + _generator("unused_zero", "Branch", 0, 2)
        + _generator("base_ref_zero", "Spline Branch", 0, 1)
        + _generator("positive", "Branch", 3, 1)
        + _generator("leaf", "Leaf Mesh", 0, 2)
        + "</Generators><Nodes>"
        + _node("visible_branch", "Branch", "visible_zero")
        + _node("hidden_branch", "Branch", "hidden_zero")
        + _node("base", "BaseRef", "")
        + _node(
            "base_ref_branch",
            "Branch",
            "base_ref_zero",
            "base-node-guid",
        )
        + _node(
            "base_ref_leaf",
            "Leaf Mesh",
            "leaf",
            "base_ref_branch-node-guid",
        )
        + "</Nodes></SpeedTree>"
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
    def test_plain_and_gzip_spm_repair_only_live_visible_zero_bone_branches(self):
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
                    {"visible_zero", "base_ref_zero"},
                )
                self.assertEqual(
                    {row["hidden"] for row in report["changed_generators"]},
                    {False},
                )
                self.assertEqual(
                    {row["runtime_branch_node_count"]
                     for row in report["changed_generators"]},
                    {1},
                )
                backup = Path(report["backup"])
                self.assertEqual(backup.read_bytes(), original)
                self.assertEqual(
                    hashlib.sha256(backup.read_bytes()).hexdigest(),
                    report["source_sha256"],
                )
                values = _generator_values(spm.read_bytes())
                for name in ("visible_zero", "base_ref_zero"):
                    self.assertEqual(values[name]["Physics:Bone style"], "0")
                    self.assertEqual(values[name]["Physics:Bones"], "1")
                self.assertEqual(values["hidden_zero"]["Physics:Bones"], "0.0")
                self.assertEqual(values["hidden_zero"]["Physics:Bone style"], "1")
                self.assertEqual(values["unused_zero"]["Physics:Bones"], "0")
                self.assertEqual(values["unused_zero"]["Physics:Bone style"], "2")
                self.assertEqual(values["positive"]["Physics:Bones"], "3")
                self.assertEqual(values["positive"]["Physics:Bone style"], "1")
                self.assertEqual(values["leaf"]["Physics:Bones"], "0")
                self.assertEqual(values["leaf"]["Physics:Bone style"], "2")

                second = speedtree_cli.ensure_minimum_absolute_branch_bones(spm)
                self.assertEqual(second["status"], "already_compliant")
                self.assertFalse(second["changed"])

    def test_visible_generator_with_hidden_branch_node_is_not_repaired(self):
        with tempfile.TemporaryDirectory() as td:
            spm = Path(td) / "SK_tree_hidden_node.spm"
            original = (
                "<SpeedTree><Generators>"
                + _generator("hidden_node_owner", "Branch", 0, 2)
                + "</Generators><Nodes>"
                + _node(
                    "hidden_branch",
                    "Branch",
                    "hidden_node_owner",
                    hidden=True,
                )
                + "</Nodes></SpeedTree>"
            ).encode("utf-8")
            spm.write_bytes(original)

            report = speedtree_cli.ensure_minimum_absolute_branch_bones(spm)

            self.assertEqual(report["status"], "already_compliant")
            self.assertFalse(report["changed"])
            self.assertEqual(spm.read_bytes(), original)
            self.assertFalse((spm.parent / "_spm_backups").exists())

    def test_visible_generator_with_only_deleted_or_culled_nodes_is_not_repaired(self):
        with tempfile.TemporaryDirectory() as td:
            spm = Path(td) / "SK_tree_inactive_nodes.spm"
            original = (
                "<SpeedTree><Generators>"
                + _generator("inactive_owner", "Spline Branch", 0, 2)
                + "</Generators><Nodes>"
                + _node(
                    "deleted_branch",
                    "Branch",
                    "inactive_owner",
                    deleted=True,
                )
                + _node(
                    "culled_branch",
                    "Branch",
                    "inactive_owner",
                    culled=True,
                )
                + "</Nodes></SpeedTree>"
            ).encode("utf-8")
            spm.write_bytes(original)

            report = speedtree_cli.ensure_minimum_absolute_branch_bones(spm)

            self.assertEqual(report["status"], "already_compliant")
            self.assertFalse(report["changed"])
            self.assertEqual(spm.read_bytes(), original)
            self.assertFalse((spm.parent / "_spm_backups").exists())

    def test_visible_branch_under_hidden_generator_ancestor_is_not_repaired(self):
        with tempfile.TemporaryDirectory() as td:
            spm = Path(td) / "SK_tree_hidden_ancestor.spm"
            original = (
                "<SpeedTree><Generators>"
                + _generator("hidden_parent", "Branch", 2, 0, hidden=True)
                + _generator("visible_child", "Branch", 0, 2)
                + "</Generators><Links>"
                + _link("hidden_parent", "visible_child")
                + "</Links><Nodes>"
                + _node("child_branch", "Branch", "visible_child")
                + "</Nodes></SpeedTree>"
            ).encode("utf-8")
            spm.write_bytes(original)

            report = speedtree_cli.ensure_minimum_absolute_branch_bones(spm)

            self.assertEqual(report["status"], "already_compliant")
            self.assertFalse(report["changed"])
            self.assertEqual(spm.read_bytes(), original)
            self.assertFalse((spm.parent / "_spm_backups").exists())

    def test_base_ref_parented_live_branch_is_repaired_exactly(self):
        with tempfile.TemporaryDirectory() as td:
            spm = Path(td) / "SK_tree_base_ref.spm"
            original = (
                "<SpeedTree><Generators>"
                + _generator("base_ref_owner", "Spline Branch", 0, 2)
                + "</Generators><Nodes>"
                + _node("base", "BaseRef", "")
                + _node(
                    "base_ref_branch",
                    "Branch",
                    "base_ref_owner",
                    "base-node-guid",
                )
                + "</Nodes></SpeedTree>"
            ).encode("utf-8")
            spm.write_bytes(original)

            report = speedtree_cli.ensure_minimum_absolute_branch_bones(spm)

            self.assertEqual(report["status"], "updated")
            self.assertEqual(report["changed_generator_count"], 1)
            self.assertEqual(
                report["changed_generators"][0]["guid"],
                "base_ref_owner-guid",
            )
            self.assertEqual(
                report["changed_generators"][0]["runtime_branch_node_count"],
                1,
            )
            values = _generator_values(spm.read_bytes())
            self.assertEqual(values["base_ref_owner"]["Physics:Bone style"], "0")
            self.assertEqual(values["base_ref_owner"]["Physics:Bones"], "1")

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
            self.assertEqual(values["base_ref_zero"]["Physics:Bones"], "1")
            self.assertEqual(values["hidden_zero"]["Physics:Bones"], "0.0")

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
                "<GUID>bad-guid</GUID><Hidden>false</Hidden>"
                + _property("Physics:Bones", 0)
                + "</Generator></Generators><Nodes>"
                + _node("bad", "Branch", "bad")
                + "</Nodes></SpeedTree>"
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
                + "</Generators><Nodes>"
                + _node("invalid", "Branch", "invalid")
                + "</Nodes></SpeedTree>"
            ).encode("utf-8")
            spm.write_bytes(original)

            with self.assertRaisesRegex(RuntimeError, "non-finite"):
                speedtree_cli.ensure_minimum_absolute_branch_bones(spm)

            self.assertEqual(spm.read_bytes(), original)
            self.assertFalse((spm.parent / "_spm_backups").exists())


if __name__ == "__main__":
    unittest.main()
