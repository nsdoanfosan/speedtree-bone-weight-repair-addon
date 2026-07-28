import importlib.util
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock


MODULE_PATH = (
    Path(__file__).resolve().parents[1]
    / "addons"
    / "speedtree_bone_weight_repair"
    / "speedtree_cli.py"
)
SPEC = importlib.util.spec_from_file_location("bwr_speedtree_cli_test", MODULE_PATH)
speedtree_cli = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(speedtree_cli)


class FakeProcess:
    next_pid = 41000

    def __init__(self, command, stdout, stderr, returncode=0, timeout=False):
        self.command = command
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode
        self.timeout = timeout
        self.pid = FakeProcess.next_pid
        FakeProcess.next_pid += 1
        self.killed = False

    def wait(self, timeout=None):
        if self.timeout:
            raise subprocess.TimeoutExpired(self.command, timeout)
        return self.returncode

    def kill(self):
        self.killed = True


def make_inputs(root):
    root = Path(root)
    exe = root / "SpeedTree_Modeler.exe"
    spm = root / "SK_test.spm"
    options = root / "Options.ini"
    exe.write_bytes(b"fake-exe")
    spm.write_bytes(b"spm-v1")
    options.write_bytes(b"options-v1")
    return exe, spm, options


def write_staged_fbx(command, content=b"fbx-v1"):
    target = Path(command[-1])
    target.write_bytes(content)
    target.with_suffix(".stmat").write_text("<Materials />", encoding="utf-8")
    (target.parent / "M_leaf_Color.png").write_bytes(b"texture-v1")


class SpeedTreeCliTests(unittest.TestCase):
    def test_export_gate_is_acquired_before_process_timeout_starts(self):
        events = []

        class Gate:
            def __enter__(self):
                events.append("gate-enter")

            def __exit__(self, *_args):
                events.append("gate-exit")

        class Process:
            pid = 40001

            def wait(self, timeout=None):
                events.append(("wait", timeout))
                return 0

        def popen(_command, **_kwargs):
            events.append("popen")
            return Process()

        with mock.patch.object(
            speedtree_cli, "speedtree_export_gate", return_value=Gate()
        ), mock.patch.object(
            speedtree_cli.subprocess, "Popen", side_effect=popen
        ):
            result = speedtree_cli._run_process(
                ["SpeedTree.exe"], ".", timeout_seconds=17
            )

        self.assertEqual(result, (0, "", ""))
        self.assertEqual(
            events,
            ["gate-enter", "popen", ("wait", 17), "gate-exit"],
        )

    def test_mutex_name_matches_sk_batch_contract(self):
        self.assertEqual(
            speedtree_cli.SPEEDTREE_EXPORT_MUTEX_DEFAULT,
            r"Local\PARK.SpeedTree.Modeler.Export.v1.slot0",
        )

    def test_bundle_mtime_sync_updates_verified_artifact_and_cache_receipt(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = root / "xml" / "SK_test.xml"
            target.parent.mkdir(parents=True)
            target.write_text("<SpeedTreeRaw />", encoding="utf-8")
            cache_path = speedtree_cli._cache_path(target)
            cache = {
                "version": speedtree_cli.EXPORT_CACHE_VERSION,
                "artifacts": [
                    speedtree_cli._artifact_record(target, target.parent)
                ],
            }
            speedtree_cli._write_cache(cache_path, cache)
            minimum = target.stat().st_mtime_ns + 1_000_000_000
            result = {
                "path": str(target),
                "cache_path": str(cache_path),
                "artifacts": cache["artifacts"],
            }

            sync = speedtree_cli.synchronize_result_mtime(result, minimum)

            self.assertTrue(sync["changed"])
            self.assertEqual(target.stat().st_mtime_ns, minimum)
            self.assertTrue(result["bundle_mtime_synchronized"])
            refreshed = speedtree_cli._load_cache(cache_path)
            self.assertEqual(refreshed["artifacts"][0]["mtime_ns"], minimum)

    def test_regular_file_logs_and_fingerprint_cache_skip_second_export(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            exe, spm, options = make_inputs(root)
            target = root / "out" / "SK_test.fbx"
            calls = []

            def popen(command, **kwargs):
                calls.append((command, kwargs))
                self.assertIsNot(kwargs["stdout"], subprocess.PIPE)
                self.assertIsNot(kwargs["stderr"], subprocess.PIPE)
                write_staged_fbx(command)
                return FakeProcess(command, kwargs["stdout"], kwargs["stderr"])

            with mock.patch.object(speedtree_cli.subprocess, "Popen", side_effect=popen):
                first = speedtree_cli.export_target(
                    exe, spm, options, "fbx", target, timeout_seconds=10
                )
                second = speedtree_cli.export_target(
                    exe, spm, options, "fbx", target, timeout_seconds=10
                )

            self.assertFalse(first["cache_hit"])
            self.assertTrue(second["cache_hit"])
            self.assertEqual(len(calls), 1)
            self.assertEqual(target.read_bytes(), b"fbx-v1")
            self.assertTrue(target.with_suffix(".stmat").is_file())
            self.assertTrue(Path(second["cache_path"]).is_file())

    def test_changed_spm_invalidates_cache(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            exe, spm, options = make_inputs(root)
            target = root / "out" / "SK_test.fbx"
            call_count = 0

            def popen(command, **kwargs):
                nonlocal call_count
                call_count += 1
                write_staged_fbx(command, f"fbx-v{call_count}".encode("ascii"))
                return FakeProcess(command, kwargs["stdout"], kwargs["stderr"])

            with mock.patch.object(speedtree_cli.subprocess, "Popen", side_effect=popen):
                speedtree_cli.export_target(exe, spm, options, "fbx", target)
                spm.write_bytes(b"spm-v2")
                result = speedtree_cli.export_target(exe, spm, options, "fbx", target)

            self.assertEqual(call_count, 2)
            self.assertFalse(result["cache_hit"])
            self.assertEqual(target.read_bytes(), b"fbx-v2")

    def test_first_run_seeds_fresh_valid_existing_export(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            exe, spm, options = make_inputs(root)
            target = root / "out" / "SK_test.fbx"
            target.parent.mkdir(parents=True)
            target.write_bytes(b"existing-fbx")
            target.with_suffix(".stmat").write_text(
                "<Materials />", encoding="utf-8"
            )

            with mock.patch.object(speedtree_cli.subprocess, "Popen") as popen:
                result = speedtree_cli.export_target(
                    exe, spm, options, "fbx", target, timeout_seconds=10
                )

            popen.assert_not_called()
            self.assertTrue(result["cache_hit"])
            self.assertTrue(result["cache_seeded"])
            self.assertTrue(Path(result["cache_path"]).is_file())
            self.assertEqual(target.read_bytes(), b"existing-fbx")

    def test_failed_export_preserves_existing_output_and_manual_sibling(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            exe, spm, options = make_inputs(root)
            target = root / "out" / "SK_test.fbx"
            target.parent.mkdir(parents=True)
            target.write_bytes(b"manual-fbx")
            target.with_suffix(".stmat").write_text(
                "<Materials Manual='1' />", encoding="utf-8"
            )
            manual = target.parent / "manual_notes.txt"
            manual.write_text("keep", encoding="utf-8")
            # Make the authored source newer so this test exercises an actual
            # failed export rather than the one-time legacy cache seed.
            spm.write_bytes(b"spm-v2-needs-export")
            future_ns = target.stat().st_mtime_ns + 1_000_000_000
            os.utime(spm, ns=(future_ns, future_ns))

            def popen(command, **kwargs):
                write_staged_fbx(command, b"failed-new-fbx")
                return FakeProcess(
                    command, kwargs["stdout"], kwargs["stderr"], returncode=7
                )

            with mock.patch.object(speedtree_cli.subprocess, "Popen", side_effect=popen):
                with self.assertRaisesRegex(RuntimeError, "failed with code 7"):
                    speedtree_cli.export_target(exe, spm, options, "fbx", target)

            self.assertEqual(target.read_bytes(), b"manual-fbx")
            self.assertIn("Manual='1'", target.with_suffix(".stmat").read_text())
            self.assertEqual(manual.read_text(encoding="utf-8"), "keep")
            self.assertFalse((target.parent / "M_leaf_Color.png").exists())

    def test_timeout_invokes_process_tree_cleanup(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            exe, spm, options = make_inputs(root)
            target = root / "out" / "SK_test.xml"
            processes = []

            def popen(command, **kwargs):
                process = FakeProcess(
                    command, kwargs["stdout"], kwargs["stderr"], timeout=True
                )
                processes.append(process)
                return process

            with mock.patch.object(speedtree_cli.subprocess, "Popen", side_effect=popen), mock.patch.object(
                speedtree_cli, "_terminate_process_tree"
            ) as terminate:
                with self.assertRaisesRegex(RuntimeError, "process tree was terminated"):
                    speedtree_cli.export_target(
                        exe, spm, options, "xml", target, timeout_seconds=0.01
                    )

            terminate.assert_called_once_with(processes[0])
            self.assertFalse(target.exists())

    def test_promotion_failure_restores_all_prior_files(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            staging = root / "staging"
            destination = root / "out"
            staging.mkdir()
            destination.mkdir()
            (staging / "a.txt").write_text("new-a", encoding="utf-8")
            (staging / "b.txt").write_text("new-b", encoding="utf-8")
            (destination / "a.txt").write_text("old-a", encoding="utf-8")
            (destination / "b.txt").write_text("old-b", encoding="utf-8")

            real_replace = speedtree_cli.os.replace
            destination_promotions = 0

            def flaky_replace(source, target):
                nonlocal destination_promotions
                source = Path(source)
                target = Path(target)
                if ".bwr-new-" in source.name and target.parent == destination:
                    destination_promotions += 1
                    if destination_promotions == 2:
                        raise OSError("simulated promotion failure")
                return real_replace(source, target)

            with mock.patch.object(speedtree_cli.os, "replace", side_effect=flaky_replace):
                with self.assertRaisesRegex(RuntimeError, "promotion failed"):
                    speedtree_cli._transactional_promote(staging, destination)

            self.assertEqual((destination / "a.txt").read_text(), "old-a")
            self.assertEqual((destination / "b.txt").read_text(), "old-b")


if __name__ == "__main__":
    unittest.main()
