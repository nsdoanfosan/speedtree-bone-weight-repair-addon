"""BWR export adapter for the generic source evaluator; no asset-name selection."""
from pathlib import Path


def evaluate_export(*, settings, paths, rich_data, wind_document, armature=None):
    del armature  # Bind authority is the validated final rich/native export bundle.
    from io_scene_fbx import parse_fbx
    from . import evaluate_export_loads

    source_spm = settings.get("spm_path") or rich_data.get("asset", {}).get("source_spm")
    if not source_spm or not Path(source_spm).is_file():
        raise ValueError("Current SPM is required for automatic structural wind export")
    asset = rich_data["asset"]
    row = {
        "stem": str(asset.get("asset_id") or paths.get("name_stem") or Path(source_spm).stem),
        "spm": str(source_spm),
        "asset": str(asset.get("expected_unreal_content_path", "")),
        "enabled": bool(wind_document.get("bIsEnabled", True)),
        "rich": str(settings.get("json_output_path") or paths.get("unreal_json", "")),
        "wind": str(settings.get("dynamic_wind_output_path") or paths.get("dynamic_wind_json", "")),
        "assembly": None,
    }
    exported_fbx = asset.get("exported_fbx")
    final_fbx = exported_fbx if exported_fbx and Path(exported_fbx).is_file() else None
    return evaluate_export_loads.evaluate_asset(
        row, audit=None, rich_data=rich_data, wind_data=wind_document,
        fbx_parser=parse_fbx, final_fbx_path=final_fbx)
