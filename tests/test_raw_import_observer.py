"""Raw FBX observers run before add-on mutation and fail closed."""
import ast
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CORE_PATH = ROOT / "addons" / "speedtree_bone_weight_repair" / "core.py"


def core_tree():
    return ast.parse(CORE_PATH.read_text(encoding="utf-8"))


def load_core_function(name, namespace):
    function = next(
        node
        for node in core_tree().body
        if isinstance(node, ast.FunctionDef) and node.name == name
    )
    module = ast.Module(body=[function], type_ignores=[])
    values = dict(namespace)
    exec(compile(module, str(CORE_PATH), "exec"), values)
    return values[name]


def call_line(function, name):
    return next(
        node.lineno
        for node in ast.walk(function)
        if (
            isinstance(node, ast.Call)
            and (
                isinstance(node.func, ast.Name)
                and node.func.id == name
            )
        )
    )


class RawImportObserverTests(unittest.TestCase):
    def test_observer_receives_same_objects_and_resolved_path(self):
        observed = []
        imported = [object(), object()]
        rollback = []
        helper = load_core_function(
            "observe_raw_import_before_mutation",
            {
                "Path": Path,
                "rollback_raw_import_observer": (
                    lambda *args: rollback.append(args)
                ),
            },
        )

        result = helper(
            lambda objects, path: observed.append((objects, path)) or "ok",
            imported,
            Path("C:/exact/./tree.fbx"),
            {"snapshot": True},
        )

        self.assertEqual(result, "ok")
        self.assertIs(observed[0][0], imported)
        self.assertEqual(
            observed[0][1],
            Path("C:/exact/tree.fbx").resolve(),
        )
        self.assertEqual(rollback, [])

    def test_observer_exception_rolls_back_and_is_re_raised(self):
        rollback = []
        imported = [object()]
        snapshot = {"meshes": {1}}
        helper = load_core_function(
            "observe_raw_import_before_mutation",
            {
                "Path": Path,
                "rollback_raw_import_observer": (
                    lambda *args: rollback.append(args)
                ),
            },
        )

        def reject(_objects, _path):
            raise RuntimeError("contract rejected raw import")

        with self.assertRaisesRegex(RuntimeError, "contract rejected"):
            helper(reject, imported, Path("C:/exact/tree.fbx"), snapshot)

        self.assertEqual(rollback, [(imported, snapshot)])

    def test_none_observer_is_a_noop(self):
        helper = load_core_function(
            "observe_raw_import_before_mutation",
            {
                "Path": Path,
                "rollback_raw_import_observer": self.fail,
            },
        )

        self.assertIsNone(helper(None, [], Path("C:/tree.fbx"), None))

    def test_observer_precedes_every_addon_source_mutation(self):
        function = next(
            node
            for node in core_tree().body
            if (
                isinstance(node, ast.FunctionDef)
                and node.name == "run_import_source_fbx"
            )
        )
        source_tag_line = next(
            node.lineno
            for node in ast.walk(function)
            if (
                isinstance(node, ast.Assign)
                and isinstance(node.targets[0], ast.Subscript)
                and isinstance(node.targets[0].slice, ast.Constant)
                and node.targets[0].slice.value == "codex_source_fbx"
            )
        )
        observer_line = call_line(
            function,
            "observe_raw_import_before_mutation",
        )
        mutation_lines = [
            call_line(function, name)
            for name in (
                "tag_native_export_geometry",
                "ensure_scene_collection",
                "tag_speedtree_import_materials",
                "discard_unassigned_geometry_before_assembly",
                "apply_object_scales",
            )
        ]

        self.assertLess(source_tag_line, observer_line)
        self.assertTrue(all(observer_line < line for line in mutation_lines))

    def test_observer_path_preserves_inspection_import_completion_gate(self):
        function = next(
            node
            for node in core_tree().body
            if (
                isinstance(node, ast.FunctionDef)
                and node.name == "run_import_source_fbx"
            )
        )
        source = ast.get_source_segment(
            CORE_PATH.read_text(encoding="utf-8"),
            function,
        )

        self.assertIn('"FINISHED" not in import_operator_result', source)
        self.assertLess(
            source.index("Raw FBX observer import returned"),
            source.index('obj["codex_source_fbx"]'),
        )

    def test_assembly_entrypoint_forwards_observer_to_import(self):
        function = next(
            node
            for node in core_tree().body
            if (
                isinstance(node, ast.FunctionDef)
                and node.name == "run_import_and_assemble"
            )
        )
        import_call = next(
            node
            for node in ast.walk(function)
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "run_import_source_fbx"
            )
        )
        keyword = next(
            item.value
            for item in import_call.keywords
            if item.arg == "raw_import_observer"
        )

        self.assertIsInstance(keyword, ast.Name)
        self.assertEqual(keyword.id, "raw_import_observer")
        pipeline_line = call_line(function, "run_assembly_pipeline")
        self.assertLess(import_call.lineno, pipeline_line)


if __name__ == "__main__":
    unittest.main()
