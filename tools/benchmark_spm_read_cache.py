"""Benchmark the process-local SPM snapshot from Blender background mode.

Usage:
  blender -b --factory-startup --python tools/benchmark_spm_read_cache.py -- tree.spm
"""

import json
import sys
from pathlib import Path
from time import perf_counter


ADDONS_ROOT = Path(__file__).resolve().parents[1] / "addons"
if str(ADDONS_ROOT) not in sys.path:
    sys.path.insert(0, str(ADDONS_ROOT))

from speedtree_bone_weight_repair import core, spm_reader  # noqa: E402


def timed(function, *args):
    started = perf_counter()
    function(*args)
    return round(perf_counter() - started, 6)


def benchmark(path):
    spm_reader.clear_cache()
    return {
        "spm": str(Path(path).resolve()),
        "first_bone_inspection_seconds": timed(
            core.inspect_spm_bone_generators, path
        ),
        "cached_bone_inspection_seconds": timed(
            core.inspect_spm_bone_generators, path
        ),
        "first_profile_inspection_seconds": timed(
            core.inspect_spm_unreal_instance_profile, path
        ),
        "cached_profile_inspection_seconds": timed(
            core.inspect_spm_unreal_instance_profile, path
        ),
        "first_semantic_parse_seconds": timed(core.parse_speedtree, path),
        "cached_semantic_parse_seconds": timed(core.parse_speedtree, path),
        "cache": spm_reader.cache_info(),
    }


arguments = sys.argv[sys.argv.index("--") + 1 :] if "--" in sys.argv else []
if not arguments:
    raise SystemExit("Pass at least one SPM path after --")
print("SPM_CACHE_BENCHMARK=" + json.dumps([benchmark(path) for path in arguments]))
