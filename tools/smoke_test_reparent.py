import argparse
import json
import sys
from pathlib import Path

import addon_utils
import bpy


def parse_args():
    argv = sys.argv
    if "--" in argv:
        argv = argv[argv.index("--") + 1 :]
    else:
        argv = []
    parser = argparse.ArgumentParser()
    parser.add_argument("--blend", required=True)
    parser.add_argument("--spm", required=True)
    parser.add_argument("--armature", default="Root")
    parser.add_argument("--true-root", default="Bone_1_Start")
    parser.add_argument("--scale", default="auto")
    parser.add_argument("--tolerance", type=float, default=0.08)
    parser.add_argument("--report", required=True)
    return parser.parse_args(argv)


def main():
    args = parse_args()
    bpy.ops.wm.open_mainfile(filepath=args.blend)
    addon_utils.enable("speedtree_bone_weight_repair", default_set=False)
    from speedtree_bone_weight_repair import core

    Path(args.report).parent.mkdir(parents=True, exist_ok=True)
    result = core.run_reparent_from_spm(
        args.spm,
        args.armature,
        args.true_root,
        args.scale,
        args.tolerance,
        apply=False,
        strict=False,
        report_path=args.report,
    )
    print(
        "DRY_REPARENT "
        + json.dumps(
            {
                "status": result.get("status"),
                "mapping_count": result.get("mapping_count"),
                "orphan_start_roots": result.get("orphan_start_roots"),
                "unresolved": len(result.get("unresolved", [])),
                "report": args.report,
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
