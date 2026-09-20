import gzip
from pathlib import Path
import importlib.util
import pytest

SOURCE=Path(__file__).resolve().parents[1]/'addons/speedtree_bone_weight_repair/wind_structural_roles.py'
spec=importlib.util.spec_from_file_location('roles_sections',SOURCE)
roles=importlib.util.module_from_spec(spec);spec.loader.exec_module(roles)

@pytest.mark.parametrize('compressed',[False,True])
def test_nested_force_generator_element_is_not_tree_graph(tmp_path,compressed):
    payload=b'<SpeedTree><Forces><Force><Extra><Generators /></Extra></Force></Forces><Generators><Generator Type="Tree"><GUID>root</GUID></Generator></Generators><Links /></SpeedTree>'
    p=tmp_path/'source.spm';p.write_bytes(gzip.compress(payload) if compressed else payload)
    bones,info=[],{}
    assert roles.apply_scan_trunk_roles(bones,info,p)==(bones,info)

def test_duplicate_top_level_graph_is_rejected(tmp_path):
    p=tmp_path/'source.spm';p.write_text('<SpeedTree><Generators/><Generators/><Links/></SpeedTree>')
    with pytest.raises(RuntimeError,match='expected one Generators'):
        roles.apply_scan_trunk_roles([],{},p)
