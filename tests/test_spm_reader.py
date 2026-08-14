import gzip
import importlib.util
import os
from pathlib import Path


MODULE_PATH = (
    Path(__file__).resolve().parents[1]
    / "addons"
    / "speedtree_bone_weight_repair"
    / "spm_reader.py"
)
SPEC = importlib.util.spec_from_file_location("bwr_spm_reader_test", MODULE_PATH)
spm_reader = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(spm_reader)


def write_spm(path, version, padding=""):
    xml = f'<SpeedTree VersionString="{version}"><Padding>{padding}</Padding></SpeedTree>'
    path.write_bytes(gzip.compress(xml.encode("utf-8")))
    os.utime(path, None)


def test_unchanged_spm_is_read_decompressed_and_parsed_once(tmp_path):
    spm = tmp_path / "tree.spm"
    write_spm(spm, "10.1")
    spm_reader.clear_cache()

    first = spm_reader.read_spm_root(spm)
    second = spm_reader.read_spm_root(spm)

    assert first is second
    assert first.attrib["VersionString"] == "10.1"
    info = spm_reader.cache_info()
    assert info["file_reads"] == 1
    assert info["gzip_decompressions"] == 1
    assert info["xml_parses"] == 1
    assert info["cache_hits"] >= 1


def test_derived_view_is_built_once_per_unchanged_spm(tmp_path):
    spm = tmp_path / "tree.spm"
    write_spm(spm, "10.1")
    spm_reader.clear_cache()
    builds = []

    def build(root):
        builds.append(root.attrib["VersionString"])
        return {"version": root.attrib["VersionString"]}

    first = spm_reader.get_derived(spm, "semantic-v1", build)
    second = spm_reader.get_derived(spm, "semantic-v1", build)

    assert first is second
    assert builds == ["10.1"]
    assert spm_reader.cache_info()["derived_builds"] == 1


def test_file_change_invalidates_all_snapshots(tmp_path):
    spm = tmp_path / "tree.spm"
    write_spm(spm, "10.1")
    spm_reader.clear_cache()
    assert spm_reader.read_spm_root(spm).attrib["VersionString"] == "10.1"

    write_spm(spm, "10.2", padding="changed-size")
    assert spm_reader.read_spm_root(spm).attrib["VersionString"] == "10.2"

    info = spm_reader.cache_info()
    assert info["file_reads"] == 2
    assert info["xml_parses"] == 2
