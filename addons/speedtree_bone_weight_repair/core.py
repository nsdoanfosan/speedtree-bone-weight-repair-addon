import gzip
import json
import math
import os
import re
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict
from pathlib import Path

import bpy
from mathutils import Vector


EPSILON = 1e-6
COORD_RE = re.compile(r"X:([-+0-9.eE]+),\s*Y:([-+0-9.eE]+),\s*Z:([-+0-9.eE]+)")


def write_report(path, data):
    if not path:
        return
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def read_spm_xml(path):
    with open(path, "rb") as handle:
        magic = handle.read(2)
    if magic == b"\x1f\x8b":
        with gzip.open(path, "rb") as handle:
            return handle.read()
    with open(path, "rb") as handle:
        return handle.read()


def child_text(element, name, default=None):
    child = element.find(name)
    if child is None or child.text is None:
        return default
    return child.text


def parse_coord(text):
    match = COORD_RE.search(text or "")
    if not match:
        return None
    return tuple(float(match.group(index)) for index in range(1, 4))


def coord_key(coord, places=4):
    if coord is None:
        return None
    return tuple(round(value, places) for value in coord)


def vec_div(value, scale):
    return (value[0] / scale, value[1] / scale, value[2] / scale)


def dist(a, b):
    return math.sqrt(sum((a[index] - b[index]) ** 2 for index in range(3)))


def distance_point_segment(point, start, end):
    line = end - start
    denom = line.dot(line)
    if denom <= 1e-12:
        return (point - start).length
    t = max(0.0, min(1.0, (point - start).dot(line) / denom))
    return (point - (start + line * t)).length


def tuple_point_segment_distance(point, start, end):
    return distance_point_segment(Vector(point), Vector(start), Vector(end))


def get_armature(name):
    if name:
        obj = bpy.data.objects.get(name)
        if obj and obj.type == "ARMATURE":
            return obj
        raise RuntimeError(f"Armature not found: {name}")
    armatures = [obj for obj in bpy.data.objects if obj.type == "ARMATURE"]
    if len(armatures) != 1:
        raise RuntimeError(f"Expected one armature, found {[obj.name for obj in armatures]}")
    return armatures[0]


def parse_speedtree(path):
    root = ET.fromstring(read_spm_xml(path))
    generators = {}
    nodes = {}
    node_order = []

    for gen in root.findall(".//Generator"):
        guid = child_text(gen, "GUID")
        if not guid:
            continue
        generators[guid] = {
            "guid": guid,
            "type": gen.attrib.get("Type"),
            "name": child_text(gen, "Name", ""),
            "level": child_text(gen, "Level"),
        }

    for node in root.findall(".//Node"):
        guid = child_text(node, "GUID")
        if not guid:
            continue
        name = child_text(node, "Name", "")
        nodes[guid] = {
            "guid": guid,
            "type": node.attrib.get("Type"),
            "gen": child_text(node, "GeneratorGUID"),
            "parent": child_text(node, "ParentGUID"),
            "name": name,
            "coord": parse_coord(name),
        }
        node_order.append(guid)

    return {
        "generators": generators,
        "nodes": nodes,
        "node_order": node_order,
        "version": root.attrib.get("VersionString", root.attrib.get("Version")),
    }


def find_base_ref_pairs(tree, raw_tolerance=0.05):
    nodes = tree["nodes"]
    base_refs = [node for node in nodes.values() if node["type"] == "BaseRef" and node["coord"]]
    refs_by_key = defaultdict(list)
    for ref in base_refs:
        refs_by_key[coord_key(ref["coord"])].append(ref)

    records = []
    issues = []
    for guid in tree["node_order"]:
        node = nodes[guid]
        if node["type"] != "Branch":
            continue
        base = nodes.get(node["parent"])
        if not base or base["type"] != "Base" or not base["coord"] or not node["coord"]:
            continue

        candidates = refs_by_key.get(coord_key(base["coord"]), [])
        if not candidates:
            candidates = [ref for ref in base_refs if dist(ref["coord"], base["coord"]) <= raw_tolerance]
        candidates = [ref for ref in candidates if nodes.get(ref["parent"], {}).get("type") == "Branch"]
        if not candidates:
            issues.append({"child_branch": guid, "base": base["guid"], "error": "no matching BaseRef"})
            continue

        ref = min(candidates, key=lambda item: dist(item["coord"], base["coord"]))
        parent_branch = nodes[ref["parent"]]
        records.append(
            {
                "child_branch_guid": guid,
                "child_branch_name": node["name"],
                "child_coord_raw": node["coord"],
                "base_guid": base["guid"],
                "base_name": base["name"],
                "attach_coord_raw": base["coord"],
                "base_ref_guid": ref["guid"],
                "base_ref_name": ref["name"],
                "parent_branch_guid": parent_branch["guid"],
                "parent_branch_name": parent_branch["name"],
                "parent_branch_coord_raw": parent_branch["coord"],
            }
        )
    return records, issues


def collect_bones(armature):
    bones = {}
    children = defaultdict(list)
    matrix = armature.matrix_world
    for bone in armature.data.bones:
        head = matrix @ bone.head_local
        tail = matrix @ bone.tail_local
        parent = bone.parent.name if bone.parent else None
        bones[bone.name] = {
            "name": bone.name,
            "parent": parent,
            "head": (head.x, head.y, head.z),
            "tail": (tail.x, tail.y, tail.z),
        }
        if parent:
            children[parent].append(bone.name)
    return bones, children


def choose_scale(records, orphan_roots, bones, candidates):
    if not records or not orphan_roots:
        return candidates[0], []
    sample_records = records[: min(len(records), 500)]
    scores = []
    for scale in candidates:
        nearest = []
        for rec in sample_records:
            point = vec_div(rec["child_coord_raw"], scale)
            nearest.append(min(dist(point, bones[root]["head"]) for root in orphan_roots))
        nearest.sort()
        scores.append({"scale": scale, "median_nearest_child_root": nearest[len(nearest) // 2]})
    scores.sort(key=lambda item: item["median_nearest_child_root"])
    return scores[0]["scale"], scores


def nearest_unused_start(coord, candidates, bones, used):
    best = None
    best_distance = None
    for name in candidates:
        if name in used:
            continue
        distance = dist(coord, bones[name]["head"])
        if best_distance is None or distance < best_distance:
            best = name
            best_distance = distance
    return best, best_distance


def nearest_start(coord, candidates, bones):
    best = None
    best_distance = None
    for name in candidates:
        distance = dist(coord, bones[name]["head"])
        if best_distance is None or distance < best_distance:
            best = name
            best_distance = distance
    return best, best_distance


def branch_chain(start_bone, children):
    chain = []
    stack = [start_bone]
    seen = set()
    while stack:
        name = stack.pop()
        if name in seen:
            continue
        seen.add(name)
        chain.append(name)
        for child in children.get(name, []):
            if child.endswith("_End"):
                stack.append(child)
    return chain


def nearest_bone_on_chain(point, chain, bones):
    best = None
    best_distance = None
    for name in chain:
        bone = bones[name]
        distance = tuple_point_segment_distance(point, bone["head"], bone["tail"])
        if best_distance is None or distance < best_distance:
            best = name
            best_distance = distance
    return best, best_distance


def build_reparent_map(records, bones, children, true_root, scale, tolerance):
    roots = [name for name, bone in bones.items() if bone["parent"] is None]
    orphan_roots = [root for root in roots if root != true_root and root.endswith("_Start")]
    all_starts = [name for name in bones if name.endswith("_Start")]

    mapping = {}
    details = []
    used_children = set()
    problems = []

    for rec in records:
        child_point = vec_div(rec["child_coord_raw"], scale)
        child_bone, child_distance = nearest_unused_start(child_point, orphan_roots, bones, used_children)
        if not child_bone:
            problems.append({**rec, "error": "no unused orphan root for child branch"})
            continue
        if child_distance is None or child_distance > tolerance:
            problems.append({**rec, "error": "child root distance over tolerance", "distance": child_distance})
            continue

        parent_branch_point = vec_div(rec["parent_branch_coord_raw"], scale)
        parent_start, parent_start_distance = nearest_start(parent_branch_point, all_starts, bones)
        if not parent_start:
            problems.append({**rec, "child_bone": child_bone, "error": "no parent branch start"})
            continue

        attach_point = vec_div(rec["attach_coord_raw"], scale)
        chain = branch_chain(parent_start, children)
        parent_bone, parent_distance = nearest_bone_on_chain(attach_point, chain, bones)
        if not parent_bone:
            problems.append({**rec, "child_bone": child_bone, "error": "empty parent branch chain"})
            continue

        mapping[child_bone] = parent_bone
        used_children.add(child_bone)
        details.append(
            {
                **rec,
                "child_bone": child_bone,
                "parent_branch_start_bone": parent_start,
                "parent_bone": parent_bone,
                "child_distance": child_distance,
                "parent_branch_start_distance": parent_start_distance,
                "parent_attach_distance": parent_distance,
                "parent_chain_bones": len(chain),
            }
        )

    return mapping, details, problems, roots, orphan_roots


def apply_reparent_mapping(armature, mapping):
    bpy.context.view_layer.objects.active = armature
    for obj in bpy.context.scene.objects:
        obj.select_set(False)
    armature.select_set(True)
    bpy.ops.object.mode_set(mode="EDIT")
    edit_bones = armature.data.edit_bones
    applied = 0
    for child, parent in mapping.items():
        if child not in edit_bones or parent not in edit_bones:
            continue
        edit_bones[child].use_connect = False
        edit_bones[child].parent = edit_bones[parent]
        applied += 1
    bpy.ops.object.mode_set(mode="OBJECT")
    return applied


def run_reparent_from_spm(spm_path, armature_name, true_root, scale_value="auto", tolerance=0.08, apply=True, strict=True, report_path=""):
    tree = parse_speedtree(spm_path)
    records, spm_issues = find_base_ref_pairs(tree)
    armature = get_armature(armature_name)
    bones, children = collect_bones(armature)
    roots_before = [name for name, bone in bones.items() if bone["parent"] is None]
    orphan_roots = [root for root in roots_before if root != true_root and root.endswith("_Start")]

    if scale_value == "auto":
        scale, scale_scores = choose_scale(records, orphan_roots, bones, [1.0, 3.28084, 100.0, 0.01])
    else:
        scale = float(scale_value)
        scale_scores = [{"scale": scale, "manual": True}]

    mapping, details, problems, roots_before, orphan_roots = build_reparent_map(
        records, bones, children, true_root, scale, tolerance
    )

    report = {
        "blend": bpy.data.filepath,
        "spm": spm_path,
        "spm_version": tree["version"],
        "armature": armature.name,
        "bones": len(bones),
        "roots_before": len(roots_before),
        "root_names_before": roots_before[:100],
        "true_root": true_root,
        "orphan_start_roots": len(orphan_roots),
        "base_ref_records": len(records),
        "mapping_count": len(mapping),
        "scale": scale,
        "scale_scores": scale_scores,
        "spm_issues": spm_issues[:80],
        "problem_count": len(problems),
        "problems": problems[:80],
        "mapping": mapping,
        "details_sample": details[:30],
        "applied": False,
    }

    blocked = strict and (len(mapping) != len(orphan_roots) or problems or spm_issues)
    if blocked:
        report["status"] = "blocked"
        report["error"] = "Reparent mapping was not complete; no changes applied."
    elif apply:
        applied = apply_reparent_mapping(armature, mapping)
        roots_after = [bone.name for bone in armature.data.bones if bone.parent is None]
        report.update(
            {
                "status": "applied",
                "applied": True,
                "applied_count": applied,
                "roots_after": len(roots_after),
                "root_names_after": roots_after,
            }
        )
    else:
        report["status"] = "dry-run-ok"

    write_report(report_path, report)
    return report


def find_skinned_parent(obj, armature):
    current = obj.parent
    while current:
        if current.type == "MESH":
            for modifier in current.modifiers:
                if modifier.type == "ARMATURE" and modifier.object == armature:
                    return current
        current = current.parent
    return None


def object_world_vertices(obj):
    return [obj.matrix_world @ vertex.co for vertex in obj.data.vertices]


def choose_bone_for_instance(obj, skinned_parent, armature, fallback_all_bones=False):
    points = object_world_vertices(obj)
    center = sum(points, Vector()) / len(points)
    group_names = [group.name for group in skinned_parent.vertex_groups]
    candidates = [name for name in group_names if name in armature.data.bones and name != "Root"]
    if not candidates and fallback_all_bones:
        candidates = [bone.name for bone in armature.data.bones if bone.name != "Root"]
    if not candidates:
        return None, None

    best_name = None
    best_distance = math.inf
    for name in candidates:
        bone = armature.data.bones[name]
        head = armature.matrix_world @ bone.head_local
        tail = armature.matrix_world @ bone.tail_local
        distance = distance_point_segment(center, head, tail)
        if distance < best_distance:
            best_name = name
            best_distance = distance
    return best_name, best_distance


def unique_material_index(materials, material):
    if material is None:
        return 0
    if material not in materials:
        materials.append(material)
    return materials.index(material)


def collect_loose_instances(armature, name_contains):
    instances = []
    needle = name_contains.lower()
    for obj in bpy.data.objects:
        if obj.type != "MESH":
            continue
        if needle and needle not in obj.name.lower():
            continue
        if len(obj.data.vertices) == 0:
            continue
        if obj.vertex_groups:
            continue
        if any(modifier.type == "ARMATURE" for modifier in obj.modifiers):
            continue
        skinned_parent = find_skinned_parent(obj, armature)
        if skinned_parent:
            instances.append((obj, skinned_parent))
    return instances


def build_skinned_instance_mesh(armature, instances, out_name, hide_originals, fallback_all_bones):
    verts = []
    faces = []
    face_materials = []
    assignments = []
    materials = []
    skipped = []
    armature_inverse = armature.matrix_world.inverted()

    for obj, skinned_parent in instances:
        bone_name, bone_distance = choose_bone_for_instance(obj, skinned_parent, armature, fallback_all_bones)
        if not bone_name:
            skipped.append({"object": obj.name, "reason": "no_candidate_bone", "parent": skinned_parent.name})
            continue

        start_index = len(verts)
        local_points = [armature_inverse @ point for point in object_world_vertices(obj)]
        verts.extend(local_points)

        for poly in obj.data.polygons:
            faces.append([start_index + index for index in poly.vertices])
            src_mat = obj.data.materials[poly.material_index] if obj.data.materials and poly.material_index < len(obj.data.materials) else None
            face_materials.append(unique_material_index(materials, src_mat))

        assignments.append(
            {
                "object": obj.name,
                "parent_mesh": skinned_parent.name,
                "bone": bone_name,
                "vertex_start": start_index,
                "vertex_count": len(local_points),
                "distance": bone_distance,
            }
        )

        if hide_originals:
            obj.hide_viewport = True
            obj.hide_render = True

    mesh = bpy.data.meshes.new(out_name + "Mesh")
    mesh.from_pydata([tuple(vertex) for vertex in verts], [], faces)
    mesh.update()

    for material in materials:
        mesh.materials.append(material)
    for poly, mat_index in zip(mesh.polygons, face_materials):
        poly.material_index = mat_index

    out_obj = bpy.data.objects.new(out_name, mesh)
    bpy.context.scene.collection.objects.link(out_obj)
    out_obj.parent = armature
    out_obj.matrix_world = armature.matrix_world.copy()

    groups = {}
    for assignment in assignments:
        group = groups.get(assignment["bone"])
        if group is None:
            group = out_obj.vertex_groups.new(name=assignment["bone"])
            groups[assignment["bone"]] = group
        indices = range(assignment["vertex_start"], assignment["vertex_start"] + assignment["vertex_count"])
        group.add(list(indices), 1.0, "ADD")

    modifier = out_obj.modifiers.new(name="Armature", type="ARMATURE")
    modifier.object = armature
    return out_obj, assignments, skipped


def run_skin_loose_instances(armature_name, name_contains, out_name, hide_originals=True, fallback_all_bones=False, apply=True, report_path=""):
    armature = get_armature(armature_name)
    instances = collect_loose_instances(armature, name_contains)
    report = {
        "file": bpy.data.filepath,
        "armature": armature.name,
        "name_contains": name_contains,
        "candidate_instances": len(instances),
        "apply": apply,
    }
    if not apply:
        report["status"] = "dry-run-ok"
    elif not instances:
        report["status"] = "skipped-no-instances"
    else:
        old = bpy.data.objects.get(out_name)
        if old:
            bpy.data.objects.remove(old, do_unlink=True)
        out_obj, assignments, skipped = build_skinned_instance_mesh(
            armature, instances, out_name, hide_originals, fallback_all_bones
        )
        report.update(
            {
                "status": "applied",
                "created_object": out_obj.name,
                "created_vertices": len(out_obj.data.vertices),
                "created_faces": len(out_obj.data.polygons),
                "assigned_instances": len(assignments),
                "skipped_instances": len(skipped),
                "sample_assignments": assignments[:50],
                "skipped": skipped[:50],
            }
        )
    write_report(report_path, report)
    return report


def armature_modifier(obj, armature):
    for modifier in obj.modifiers:
        if modifier.type == "ARMATURE" and modifier.object == armature:
            return modifier
    return None


def group_name_by_index(obj):
    return {group.index: group.name for group in obj.vertex_groups}


def get_vertex_weights(obj, vertex):
    names = group_name_by_index(obj)
    weights = []
    for group_ref in vertex.groups:
        if group_ref.weight <= EPSILON:
            continue
        name = names.get(group_ref.group)
        if name:
            weights.append((name, group_ref.weight))
    return weights


def candidate_bones_for_object(obj, armature, valid_bones, fallback_all=True):
    candidates = set()
    for group in obj.vertex_groups:
        if group.name in valid_bones:
            candidates.add(group.name)
    for name in list(candidates):
        bone = armature.data.bones.get(name)
        if not bone:
            continue
        if bone.parent:
            candidates.add(bone.parent.name)
        for child in bone.children:
            candidates.add(child.name)
    if not candidates and fallback_all:
        candidates = set(valid_bones)
    return candidates


def nearest_bone_name(armature, world_point, candidates):
    best_name = None
    best_distance = None
    for name in candidates:
        bone = armature.data.bones.get(name)
        if not bone:
            continue
        head = armature.matrix_world @ bone.head_local
        tail = armature.matrix_world @ bone.tail_local
        distance = distance_point_segment(world_point, head, tail)
        if best_distance is None or distance < best_distance:
            best_name = name
            best_distance = distance
    return best_name, best_distance


def ensure_group(obj, name):
    group = obj.vertex_groups.get(name)
    if group is None:
        group = obj.vertex_groups.new(name=name)
    return group


def replace_vertex_weights(obj, vertex_index, weights):
    for group in obj.vertex_groups:
        try:
            group.remove([vertex_index])
        except RuntimeError:
            pass
    for name, weight in weights.items():
        if weight > EPSILON:
            ensure_group(obj, name).add([vertex_index], weight, "REPLACE")


def remove_empty_non_bone_groups(obj, valid_bones):
    removed = []
    for group in list(obj.vertex_groups):
        if group.name in valid_bones:
            continue
        used = False
        for vertex in obj.data.vertices:
            for group_ref in vertex.groups:
                if group_ref.group == group.index and group_ref.weight > EPSILON:
                    used = True
                    break
            if used:
                break
        if not used:
            removed.append(group.name)
            obj.vertex_groups.remove(group)
    return removed


def repair_object_weights(obj, armature, valid_bones, fill_zero_weight=True, max_samples_per_object=20):
    candidates = candidate_bones_for_object(obj, armature, valid_bones)
    report = {
        "object": obj.name,
        "vertex_count": len(obj.data.vertices),
        "invalid_groups": Counter(),
        "repaired_invalid_vertices": 0,
        "filled_zero_weight_vertices": 0,
        "samples": [],
    }
    for vertex in obj.data.vertices:
        entries = get_vertex_weights(obj, vertex)
        valid_entries = [(name, weight) for name, weight in entries if name in valid_bones]
        invalid_entries = [(name, weight) for name, weight in entries if name not in valid_bones]
        needs_repair = bool(invalid_entries)
        needs_fill = fill_zero_weight and not entries
        if not needs_repair and not needs_fill:
            continue

        for name, _weight in invalid_entries:
            report["invalid_groups"][name] += 1

        valid_total = sum(weight for _name, weight in valid_entries)
        target_name = None
        target_distance = None
        if valid_total > EPSILON:
            new_weights = {name: weight / valid_total for name, weight in valid_entries}
        else:
            world_point = obj.matrix_world @ vertex.co
            target_name, target_distance = nearest_bone_name(armature, world_point, candidates)
            if not target_name:
                continue
            new_weights = {target_name: 1.0}

        replace_vertex_weights(obj, vertex.index, new_weights)
        if needs_repair:
            report["repaired_invalid_vertices"] += 1
        else:
            report["filled_zero_weight_vertices"] += 1

        if len(report["samples"]) < max_samples_per_object:
            report["samples"].append(
                {
                    "vertex": vertex.index,
                    "old": [{"name": name, "weight": weight} for name, weight in entries],
                    "new": [{"name": name, "weight": weight} for name, weight in sorted(new_weights.items())],
                    "nearest_target": target_name,
                    "nearest_distance": target_distance,
                }
            )
    report["invalid_groups"] = dict(report["invalid_groups"].most_common())
    return report


def count_remaining_issues(objects, valid_bones):
    invalid_weight_vertices = 0
    zero_weight_vertices = 0
    invalid_group_counts = Counter()
    for obj in objects:
        names = group_name_by_index(obj)
        for vertex in obj.data.vertices:
            total = 0.0
            has_invalid = False
            for group_ref in vertex.groups:
                if group_ref.weight <= EPSILON:
                    continue
                total += group_ref.weight
                name = names.get(group_ref.group)
                if name not in valid_bones:
                    has_invalid = True
                    invalid_group_counts[name] += 1
            if total <= EPSILON:
                zero_weight_vertices += 1
            if has_invalid:
                invalid_weight_vertices += 1
    return {
        "remaining_invalid_weight_vertices": invalid_weight_vertices,
        "remaining_zero_weight_vertices": zero_weight_vertices,
        "remaining_invalid_group_counts": dict(invalid_group_counts.most_common()),
    }


def run_repair_invalid_weights(armature_name, mesh_regex="", fill_zero_weight=True, remove_empty_invalid_groups=True, max_samples_per_object=20, report_path=""):
    armature = get_armature(armature_name)
    name_filter = re.compile(mesh_regex) if mesh_regex else None
    valid_bones = {bone.name for bone in armature.data.bones}
    objects = []
    object_reports = []
    removed_groups = {}

    for obj in bpy.data.objects:
        if obj.type != "MESH":
            continue
        if name_filter and not name_filter.search(obj.name):
            continue
        if not armature_modifier(obj, armature):
            continue
        objects.append(obj)

    for obj in objects:
        report = repair_object_weights(obj, armature, valid_bones, fill_zero_weight, max_samples_per_object)
        if report["repaired_invalid_vertices"] or report["filled_zero_weight_vertices"]:
            object_reports.append(report)
        if remove_empty_invalid_groups:
            removed = remove_empty_non_bone_groups(obj, valid_bones)
            if removed:
                removed_groups[obj.name] = removed

    integrity = count_remaining_issues(objects, valid_bones)
    roots = [bone.name for bone in armature.data.bones if bone.parent is None]
    final_report = {
        "source_file": bpy.data.filepath,
        "armature": armature.name,
        "root_bones": roots,
        "checked_meshes": len(objects),
        "objects_repaired": len(object_reports),
        "total_repaired_invalid_vertices": sum(item["repaired_invalid_vertices"] for item in object_reports),
        "total_filled_zero_weight_vertices": sum(item["filled_zero_weight_vertices"] for item in object_reports),
        "invalid_group_vertex_counts": dict(
            Counter(
                group
                for item in object_reports
                for group, count in item["invalid_groups"].items()
                for _unused in range(count)
            ).most_common()
        ),
        "removed_empty_invalid_groups": removed_groups,
        "integrity_after_repair": integrity,
        "object_reports_sample": object_reports[:80],
    }
    write_report(report_path, final_report)
    return final_report


def armature_modifier_uses(obj, armature):
    return obj.type == "MESH" and any(modifier.type == "ARMATURE" and modifier.object == armature for modifier in obj.modifiers)


def source_uv_layers(source_objects):
    names = []
    seen = set()
    for obj in source_objects:
        for layer in obj.data.uv_layers:
            if layer.name not in seen:
                seen.add(layer.name)
                names.append(layer.name)
    return names


def material_index(state, material):
    if material is None:
        return 0
    if material.name not in state["by_name"]:
        state["slots"].append(material)
        state["by_name"][material.name] = len(state["slots"]) - 1
    return state["by_name"][material.name]


def copy_vertex_groups(src_obj, dst_obj, vertex_offset, group_map, valid_bones):
    src_groups = {group.index: group.name for group in src_obj.vertex_groups}
    skipped_invalid = 0
    copied_weights = 0
    for vertex in src_obj.data.vertices:
        out_index = vertex_offset + vertex.index
        for group_ref in vertex.groups:
            if group_ref.weight <= EPSILON:
                continue
            name = src_groups.get(group_ref.group)
            if not name or name not in valid_bones:
                skipped_invalid += 1
                continue
            dst_group = group_map.get(name)
            if dst_group is None:
                dst_group = dst_obj.vertex_groups.new(name=name)
                group_map[name] = dst_group
            dst_group.add([out_index], group_ref.weight, "ADD")
            copied_weights += 1
    return skipped_invalid, copied_weights


def merge_skinned_meshes(armature, source_objects, merged_name):
    armature_inverse = armature.matrix_world.inverted()
    valid_bones = {bone.name for bone in armature.data.bones}
    uv_names = source_uv_layers(source_objects)
    verts = []
    faces = []
    face_materials = []
    face_smooth = []
    uv_data = {name: [] for name in uv_names}
    source_ranges = []
    material_state = {"slots": [], "by_name": {}}

    for obj in source_objects:
        mesh = obj.data
        vertex_offset = len(verts)
        for vertex in mesh.vertices:
            verts.append(armature_inverse @ (obj.matrix_world @ vertex.co))
        for poly in mesh.polygons:
            faces.append([vertex_offset + index for index in poly.vertices])
            material = mesh.materials[poly.material_index] if mesh.materials and poly.material_index < len(mesh.materials) else None
            face_materials.append(material_index(material_state, material))
            face_smooth.append(poly.use_smooth)
            for uv_name in uv_names:
                layer = mesh.uv_layers.get(uv_name)
                for loop_index in poly.loop_indices:
                    uv_data[uv_name].append(tuple(layer.data[loop_index].uv) if layer else (0.0, 0.0))
        source_ranges.append(
            {
                "object": obj.name,
                "vertex_start": vertex_offset,
                "vertex_count": len(mesh.vertices),
                "face_count": len(mesh.polygons),
            }
        )

    merged_mesh = bpy.data.meshes.new(merged_name + "Mesh")
    merged_mesh.from_pydata([tuple(vertex) for vertex in verts], [], faces)
    merged_mesh.update()
    for material in material_state["slots"]:
        merged_mesh.materials.append(material)
    for poly, mat_index, smooth in zip(merged_mesh.polygons, face_materials, face_smooth):
        poly.material_index = mat_index
        poly.use_smooth = smooth
    for uv_name in uv_names:
        uv_layer = merged_mesh.uv_layers.new(name=uv_name)
        for loop_index, uv in enumerate(uv_data[uv_name]):
            uv_layer.data[loop_index].uv = uv

    old = bpy.data.objects.get(merged_name)
    if old:
        bpy.data.objects.remove(old, do_unlink=True)
    merged_obj = bpy.data.objects.new(merged_name, merged_mesh)
    bpy.context.scene.collection.objects.link(merged_obj)
    merged_obj.parent = armature
    merged_obj.matrix_world = armature.matrix_world.copy()

    group_map = {}
    skipped_invalid_weights = 0
    copied_weights = 0
    for obj, info in zip(source_objects, source_ranges):
        skipped, copied = copy_vertex_groups(obj, merged_obj, info["vertex_start"], group_map, valid_bones)
        skipped_invalid_weights += skipped
        copied_weights += copied

    modifier = merged_obj.modifiers.new(name="Armature", type="ARMATURE")
    modifier.object = armature
    return merged_obj, source_ranges, len(material_state["slots"]), skipped_invalid_weights, copied_weights, uv_names


def count_zero_weight_vertices(obj):
    zero = 0
    for vertex in obj.data.vertices:
        if not any(group.weight > EPSILON for group in vertex.groups):
            zero += 1
    return zero


def run_merge_export(armature_name, merged_name, fbx_path="", mesh_regex="", include_hidden=False, report_path=""):
    armature = get_armature(armature_name)
    name_filter = re.compile(mesh_regex) if mesh_regex else None
    source_objects = []
    for obj in bpy.data.objects:
        if obj.name == merged_name:
            continue
        if not armature_modifier_uses(obj, armature):
            continue
        if name_filter and not name_filter.search(obj.name):
            continue
        if not include_hidden and (obj.hide_get() or obj.hide_viewport):
            continue
        if len(obj.data.vertices) == 0:
            continue
        source_objects.append(obj)
    if not source_objects:
        raise RuntimeError("No skinned source meshes found.")

    merged_obj, ranges, material_count, skipped_invalid_weights, copied_weights, uv_names = merge_skinned_meshes(
        armature, source_objects, merged_name
    )
    zero_weight_vertices = count_zero_weight_vertices(merged_obj)
    for obj in bpy.data.objects:
        obj.select_set(False)
    armature.select_set(True)
    merged_obj.select_set(True)
    bpy.context.view_layer.objects.active = armature
    roots = [bone.name for bone in armature.data.bones if bone.parent is None]

    report = {
        "source_file": bpy.data.filepath,
        "fbx": fbx_path,
        "armature": armature.name,
        "bone_count": len(armature.data.bones),
        "root_bones": roots,
        "source_mesh_count": len(source_objects),
        "merged_object": merged_obj.name,
        "merged_vertices": len(merged_obj.data.vertices),
        "merged_faces": len(merged_obj.data.polygons),
        "merged_vertex_groups": len(merged_obj.vertex_groups),
        "zero_weight_vertices": zero_weight_vertices,
        "skipped_invalid_weights": skipped_invalid_weights,
        "copied_weights": copied_weights,
        "material_count": material_count,
        "uv_layers": uv_names,
        "sample_sources": ranges[:80],
    }

    if fbx_path:
        Path(fbx_path).parent.mkdir(parents=True, exist_ok=True)
        bpy.ops.export_scene.fbx(
            filepath=fbx_path,
            use_selection=True,
            object_types={"ARMATURE", "MESH"},
            add_leaf_bones=False,
            bake_anim=False,
            use_mesh_modifiers=False,
            mesh_smooth_type="FACE",
            use_custom_props=False,
            primary_bone_axis="Y",
            secondary_bone_axis="X",
            armature_nodetype="NULL",
            path_mode="AUTO",
        )

    write_report(report_path, report)
    return report


def default_paths(settings):
    blend_path = bpy.data.filepath
    if not blend_path:
        raise RuntimeError("Save or open a .blend before running the SpeedTree repair pipeline.")
    out_dir = settings.get("out_dir") or os.path.dirname(blend_path)
    name_stem = settings.get("name_stem") or Path(blend_path).stem
    merged_name = settings.get("merged_name") or f"{name_stem}_Codex_MergedSkinned_WeightsFixed"
    return {
        "out_dir": out_dir,
        "name_stem": name_stem,
        "merged_name": merged_name,
        "fixed_blend": os.path.join(out_dir, f"{name_stem}_fixed_codex.blend"),
        "leaf_blend": os.path.join(out_dir, f"{name_stem}_fixed_codex_skinned_leaves.blend"),
        "weight_blend": os.path.join(out_dir, f"{name_stem}_fixed_codex_skinned_weights_fixed.blend"),
        "export_blend": os.path.join(out_dir, f"{name_stem}_codex_merged_skinned_weights_fixed_export.blend"),
        "fbx": os.path.join(out_dir, f"{name_stem}_codex_merged_skinned_weights_fixed.fbx"),
        "reparent_report": os.path.join(out_dir, f"{name_stem}_reparent_report_codex.json"),
        "leaf_report": os.path.join(out_dir, f"{name_stem}_leaf_skin_report_codex.json"),
        "weight_report": os.path.join(out_dir, f"{name_stem}_weight_repair_report_codex.json"),
        "export_report": os.path.join(out_dir, f"{name_stem}_codex_merged_skinned_weights_fixed_export_report.json"),
        "pipeline_report": os.path.join(out_dir, f"{name_stem}_speedtree_repair_pipeline_report_codex.json"),
    }


def save_blend(path):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    bpy.ops.wm.save_as_mainfile(filepath=path)


def run_full_pipeline(settings):
    paths = default_paths(settings)
    reports = {"paths": paths, "steps": []}

    reparent = run_reparent_from_spm(
        settings["spm_path"],
        settings.get("armature_name", "Root"),
        settings.get("true_root", "Bone_1_Start"),
        settings.get("scale_value", "auto"),
        settings.get("tolerance", 0.08),
        apply=True,
        strict=settings.get("strict_reparent", True),
        report_path=paths["reparent_report"],
    )
    reports["steps"].append({"name": "reparent", "status": reparent.get("status"), "report": paths["reparent_report"]})
    if reparent.get("status") == "blocked":
        write_report(paths["pipeline_report"], reports)
        raise RuntimeError(reparent.get("error", "Reparent blocked."))
    save_blend(paths["fixed_blend"])

    if settings.get("skip_leaf_skin", False):
        reports["steps"].append({"name": "skin_loose_instances", "status": "skipped"})
    else:
        leaf = run_skin_loose_instances(
            settings.get("armature_name", "Root"),
            settings.get("leaf_name_contains", "leaf"),
            settings.get("leaf_out_name", "Leaves_Skinned_Codex"),
            hide_originals=settings.get("hide_originals", True),
            fallback_all_bones=settings.get("fallback_all_bones", False),
            apply=True,
            report_path=paths["leaf_report"],
        )
        reports["steps"].append({"name": "skin_loose_instances", "status": leaf.get("status"), "report": paths["leaf_report"]})
        save_blend(paths["leaf_blend"])

    weights = run_repair_invalid_weights(
        settings.get("armature_name", "Root"),
        mesh_regex=settings.get("mesh_regex", ""),
        fill_zero_weight=settings.get("fill_zero_weight", True),
        remove_empty_invalid_groups=settings.get("remove_empty_invalid_groups", True),
        max_samples_per_object=settings.get("max_samples_per_object", 20),
        report_path=paths["weight_report"],
    )
    reports["steps"].append(
        {
            "name": "repair_invalid_weights",
            "status": "applied",
            "report": paths["weight_report"],
            "remaining": weights.get("integrity_after_repair", {}),
        }
    )
    save_blend(paths["weight_blend"])

    export = run_merge_export(
        settings.get("armature_name", "Root"),
        paths["merged_name"],
        fbx_path=paths["fbx"] if settings.get("export_fbx", True) else "",
        mesh_regex=settings.get("mesh_regex", ""),
        include_hidden=settings.get("include_hidden", False),
        report_path=paths["export_report"],
    )
    reports["steps"].append(
        {
            "name": "merge_export",
            "status": "applied",
            "report": paths["export_report"],
            "zero_weight_vertices": export.get("zero_weight_vertices"),
            "skipped_invalid_weights": export.get("skipped_invalid_weights"),
        }
    )
    save_blend(paths["export_blend"])
    reports["status"] = "done"
    write_report(paths["pipeline_report"], reports)
    return reports
