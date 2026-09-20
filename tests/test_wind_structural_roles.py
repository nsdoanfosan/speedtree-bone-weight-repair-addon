import importlib.util
import tempfile
import unittest
from pathlib import Path

SOURCE = Path(__file__).resolve().parents[1] / 'addons/speedtree_bone_weight_repair/wind_structural_roles.py'
spec = importlib.util.spec_from_file_location('wind_structural_roles', SOURCE)
roles = importlib.util.module_from_spec(spec)
spec.loader.exec_module(roles)


class ScanTrunkRolesTest(unittest.TestCase):
    def test_renamed_scan_stem_is_trunk_but_side_branch_is_not(self):
        generators = [('t', 'Oak', 'Tree'), ('m', 'Scanned wood', 'BranchMesh'),
                      ('s', 'Seam', 'Stitch'), ('b', 'Leader renamed', 'Branch'),
                      ('l', 'Trunk misleading name', 'Branch')]
        xml = '<SpeedTree><Generators>' + ''.join(
            f'<Generator Type="{typ}"><GUID>{gid}</GUID><Name>{name}</Name></Generator>'
            for gid, name, typ in generators) + '</Generators><Links>' + ''.join(
            f'<Link><SourceGUID>{a}</SourceGUID><TargetGUID>{b}</TargetGUID></Link>'
            for a, b in [('t', 'm'), ('m', 's'), ('s', 'b'), ('b', 'l')]) + '</Links></SpeedTree>'
        bones = [{'name': 'Root', 'bone_index': 0, 'parent_index': -1, 'group': 0,
                  'native_role': 'fbx_wrapper_root'},
                 {'name': 'Stem', 'bone_index': 1, 'parent_index': 0, 'group': 1, 'generator': 'Leader renamed'},
                 {'name': 'Side', 'bone_index': 2, 'parent_index': 1, 'group': 0, 'generator': 'Trunk misleading name'}]
        info = {'simulation_groups': [
            {'index': 0, 'generators': ['Trunk misleading name'], 'is_trunk_group': False},
            {'index': 1, 'generators': ['Leader renamed'], 'is_trunk_group': False}]}
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'source.spm'
            path.write_text(xml, encoding='utf-8')
            result, metadata = roles.apply_scan_trunk_roles(bones, info, path)
            self.assertEqual(result[0]['group'], 2)
            self.assertEqual(result[1]['group'], 1)
            self.assertEqual(result[2]['group'], 0)
            self.assertEqual([g['is_trunk_group'] for g in metadata['simulation_groups']], [False, True, True])
            repeated, repeated_metadata = roles.apply_scan_trunk_roles(result, metadata, path)
            self.assertEqual((repeated, repeated_metadata), (result, metadata))
            self.assertEqual(bones[0]['group'], 0)  # input remains immutable
            self.assertFalse(info['simulation_groups'][1]['is_trunk_group'])
            bad = [dict(b) for b in bones]
            bad[1]['parent_index'] = 2
            with self.assertRaisesRegex(RuntimeError, 'not attached'):
                roles.apply_scan_trunk_roles(bad, info, path)
            path.write_text(xml.replace('Type="Stitch"', 'Type="Branch"'), encoding='utf-8')
            self.assertEqual(roles.apply_scan_trunk_roles(bones, info, path), (bones, info))


if __name__ == '__main__':
    unittest.main()
