import sys
import tempfile
from pathlib import Path


ADDON_ROOT = Path(__file__).resolve().parents[1] / "addons"
if str(ADDON_ROOT) not in sys.path:
    sys.path.insert(0, str(ADDON_ROOT))

from speedtree_bone_weight_repair import core  # noqa: E402


def main():
    with tempfile.TemporaryDirectory() as temporary:
        xml_path = Path(temporary) / "localized.xml"
        xml_path.write_text(
            '<SpeedTreeRaw><Bones><Bone ID="0" ParentID="-1" '
            'Radius="0,3048" StartX="21,0313" StartY="0.5" StartZ="-2,70936" '
            'EndX="21,2172" EndY="2,49074" EndZ="1,36796" '
            'Mass="0,0001" Generator="Trunk 10" /></Bones></SpeedTreeRaw>',
            encoding="utf-8",
        )
        bones = core.parse_speedtree_xml_bones(xml_path)

    assert len(bones) == 1
    assert bones[0]["radius"] == 0.3048
    assert bones[0]["start"] == (21.0313, 0.5, -2.70936)
    assert bones[0]["end"] == (21.2172, 2.49074, 1.36796)
    assert bones[0]["mass"] == 0.0001
    print("SpeedTree localized XML wind parser smoke: PASS")


if __name__ == "__main__":
    main()
