"""No server or production files are mutated by recovery checks."""
import hashlib
import importlib.util
import marshal
import os
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1] / 'addons/speedtree_bone_weight_repair'
package = types.ModuleType('recovery_test_package')
package.__path__ = [str(ROOT)]
sys.modules[package.__name__] = package
import importlib
recovery = importlib.import_module(package.__name__ + '.perforce_recovery')
cli = importlib.import_module(package.__name__ + '.speedtree_cli')


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / 'source.spm'
        self.original = b'original binary SPM\x00\xff'
        self.path.write_bytes(self.original)
        self.calls = []
        self.depot = '//depot/source.spm'
        self.server = self.original
        def command(client, *args):
            self.calls.append((client, args))
            if args[0] == 'print': return self.server
            if args[1] == 'where':
                return marshal.dumps({b'code': b'stat', b'depotFile': self.depot.encode(),
                                      b'path': str(self.path).encode()})
            return marshal.dumps({b'code': b'stat', b'haveRev': b'3', b'headType': b'binary+l'})
        self.mock = patch.object(recovery, '_p4', side_effect=command)
        self.mock.start(); self.addCleanup(self.mock.stop)
        env = patch.dict(os.environ, {'SPEEDTREE_PERFORCE_CLIENT': 'ArtSources', 'SPEEDTREE_PERFORCE_SHELF': ''})
        env.start(); self.addCleanup(env.stop)

    def test_submitted_bytes_verified_without_local_backup(self):
        self.assertEqual(recovery.verified_recovery(self.path), 'p4://ArtSources//depot/source.spm#3')
        self.assertEqual(list(self.path.parent.iterdir()), [self.path])
        self.assertTrue(all(c == 'ArtSources' for c, _ in self.calls))

    def test_shelf_bytes_verified(self):
        with patch.dict(os.environ, {'SPEEDTREE_PERFORCE_SHELF': '1072'}):
            self.assertTrue(recovery.verified_recovery(self.path).endswith('@=1072'))

    def test_pending_add_or_empty_shelf_is_not_recovery(self):
        self.server = b''
        with patch.dict(os.environ, {'SPEEDTREE_PERFORCE_SHELF': '1072'}):
            with self.assertRaisesRegex(RuntimeError, 'bytes do not match'):
                recovery.verified_recovery(self.path)
        self.assertEqual(self.path.read_bytes(), self.original)

    def test_modified_workspace_requires_matching_shelf(self):
        self.path.write_bytes(b'unsubmitted source')
        with self.assertRaisesRegex(RuntimeError, 'bytes do not match'):
            recovery.verified_recovery(self.path)

    def test_reference_cannot_borrow_other_file(self):
        with self.assertRaisesRegex(RuntimeError, 'different source'):
            recovery.verify_reference(self.path, 'p4://ArtSources//depot/other.spm#3', hashlib.sha256(self.original).hexdigest())

    def test_receipt_revalidates_original_server_bytes_after_mutation(self):
        reference = recovery.verified_recovery(self.path)
        self.path.write_bytes(b'updated source')
        receipt = {'status': 'updated', 'spm': str(self.path), 'changed': True,
                   'changed_generator_count': 1, 'backup': reference,
                   'source_sha256': hashlib.sha256(self.original).hexdigest(),
                   'updated_sha256': hashlib.sha256(self.path.read_bytes()).hexdigest()}
        cli._validated_minimum_bone_policy_receipt(self.path, receipt)
        self.server = b'tampered recovery'
        with self.assertRaisesRegex(RuntimeError, 'bytes do not match'):
            cli._validated_minimum_bone_policy_receipt(self.path, receipt)

    def test_invalid_shelf_rejected(self):
        with patch.dict(os.environ, {'SPEEDTREE_PERFORCE_SHELF': '-1'}):
            with self.assertRaisesRegex(RuntimeError, 'positive changelist'):
                recovery.verified_recovery(self.path)

    def test_korean_server_paths_are_decoded_without_loss(self):
        path = self.path.parent / '식생.spm'
        payload = marshal.dumps({b'code': b'stat', b'depotFile': '//depot/식생.spm'.encode('cp949'),
                                 b'path': str(path).encode('cp949')})
        with patch.object(recovery, '_p4', return_value=payload):
            self.assertEqual(recovery._mapping(path, 'ArtSources'), '//depot/식생.spm')

if __name__ == '__main__': unittest.main()
