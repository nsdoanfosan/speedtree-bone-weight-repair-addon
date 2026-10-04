"""Generic source extraction and adaptive terrain groups owned by the BWR addon.

The canonical source must already be open in a disposable Blender process. This
module never opens/saves a blend, changes preferences, exports FBX, or invokes UE.
The evidence output is a small, reproducible grouping receipt, not a source copy.
"""
from __future__ import annotations

from collections import defaultdict
import hashlib
import json
from pathlib import Path
import struct

import bpy
import numpy as np

from . import debris_terrain_groups as common
from .debris_terrain_voronoi import adaptive_voronoi


def extract_source_parts(context, *, source_path):
    expected = Path(source_path).resolve(strict=True)
    current = Path(bpy.data.filepath).resolve()
    common._require(expected.suffix.casefold()==".blend" and current==expected,
                    "source_path must be the currently open canonical .blend")
    export = bpy.data.collections.get("Export")
    common._require(export is not None,"Original Export collection is absent")
    meshes = [obj for obj in export.all_objects if obj.type=="MESH" and len(obj.data.vertices)
              and obj.get(common.OWNER_TAG) is not True]
    common._require(len(meshes)==1,"Exactly one original Export mesh is required")
    source = meshes[0]
    common._require(source.data.library is None,"Linked source geometry is unsupported")
    common._require(context.scene.unit_settings.system=="METRIC"
                    and abs(context.scene.unit_settings.scale_length-1)<1e-8,
                    "Canonical source must use METRIC scale_length=1")
    wind = common._source_wind_audit(context,source)
    arrays = common._mesh_arrays(source.data)
    attributes = common._attribute_cache(source.data)
    evaluated = common._evaluated_gate(context,source,arrays)
    common._require(common.NATIVE_ORDINAL in attributes and common.NATIVE_VERTEX in attributes,
                    "Stable native geometry ordinal/vertex identity is required")
    for name in (common.NATIVE_ORDINAL,common.NATIVE_VERTEX):
        common._require(attributes[name]["domain"]=="POINT","Native identity must be point-domain")
    size = len(source.data.vertices)
    parent = np.arange(size,dtype=np.int32)
    def find(i):
        while parent[i]!=i:
            parent[i]=parent[parent[i]]
            i=int(parent[i])
        return int(i)
    for a,b in arrays["edges"]:
        a,b=find(int(a)),find(int(b))
        if a!=b:
            parent[b]=a
    components = defaultdict(list)
    for i in range(size):
        components[find(i)].append(i)
    face_indexes = defaultdict(list)
    for face,(start,total) in enumerate(zip(arrays["loop_start"],arrays["loop_total"])):
        owners = {find(int(v)) for v in arrays["loop_vertices"][start:start+total]}
        common._require(len(owners)==1,"Polygon crosses connected source components")
        face_indexes[next(iter(owners))].append(face)
    matrix = np.array(source.matrix_world,dtype=np.float64)
    world = arrays["co"].astype(np.float64)@matrix[:3,:3].T+matrix[:3,3]
    ordinals = attributes[common.NATIVE_ORDINAL]["values"]
    native = attributes[common.NATIVE_VERTEX]["values"]
    bone_names = {bone.name for modifier in source.modifiers if modifier.type=="ARMATURE"
                  and modifier.object for bone in modifier.object.data.bones}
    vertex_group_names = {group.index:group.name for group in source.vertex_groups}
    parts = []
    for key,indexes in components.items():
        faces = face_indexes[key]
        common._require(bool(faces),"Isolated vertices without leaf faces are unsupported")
        pairs = sorted({(int(ordinals[i]),int(native[i])) for i in indexes})
        signature = hashlib.sha256(b"".join(struct.pack("<qq",*p) for p in pairs)).hexdigest()
        owners = {vertex_group_names[a.group] for i in indexes for a in source.data.vertices[i].groups
                  if a.weight>1e-5 and vertex_group_names[a.group] in bone_names
                  and vertex_group_names[a.group]!="Root"}
        common._require(len(owners)==1 and next(iter(owners)).endswith("_Start"),
                        "Every whole leaf must have exactly one non-root Start owner")
        low,high=world[indexes].min(axis=0),world[indexes].max(axis=0)
        parts.append({"native_vertex_set_sha256":signature,"owner_start":next(iter(owners)),
                      "vertex_count":len(indexes),"polygon_count":len(faces),
                      "triangle_count":sum(int(arrays["loop_total"][i])-2 for i in faces),
                      "world_aabb_min_m":low.tolist(),"world_aabb_max_m":high.tolist()})
    parts.sort(key=lambda row:row["native_vertex_set_sha256"])
    common._require(len({p["native_vertex_set_sha256"] for p in parts})==len(parts),
                    "Native whole-part identities are ambiguous")
    for index,part in enumerate(parts):
        part["part_index"]=index
        part["world_aabb_center_m"]=[(a+b)/2 for a,b in zip(part["world_aabb_min_m"],part["world_aabb_max_m"])]
    return {"asset_suffix":expected.stem,"source_file":str(expected),
            "source_sha256":common._file_sha256(expected),"part_count":len(parts),
            "vertices":size,"polygons":len(source.data.polygons),
            "triangles":sum(p["triangle_count"] for p in parts),"parts":parts,
            "source_aabb_min_m":world.min(axis=0).tolist(),
            "source_aabb_max_m":world.max(axis=0).tolist(),
            "wind_none_contract":wind,"source_wind_contract":wind,
            "activation_policy":"explicit_per_spm_selection","output_mode":"static_rest_pose",
            "evaluated_source_gate":evaluated}


def build_generic_terrain_groups(context, *, source_path, evidence_path, max_groups=20,
                                 target_radius_cm=30., target_height_span_cm=10.,
                                 collection_name="DebrisTerrainGroups", asset_base_name=None):
    # All read-only identity, Wind audit, modifier, native-part and adaptive budget
    # checks precede the common helper's first Blender-local mutation.
    row = extract_source_parts(context,source_path=source_path)
    row["recommended"]=adaptive_voronoi(row["parts"],max_groups=max_groups,
                                        target_radius_cm=target_radius_cm,
                                        target_height_span_cm=target_height_span_cm)
    output=Path(evidence_path).resolve()
    common._require(output!=Path(source_path).resolve(),"Evidence cannot overwrite the canonical source")
    output.parent.mkdir(parents=True,exist_ok=True)
    output.write_text(json.dumps({"schema_version":1,"kind":"bwr_adaptive_terrain_group_evidence",
                                 "sources":[row]},ensure_ascii=False,indent=2),encoding="utf-8")
    receipt=common.build_terrain_groups(context,evidence_path=str(output),
                                       source_asset_suffix=row["asset_suffix"],
                                       collection_name=collection_name,asset_base_name=asset_base_name,
                                       max_groups=max_groups,source_wind_policy="audit_only")
    receipt["adaptive_grouping"]={key:value for key,value in row["recommended"].items()
                                 if key not in {"labels","groups"}}
    receipt["group_evidence_path"]=str(output)
    receipt["source_part_order"]="Sorted stable native whole-component signatures"
    collection=bpy.data.collections.get(collection_name)
    collection["terrain_build_receipt"]=json.dumps(receipt,separators=(",",":"))
    return receipt
