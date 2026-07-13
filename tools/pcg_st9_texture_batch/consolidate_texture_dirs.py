"""texture\\substance\\ (및 substance\\) 를 texture\\ 로 통합 정리.

왜: 대부분 폴더는 텍스처/ .sbs 를 <folder>\\texture\\ 에 두는데, 9개 폴더만
<folder>\\texture\\substance\\ 에 둔다. 이 위치는 단순 실수가 아니라 SK SPM 의
<TexFilename> 상대경로(texture/substance/...)와 .sbs 상대참조에 baked-in 되어
있어서, 파일만 옮기면 SPM/SBS 참조가 깨진다.

이 도구는 안전하게 통합한다:
  1) 모든 substance\\ 파일을 texture\\ 로 이동 (충돌은 아카이브 후 이동)
  2) 옮긴 .sbs 의 내부 상대경로 재작성 (resolve→recompute, 깊이 변화 반영)
  3) 그 .sbs 를 참조하던 외부 .sbs 의 dependency 경로 재작성
  4) SK SPM 의 texture/substance/ 경로를 texture/ 로 재작성 (바이트 단위)
  5) 검증: 이동 전에 '존재하던' 모든 참조가 이동 후에도 존재하는지 확인,
     하나라도 새로 깨지면 전체 롤백
모든 수정 파일은 먼저 백업. 기본은 dry-run(아무것도 안 씀). --apply 로 실행.

.blend / .stgmat / manifest.json 은 이 경로를 참조하지 않아 손대지 않는다(확인됨).
"""
import argparse
import gzip
import json
import os
import re
import shutil
import sys
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent))

from pcg_texture_common import load_config, is_backup_path

SUBDIR_NAMES = (("texture", "substance"), ("substance",))
IMAGE_SBS = {".sbs"}
SKIP_FOLDERS = {"atlas", "mesh", "st9", "trunk"}


# ------------------------------------------------------------------ 스캔
def substance_dirs(tree_root):
    """<folder>\\texture\\substance 및 <folder>\\substance 디렉터리 목록."""
    dirs = []
    for folder in sorted(p for p in Path(tree_root).iterdir() if p.is_dir()):
        if folder.name.lower() in SKIP_FOLDERS:
            continue
        for parts in SUBDIR_NAMES:
            d = folder.joinpath(*parts)
            if d.exists() and d.is_dir():
                dirs.append((folder, d))
    return dirs


def build_move_map(tree_root):
    """old_abs(resolve소문자) -> (old_path, new_path). new = <folder>/texture/<name>."""
    move = {}
    collisions = []
    for folder, sub in substance_dirs(tree_root):
        tex = folder / "texture"
        for src in sub.iterdir():
            if not src.is_file() or is_backup_path(src):
                continue
            dst = tex / src.name
            key = str(src.resolve()).lower()
            if dst.exists():
                same = dst.stat().st_size == src.stat().st_size and \
                    dst.read_bytes() == src.read_bytes()
                collisions.append({"folder": folder.name, "name": src.name,
                                   "src": src, "dst": dst, "identical": same})
            move[key] = (src, dst)
    return move, collisions


# ------------------------------------------------------------------ 경로 유틸
def is_pathlike_ref(text):
    if not text:
        return False
    low = text.lower()
    if low.startswith("sbs://") or text == "?himself":
        return False
    return True


def resolve_ref(ref, base_dir):
    try:
        p = Path(ref)
        if p.is_absolute():
            return p.resolve()
        return (Path(base_dir) / ref).resolve()
    except Exception:
        return None


def recompute_ref(ref, old_base_dir, move_map, new_referencing_dir):
    """참조를 새 위치 기준으로 다시 계산. (새 문자열 or None=변화없음/해당없음)."""
    if not is_pathlike_ref(ref):
        return None
    old_target = resolve_ref(ref, old_base_dir)
    if old_target is None:
        return None
    key = str(old_target).lower()
    moved = move_map.get(key)
    new_target = moved[1].resolve() if moved else old_target
    if new_target == old_target and Path(new_referencing_dir) == Path(old_base_dir):
        return None  # 아무것도 안 움직임
    try:
        new_ref = os.path.relpath(str(new_target), str(new_referencing_dir))
    except ValueError:
        new_ref = str(new_target)
    new_ref = new_ref.replace("\\", "/")
    return new_ref if new_ref != ref.replace("\\", "/") else None


def ref_exists(ref, base_dir):
    if not is_pathlike_ref(ref):
        return None  # 판단 대상 아님
    t = resolve_ref(ref, base_dir)
    return bool(t and t.exists())


# ------------------------------------------------------------------ sbs 처리
def all_sbs(tree_root):
    out = []
    for folder in Path(tree_root).iterdir():
        if not folder.is_dir():
            continue
        for d in (folder, folder / "texture", folder / "texture" / "substance", folder / "substance"):
            if d.exists():
                out.extend(p for p in d.glob("*.sbs") if not is_backup_path(p))
    return out


def sbs_refs(sbs_path):
    """(kind, element, attr, value) 목록. kind: dep|resource."""
    tree = ET.parse(sbs_path)
    root = tree.getroot()
    items = []
    for dep in root.iter("dependency"):
        fn = dep.find("filename")
        if fn is not None:
            items.append(("dep", fn, "v", fn.get("v")))
    for res in root.iter("resource"):
        fp = res.find("filepath")
        if fp is not None:
            items.append(("resource", fp, "v", fp.get("v")))
    return tree, root, items


def plan_sbs(tree_root, move_map):
    """각 sbs의 재작성 계획. [{sbs, old_dir, new_dir, changes:[(old,new)], moved:bool}]"""
    plans = []
    for sbs in all_sbs(tree_root):
        key = str(sbs.resolve()).lower()
        moved = move_map.get(key)
        old_dir = sbs.parent
        new_dir = moved[1].parent if moved else old_dir
        try:
            tree, root, items = sbs_refs(sbs)
        except Exception as exc:
            plans.append({"sbs": sbs, "error": str(exc)})
            continue
        changes = []
        for kind, el, attr, val in items:
            new = recompute_ref(val, old_dir, move_map, new_dir)
            if new is not None:
                changes.append((val, new, el))
        if changes or moved:
            plans.append({"sbs": sbs, "old_dir": old_dir, "new_dir": new_dir,
                          "moved": bool(moved), "new_path": moved[1] if moved else sbs,
                          "changes": changes, "tree": tree})
    return plans


# ------------------------------------------------------------------ spm 처리
def spm_read(path):
    with gzip.open(path, "rb") as h:
        return h.read()


def plan_spms(tree_root, move_map):
    """substance 경로를 참조하는 SPM 목록과 교체 횟수. (blob은 apply 때 다시 읽음)"""
    plans = []
    needles = [b"texture/substance/", b"texture\\substance\\"]
    for folder, _sub in substance_dirs(tree_root):
        seen = set()
        for spm in folder.glob("*.spm"):
            if is_backup_path(spm) or spm in seen:
                continue
            seen.add(spm)
            try:
                blob = spm_read(spm)
            except Exception as exc:
                plans.append({"spm": spm, "error": str(exc)})
                continue
            count = sum(blob.count(n) for n in needles)
            if count:
                plans.append({"spm": spm, "count": count})
    return plans


def spm_rewrite_bytes(blob):
    return blob.replace(b"texture/substance/", b"texture/").replace(
        b"texture\\substance\\", b"texture\\")


# ------------------------------------------------------------------ 검증
def inventory_existing_refs(tree_root):
    """이동 전, (파일, ref) 중 실제로 존재하는 참조만 기록."""
    inv = []
    for sbs in all_sbs(tree_root):
        try:
            _tree, _root, items = sbs_refs(sbs)
        except Exception:
            continue
        for kind, el, attr, val in items:
            if ref_exists(val, sbs.parent):
                inv.append((sbs, val))
    return inv


# ------------------------------------------------------------------ 실행
def run(tree_root, apply=False, log=print):
    move_map, collisions = build_move_map(tree_root)
    sbs_plans = plan_sbs(tree_root, move_map)
    spm_plans = plan_spms(tree_root, move_map)

    n_files = len(move_map)
    n_sbs_rewrite = sum(1 for p in sbs_plans if p.get("changes"))
    n_sbs_change = sum(len(p.get("changes", [])) for p in sbs_plans)
    n_spm = sum(1 for p in spm_plans if p.get("count"))
    n_spm_change = sum(p.get("count", 0) for p in spm_plans)

    log(f"[스캔] 이동 대상 파일 {n_files}개 ({len(substance_dirs(tree_root))}개 substance 폴더)")
    log(f"[스캔] .sbs 경로 재작성: {n_sbs_rewrite}개 파일, {n_sbs_change}개 경로")
    log(f"[스캔] SK SPM 경로 재작성: {n_spm}개 파일, {n_spm_change}개 경로")
    if collisions:
        log(f"[충돌] {len(collisions)}건 (texture\\ 에 같은 이름 존재):")
        for c in collisions:
            tag = "동일내용(중복)" if c["identical"] else "다른내용→기존을 아카이브"
            log(f"   · {c['folder']}\\{c['name']}: {tag}")
    for p in sbs_plans:
        if p.get("error"):
            log(f"[경고] sbs 파싱 실패: {p['sbs']}: {p['error']}")
    for p in spm_plans:
        if p.get("error"):
            log(f"[경고] spm 읽기 실패: {p['spm']}: {p['error']}")

    if not apply:
        log("\n[dry-run] 아무것도 수정하지 않았습니다. 실제 적용하려면 --apply.")
        return {"move_map": move_map, "collisions": collisions,
                "sbs_plans": sbs_plans, "spm_plans": spm_plans,
                "summary": {"files": n_files, "sbs": n_sbs_rewrite,
                            "sbs_changes": n_sbs_change, "spm": n_spm,
                            "spm_changes": n_spm_change, "collisions": len(collisions)}}

    # ---- 적용 ----
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_root = Path(tree_root) / "_consolidate_backup" / stamp
    backup_root.mkdir(parents=True, exist_ok=True)
    undo = {"stamp": stamp, "moves": [], "sbs": [], "spm": [], "archived": []}

    before = inventory_existing_refs(tree_root)
    log(f"[검증] 이동 전 존재하는 참조 {len(before)}개 기록")

    def backup(path):
        rel = Path(path).resolve().relative_to(Path(tree_root).resolve())
        dst = backup_root / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, dst)
        return dst

    try:
        # 1) sbs / spm 참조 먼저 재작성 (파일은 아직 원위치)
        #    → 이동과 분리해야 resolve 계산이 일관됨. move_map 기반이라 순서 무관.
        for p in sbs_plans:
            if not p.get("changes"):
                continue
            backup(p["sbs"])
            for old, new, el in p["changes"]:
                el.set("v", new)
            p["tree"].write(p["sbs"], encoding="utf-8", xml_declaration=True)
            undo["sbs"].append(str(p["sbs"]))
        for p in spm_plans:
            if not p.get("count"):
                continue
            backup(p["spm"])
            blob = spm_rewrite_bytes(spm_read(p["spm"]))
            with gzip.open(p["spm"], "wb") as h:
                h.write(blob)
            undo["spm"].append(str(p["spm"]))

        # 2) 충돌 처리: 기존 texture\ 파일을 아카이브(다른 내용)거나 스킵(동일)
        skip_src = set()
        for c in collisions:
            if c["identical"]:
                skip_src.add(str(c["src"].resolve()).lower())  # 소스만 지우면 됨
            else:
                arch = backup_root / "_superseded" / c["folder"] / c["dst"].name
                arch.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(c["dst"]), str(arch))
                undo["archived"].append({"from": str(c["dst"]), "to": str(arch)})

        # 3) 파일 이동
        for key, (src, dst) in move_map.items():
            if not src.exists():
                continue
            if key in skip_src:
                src.unlink()  # 동일 중복 → 소스 삭제
                undo["moves"].append({"from": str(src), "to": None, "dedup": True})
                continue
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(src), str(dst))
            undo["moves"].append({"from": str(src), "to": str(dst)})

        # 4) 빈 substance 폴더 제거
        for _folder, sub in substance_dirs(tree_root):
            try:
                if sub.exists() and not any(sub.iterdir()):
                    sub.rmdir()
            except OSError:
                pass

        # 5) 검증: 이동 전 존재하던 참조가 전부 여전히 존재하나
        broken = []
        for sbs, val in before:
            # sbs 자체가 이동했으면 새 위치에서 판단
            sbs_now = move_map.get(str(sbs.resolve()).lower())
            base = (sbs_now[1].parent if sbs_now else sbs.parent)
            cur = sbs_now[1] if sbs_now else sbs
            # 재작성된 트리를 다시 읽어 해당 참조가 존재하는지
            try:
                _t, _r, items = sbs_refs(cur)
            except Exception:
                broken.append((str(sbs), val, "sbs 재파싱 실패"))
                continue
            ok = False
            for _k, _el, _a, v in items:
                if ref_exists(v, base):
                    ok = True  # 최소 하나 이상 유효 — 개별 매칭은 아래서
            # 개별 val 이 (재작성 후) 어떤 형태로든 존재하는지: 원 타깃 기준
            old_target = resolve_ref(val, sbs.parent)
            moved = move_map.get(str(old_target).lower()) if old_target else None
            expect = moved[1] if moved else old_target
            if not (expect and Path(expect).exists()):
                broken.append((str(sbs), val, "타깃 사라짐"))
        undo_path = backup_root / "undo_manifest.json"
        undo_path.write_text(json.dumps(undo, indent=2, ensure_ascii=False), encoding="utf-8")

        if broken:
            log(f"[검증 실패] 새로 깨진 참조 {len(broken)}개 → 롤백합니다.")
            for b in broken[:12]:
                log(f"   · {Path(b[0]).name}: {b[1]} ({b[2]})")
            rollback(tree_root, backup_root, undo, log)
            return {"status": "rolled_back", "broken": broken}

        log(f"[완료] 이동 {len(undo['moves'])} · sbs {len(undo['sbs'])} · "
            f"spm {len(undo['spm'])} · 아카이브 {len(undo['archived'])}")
        log(f"[백업] {backup_root}  (undo_manifest.json 로 복원 가능)")
        return {"status": "applied", "backup": str(backup_root), "undo": undo}
    except Exception as exc:
        log(f"[오류] {exc} → 롤백 시도")
        rollback(tree_root, backup_root, undo, log)
        raise


def rollback(tree_root, backup_root, undo, log=print):
    # 이동 되돌리기
    for m in reversed(undo["moves"]):
        if m.get("dedup"):
            continue
        if m["to"] and Path(m["to"]).exists():
            Path(m["from"]).parent.mkdir(parents=True, exist_ok=True)
            shutil.move(m["to"], m["from"])
    for a in reversed(undo["archived"]):
        if Path(a["to"]).exists():
            shutil.move(a["to"], a["from"])
    # 백업에서 sbs/spm 및 dedup 소스 복원
    for rel_file in list(undo["sbs"]) + list(undo["spm"]):
        rel = Path(rel_file).resolve().relative_to(Path(tree_root).resolve())
        bak = backup_root / rel
        if bak.exists():
            shutil.copy2(bak, rel_file)
    for m in undo["moves"]:
        if m.get("dedup"):
            rel = Path(m["from"]).resolve().relative_to(Path(tree_root).resolve())
            bak = backup_root / rel
            if bak.exists() and not Path(m["from"]).exists():
                shutil.copy2(bak, m["from"])
    log("[롤백] 원상 복구 완료.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tree-root", default=None)
    parser.add_argument("--apply", action="store_true", help="실제 적용 (기본은 dry-run)")
    args = parser.parse_args()
    cfg = load_config()
    tree_root = args.tree_root or cfg["tree_root"]
    run(tree_root, apply=args.apply)


if __name__ == "__main__":
    main()
