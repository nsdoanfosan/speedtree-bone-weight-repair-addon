import ast
from pathlib import Path


CORE_PATH = (
    Path(__file__).resolve().parents[1]
    / "addons"
    / "speedtree_bone_weight_repair"
    / "core.py"
)


def export_function():
    tree = ast.parse(CORE_PATH.read_text(encoding="utf-8"))
    return next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef)
        and node.name == "run_speedtree_cli_export"
    )


def test_boneless_export_is_explicit_opt_in():
    function = export_function()
    defaults = dict(
        zip(
            (argument.arg for argument in function.args.args[-len(function.args.defaults) :]),
            function.args.defaults,
        )
    )
    assert isinstance(defaults["allow_boneless"], ast.Constant)
    assert defaults["allow_boneless"].value is False
    assert isinstance(defaults["allow_manual_bones"], ast.Constant)
    assert defaults["allow_manual_bones"].value is False


def test_boneless_export_still_inspects_generators_for_preset_selection():
    function = export_function()
    calls = {
        node.func.id
        for node in ast.walk(function)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert "inspect_spm_bone_generators" in calls
    assert "require_spm_sk_ready" in calls


def test_cluster_source_export_is_deferred_only_for_normalizer_producer():
    tree = ast.parse(CORE_PATH.read_text(encoding="utf-8"))
    function = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef)
        and node.name == "run_merge_export"
    )
    strings = {
        node.value
        for node in ast.walk(function)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
    }
    calls = {
        node.func.id
        for node in ast.walk(function)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
    }
    assert "cluster_source_skin_contract" in strings
    assert "defer_cluster_export_to_normalizer" in strings
    assert "park_cluster_source_full_reference" in calls
    assert "structure_export_unit" in calls
