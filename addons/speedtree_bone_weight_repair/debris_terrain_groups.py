"""Opt-in, source-preserving terrain groups for the BAT gateway.

This module owns Blender-local mutation only. It never saves a file or user
preferences, imports Send to Unreal, or edits the original Export hierarchy.
The separate export activation operation is intentionally memory-only.
"""
from __future__ import annotations

import hashlib
import json
import math
import pathlib
import struct
import time
from collections import defaultdict

import bpy
import numpy as np
from mathutils import Matrix, Vector


OWNER_TAG = "speedtree_debris_terrain_groups_v1"
NATIVE_ORDINAL = "speedtree_native_geometry_ordinal"
NATIVE_VERTEX = "speedtree_native_vertex_index"
POSITION_TOLERANCE_M = 2e-6
NORMAL_TOLERANCE = 2e-5
MAX_GROUPS = 256  # Storage safety bound; normal work budget remains 20.
INTERNAL_TOPOLOGY = {"position", ".edge_verts", ".corner_vert", ".corner_edge"}
ATTRIBUTE_FIELDS = {
    "FLOAT": ("value", 1, np.float32),
    "INT": ("value", 1, np.int32),
    "INT8": ("value", 1, np.int32),
    "BOOLEAN": ("value", 1, np.bool_),
    "FLOAT_VECTOR": ("vector", 3, np.float32),
    "FLOAT2": ("vector", 2, np.float32),
    "FLOAT_COLOR": ("color", 4, np.float32),
    # Read and write the quantized sRGB channel rather than round-tripping
    # byte colors through a linear color conversion.
    "BYTE_COLOR": ("color_srgb", 4, np.float32),
    "INT16_2D": ("value", 2, np.int32),
    "INT32_2D": ("value", 2, np.int32),
}


class TerrainGroupError(RuntimeError):
    pass


def _require(condition, message):
    if not condition:
        raise TerrainGroupError(message)


def _file_sha256(path):
    digest = hashlib.sha256()
    with pathlib.Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024*1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _dense(collection, field, width, dtype):
    values = np.empty(len(collection)*width, dtype=dtype)
    collection.foreach_get(field, values)
    return values.reshape((-1,width)) if width > 1 else values


def _attribute_descriptor(attribute):
    _require(attribute.data_type in ATTRIBUTE_FIELDS,
             f"Unsupported attribute type before mutation: {attribute.name}:{attribute.data_type}")
    field,width,dtype = ATTRIBUTE_FIELDS[attribute.data_type]
    if len(attribute.data):
        datum = attribute.data[0]
        if not hasattr(datum,field):
            # Blender exposes internal packed int-pair attributes differently
            # between minor versions. Use only a proven same-width RNA field.
            alternatives = [prop.identifier for prop in datum.bl_rna.properties
                            if prop.identifier != "rna_type" and
                            getattr(prop,"array_length",0) == width and
                            prop.identifier in ("value","vector")]
            _require(len(alternatives)==1,
                     f"Unresolved attribute RNA field: {attribute.name}:{attribute.data_type}")
            field = alternatives[0]
    return field,width,dtype


def _attribute_cache(mesh):
    result = {}
    for attribute in mesh.attributes:
        _require(attribute.domain in {"POINT","EDGE","FACE","CORNER"},
                 f"Unsupported mesh attribute domain: {attribute.name}:{attribute.domain}")
        field,width,dtype = _attribute_descriptor(attribute)
        result[attribute.name] = {"domain":attribute.domain,"data_type":attribute.data_type,
                                  "field":field,"width":width,"dtype":dtype,
                                  "values":_dense(attribute.data,field,width,dtype)}
    return result


def _mesh_arrays(mesh):
    return {
        "co":_dense(mesh.vertices,"co",3,np.float32),
        "edges":_dense(mesh.edges,"vertices",2,np.int32),
        "loop_vertices":_dense(mesh.loops,"vertex_index",1,np.int32),
        "loop_edges":_dense(mesh.loops,"edge_index",1,np.int32),
        "loop_start":_dense(mesh.polygons,"loop_start",1,np.int32),
        "loop_total":_dense(mesh.polygons,"loop_total",1,np.int32),
        "material_index":_dense(mesh.polygons,"material_index",1,np.int32),
        "use_smooth":_dense(mesh.polygons,"use_smooth",1,np.bool_),
        "corner_normals":_dense(mesh.corner_normals,"vector",3,np.float32),
    }


def _fingerprint(obj, arrays, attributes):
    digest = hashlib.sha256()
    digest.update(json.dumps([list(row) for row in obj.matrix_world],separators=(",",":")).encode())
    digest.update(json.dumps([material.name if material else None for material in obj.data.materials],separators=(",",":")).encode())
    for name,values in sorted(arrays.items()):
        digest.update(name.encode())
        digest.update(np.ascontiguousarray(values).tobytes())
    for name,row in sorted(attributes.items()):
        digest.update(json.dumps([name,row["domain"],row["data_type"],row["field"]]).encode())
        digest.update(np.ascontiguousarray(row["values"]).tobytes())
    return digest.hexdigest()


def _source_wind_audit(context, source):
    """Read source Wind provenance without requiring a preset or a sidecar.

    This snapshot is a preservation receipt, not an activation condition.
    Generic output is static rest pose; source Wind settings remain untouched.
    """
    settings = getattr(context.scene,"speedtree_bwr_settings",None)
    scene_preset = str(getattr(settings,"wind_preset","") or "").upper()
    scene_explicit = bool(settings and settings.is_property_set("wind_preset"))
    scene_values = {name:getattr(settings,name,None) for name in
                    ("wind_preset","dynamic_wind_flexibility","dynamic_wind_gust_attenuation",
                     "dynamic_wind_ground_cover","write_dynamic_wind_json")}
    names = {pathlib.Path(bpy.data.filepath).stem}
    for property_name in ("codex_source_fbx","codex_source_identity"):
        value = str(source.get(property_name,"") or "")
        if value:
            names.add(pathlib.Path(value).stem)
    candidates = []
    blend_directory = pathlib.Path(bpy.data.filepath).parent
    for name in sorted(names):
        path = blend_directory/"JSON"/f"{name}_dynamic_wind_import_from_megaplant_groups.json"
        if not path.is_file():
            continue
        raw = path.read_bytes()
        record = {"path":str(path),"sha256":hashlib.sha256(raw).hexdigest(),
                  "preset":None,"enabled":None}
        try:
            data = json.loads(raw.decode("utf-8-sig"))
            record["preset"] = (data.get("WindResponsePresetContract") or {}).get("Preset")
            record["enabled"] = data.get("bIsEnabled")
        except (UnicodeError,ValueError,AttributeError,TypeError) as error:
            record["parse_error"] = str(error)
        candidates.append(record)
    audit = {"scene_preset":scene_preset,"scene_explicit":scene_explicit,
             "scene_wind_values":scene_values,"saved_contracts":candidates,"audit_only":True}
    audit["snapshot_sha256"] = hashlib.sha256(json.dumps(audit,sort_keys=True,
                                     separators=(",",":"),ensure_ascii=False).encode()).hexdigest()
    return audit


def _wind_none_contract(context, source):
    """Historical disabled-NONE experiment admission; generic does not use it."""
    audit = _source_wind_audit(context,source)
    scene_explicit,scene_preset = audit["scene_explicit"],audit["scene_preset"]
    if scene_explicit:
        _require(scene_preset == "NONE", f"Scene explicitly selects wind {scene_preset}; NONE is required")
    candidates = audit["saved_contracts"]
    for record in candidates:
        _require(str(record["preset"] or "").upper()=="NONE" and record["enabled"] is False,
                 f"Saved dynamic-wind contract is not disabled NONE: {record['path']}")
    _require((scene_explicit and scene_preset == "NONE") or candidates,
             "No explicit scene Wind NONE or exact saved disabled-NONE dynamic-wind contract")
    return audit


def _evaluated_gate(context, source, raw):
    _require(not source.data.shape_keys, "Shape keys are unsupported for static terrain groups")
    _require(all(modifier.type == "ARMATURE" for modifier in source.modifiers),
             "Only an undeformed armature modifier is admitted")
    for modifier in source.modifiers:
        armature = modifier.object
        _require(armature is not None, "Armature modifier has no armature")
        _require(all(max(abs(bone.matrix_basis[r][c]-(1.0 if r==c else 0.0))
                         for r in range(4) for c in range(4)) < 1e-6
                     for bone in armature.pose.bones), "Armature pose is not identity")
    evaluated = source.evaluated_get(context.evaluated_depsgraph_get())
    mesh = evaluated.to_mesh(preserve_all_data_layers=True,depsgraph=context.evaluated_depsgraph_get())
    try:
        _require(len(mesh.vertices)==len(source.data.vertices) and len(mesh.polygons)==len(source.data.polygons)
                 and len(mesh.loops)==len(source.data.loops), "Evaluated source topology changed")
        evaluated_co = _dense(mesh.vertices,"co",3,np.float32)
        max_delta = float(np.linalg.norm(evaluated_co.astype(np.float64)-raw["co"],axis=1).max(initial=0))
        _require(max_delta <= POSITION_TOLERANCE_M, f"Evaluated source is deformed: {max_delta}m")
        for key,field,collection in (("loop_vertices","vertex_index",mesh.loops),
                                     ("loop_start","loop_start",mesh.polygons),
                                     ("loop_total","loop_total",mesh.polygons)):
            _require(np.array_equal(raw[key],_dense(collection,field,1,np.int32)),
                     f"Evaluated source topology order changed: {key}")
        return {"topology_identical":True,"maximum_coordinate_delta_m":max_delta,
                "identity_armature_pose":True,"armature_modifiers_only":True}
    finally:
        evaluated.to_mesh_clear()


def _component_partition(source, arrays, attributes, evidence_source):
    n = len(source.data.vertices)
    parent = np.arange(n,dtype=np.int32)
    def root(index):
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = int(parent[index])
        return int(index)
    for a,b in arrays["edges"]:
        left,right = root(int(a)),root(int(b))
        if left != right:
            parent[right] = left
    components = defaultdict(list)
    for i in range(n):
        components[root(i)].append(i)
    components = sorted(components.values(),key=lambda indexes:indexes[0])
    vertex_part = np.empty(n,dtype=np.int32)
    for i,indexes in enumerate(components):
        vertex_part[indexes] = i
    face_part = np.empty(len(source.data.polygons),dtype=np.int32)
    component_faces = defaultdict(list)
    for face,(start,total) in enumerate(zip(arrays["loop_start"],arrays["loop_total"])):
        labels = set(int(v) for v in vertex_part[arrays["loop_vertices"][start:start+total]])
        _require(len(labels)==1,f"Polygon crosses loose components: {face}")
        label = next(iter(labels))
        face_part[face] = label
        component_faces[label].append(face)
    _require(NATIVE_ORDINAL in attributes and NATIVE_VERTEX in attributes,
             "Exact native point identity is absent")
    ordinals = attributes[NATIVE_ORDINAL]["values"]
    native_vertices = attributes[NATIVE_VERTEX]["values"]
    matrix = np.array(source.matrix_world,dtype=np.float64)
    world = arrays["co"].astype(np.float64)@matrix[:3,:3].T + matrix[:3,3]
    expected_parts = {p["native_vertex_set_sha256"]:p for p in evidence_source["parts"]}
    _require(len(expected_parts)==len(components)==evidence_source["part_count"],
             "Loose-component/native-signature count differs from evidence")
    group_by_expected_part = evidence_source["recommended"]["labels"]
    group_by_vertex = np.empty(n,dtype=np.int32)
    matched = []
    supports = []
    armature_names = {bone.name for modifier in source.modifiers if modifier.type=="ARMATURE"
                      for bone in modifier.object.data.bones}
    group_names = {group.index:group.name for group in source.vertex_groups}
    for current_part,indexes in enumerate(components):
        pairs = sorted({(int(ordinals[i]),int(native_vertices[i])) for i in indexes})
        signature = hashlib.sha256(b"".join(struct.pack("<qq",*pair) for pair in pairs)).hexdigest()
        _require(signature in expected_parts,f"Unrecognized native loose-component signature {signature}")
        expected = expected_parts.pop(signature)
        face_indexes = component_faces[current_part]
        triangle_count = int(sum(int(arrays["loop_total"][i])-2 for i in face_indexes))
        _require((len(indexes),len(face_indexes),triangle_count)==
                 (expected["vertex_count"],expected["polygon_count"],expected["triangle_count"]),
                 f"Component geometry counts changed: {expected['part_index']}")
        owners = set()
        for i in indexes:
            owners.update(group_names[assignment.group] for assignment in source.data.vertices[i].groups
                          if assignment.weight > 1e-5 and group_names[assignment.group] in armature_names
                          and group_names[assignment.group] != "Root")
        _require(owners == {expected["owner_start"]},f"Non-root Start owner changed: {expected['part_index']}")
        points = world[indexes]
        low,high = points.min(axis=0),points.max(axis=0)
        _require(np.max(np.abs(low-np.array(expected["world_aabb_min_m"]))) <= POSITION_TOLERANCE_M
                 and np.max(np.abs(high-np.array(expected["world_aabb_max_m"]))) <= POSITION_TOLERANCE_M,
                 f"Component world coordinate bounds changed: {expected['part_index']}")
        group_id = int(group_by_expected_part[expected["part_index"]])
        group_by_vertex[indexes] = group_id
        matched.append({"part_index":expected["part_index"],"group_id":group_id,"native_signature":signature,
                        "vertices":len(indexes),"polygons":len(face_indexes),"triangles":triangle_count})
        # Exact existing mesh vertices, capped at five unique samples per leaf.
        selected = {}
        for kind,axis,maximize in (("min_z",2,False),("max_x",0,True),("min_x",0,False),
                                  ("max_y",1,True),("min_y",1,False)):
            local = int(np.argmax(points[:,axis]) if maximize else np.argmin(points[:,axis]))
            vertex_index = int(indexes[local])
            selected.setdefault(vertex_index,[]).append(kind)
        for vertex_index,kinds in selected.items():
            supports.append({"part_index":expected["part_index"],"group_id":group_id,
                             "source_vertex_index":vertex_index,"source_world_m":world[vertex_index].tolist(),
                             "kind":kinds[0],"kinds":kinds})
    _require(not expected_parts,"Some evidence components have no current geometry")
    _require(len(supports)<=5*len(components),"Support sample cap exceeded")
    group_by_face = np.array([group_by_vertex[arrays["loop_vertices"][int(start)]]
                              for start in arrays["loop_start"]],dtype=np.int32)
    return group_by_vertex,group_by_face,matched,supports,world


def _copy_attributes(source, target, cache, domain_indexes):
    # UV order and active/render selection are part of the export contract.
    for layer in source.uv_layers:
        new = target.uv_layers.new(name=layer.name)
        new.active_render = layer.active_render
    if source.uv_layers:
        target.uv_layers.active_index = source.uv_layers.active_index
    for color in source.color_attributes:
        if target.color_attributes.get(color.name) is None:
            target.color_attributes.new(name=color.name,type=color.data_type,domain=color.domain)
    if source.color_attributes:
        target.color_attributes.active_color_index = source.color_attributes.active_color_index
        target.color_attributes.render_color_index = source.color_attributes.render_color_index
    # Create the packed custom-normal storage through Blender's public API.
    target.normals_split_custom_set(cache["__corner_normals"][domain_indexes["CORNER"]].tolist())
    hashes = {}
    for name,row in cache.items():
        if name in INTERNAL_TOPOLOGY or name == "__corner_normals":
            continue
        selected = np.ascontiguousarray(row["values"][domain_indexes[row["domain"]]])
        attribute = target.attributes.get(name)
        if name in {".select_vert",".select_edge",".select_poly"}:
            collection = {"POINT":target.vertices,"EDGE":target.edges,"FACE":target.polygons}[row["domain"]]
            collection.foreach_set("select",selected.ravel())
            attribute = target.attributes.get(name)
        else:
            if attribute is None:
                attribute = target.attributes.new(name=name,type=row["data_type"],domain=row["domain"])
            _require(attribute.domain==row["domain"] and attribute.data_type==row["data_type"],
                     f"Target attribute schema changed: {name}")
            attribute.data.foreach_set(row["field"],selected.ravel())
        _require(attribute is not None,f"Attribute copy produced no storage: {name}")
        actual = _dense(attribute.data,row["field"],row["width"],row["dtype"])
        _require(np.array_equal(actual,selected),f"Attribute subset differs after copy: {name}")
        hashes[name] = hashlib.sha256(selected.tobytes()).hexdigest()
    target.update()
    return hashes


def _owned_objects(collection):
    return [obj for obj in collection.all_objects if obj.get(OWNER_TAG) is True]


def _normal_rebase_diagnostic(source, mesh, arrays, attributes, loop_indexes):
    """Measure packing and tiny-face frame drift before choosing exact storage.

    All temporary data belongs to this operation. No source datablock changes.
    The unrecentered strategy retains original float32 vertex bytes and packed
    normal bytes; the group pivot is represented by parent/child transforms.
    """
    expected = arrays["corner_normals"][loop_indexes]
    current = _dense(mesh.corner_normals,"vector",3,np.float32)
    delta = np.linalg.norm(current.astype(np.float64)-expected,axis=1)
    worst_loop = int(np.argmax(delta))
    source_loop = int(loop_indexes[worst_loop])
    source_face = int(np.searchsorted(arrays["loop_start"],source_loop,side="right")-1)
    start,total = int(arrays["loop_start"][source_face]),int(arrays["loop_total"][source_face])
    face_points = arrays["co"][arrays["loop_vertices"][start:start+total]].astype(np.float64)
    area = sum(np.linalg.norm(np.cross(face_points[i]-face_points[0],face_points[i+1]-face_points[0]))/2
               for i in range(1,total-1))
    lengths = [float(np.linalg.norm(face_points[(i+1)%total]-face_points[i])) for i in range(total)]
    copy = source.data.copy()
    try:
        baseline = _dense(copy.corner_normals,"vector",3,np.float32)
        baseline_delta = float(np.linalg.norm(baseline.astype(np.float64)-arrays["corner_normals"],axis=1).max(initial=0))
        copy.normals_split_custom_set(arrays["corner_normals"].tolist())
        copy.update()
        roundtrip = _dense(copy.corner_normals,"vector",3,np.float32)
        roundtrip_delta = np.linalg.norm(roundtrip.astype(np.float64)-arrays["corner_normals"],axis=1)
        packed = attributes.get("custom_normal")
        if packed:
            copy.attributes["custom_normal"].data.foreach_set(packed["field"],packed["values"].ravel())
            copy.update()
        restored = _dense(copy.corner_normals,"vector",3,np.float32)
        restore_delta = float(np.linalg.norm(restored.astype(np.float64)-arrays["corner_normals"],axis=1).max(initial=0))
    finally:
        bpy.data.meshes.remove(copy)
    return {"recentered_group_raw_packed_normal_max_delta":float(delta.max(initial=0)),
            "recentered_group_raw_packed_normal_max_angle_deg":math.degrees(2*math.asin(min(1,float(delta.max(initial=0))/2))),
            "source_copy_original_packed_normal_max_delta":baseline_delta,
            "source_copy_vector_setter_roundtrip_max_delta":float(roundtrip_delta.max(initial=0)),
            "source_copy_vector_setter_roundtrip_p95_delta":float(np.quantile(roundtrip_delta,.95)),
            "source_copy_restored_original_packed_normal_max_delta":restore_delta,
            "most_affected_source_face_index":source_face,"most_affected_face_area_m2":float(area),
            "most_affected_face_edge_length_m":lengths}


def build_terrain_groups(context, *, evidence_path, source_asset_suffix,
                         collection_name="DebrisTerrainGroups", asset_base_name=None,
                         max_groups=20, source_wind_policy="legacy_disabled_none"):
    started = time.perf_counter()
    _require(isinstance(max_groups,int) and not isinstance(max_groups,bool)
             and 1<=max_groups<=MAX_GROUPS,"Requested group budget must be in 1..256")
    _require(source_wind_policy in {"legacy_disabled_none","audit_only"},
             "Unknown source Wind audit policy")
    evidence_path = pathlib.Path(evidence_path).resolve(strict=True)
    evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
    rows = [row for row in evidence["sources"] if row["asset_suffix"]==str(source_asset_suffix)]
    _require(len(rows)==1,"Exactly one requested source must exist in the evidence")
    row = rows[0]
    export = bpy.data.collections.get("Export")
    _require(export is not None,"Original Export collection is absent")
    candidates = [obj for obj in export.all_objects if obj.type=="MESH" and len(obj.data.vertices)
                  and obj.get(OWNER_TAG) is not True]
    _require(len(candidates)==1,"Exactly one original Export mesh is required")
    source = candidates[0]
    _require(context.scene.unit_settings.system=="METRIC" and abs(context.scene.unit_settings.scale_length-1)<1e-8,
             "Evidence requires METRIC scale_length=1")
    _require(len(source.data.vertices)==row["vertices"] and len(source.data.polygons)==row["polygons"],
             "Original source counts differ from evidence")
    _require(source.data.library is None,"Linked-library source is unsupported")
    wind = (_source_wind_audit(context,source) if source_wind_policy=="audit_only"
            else _wind_none_contract(context,source))
    arrays = _mesh_arrays(source.data)
    attributes = _attribute_cache(source.data)
    evaluated_gate = _evaluated_gate(context,source,arrays)
    vertex_groups,face_groups,members,supports,source_world = _component_partition(source,arrays,attributes,row)
    recommended = row["recommended"]["groups"]
    group_ids = sorted(set(int(g["group_id"]) for g in recommended))
    _require(group_ids==list(range(len(group_ids))) and 1<=len(group_ids)<=max_groups,
             "Recommended groups violate the requested group budget")
    _require(set(vertex_groups.tolist())==set(group_ids),"Current geometry has empty or additional groups")
    material_names = [material.name if material else None for material in source.data.materials]
    _require(all(source.data.materials[int(i)] is not None for i in set(arrays["material_index"].tolist())),
             "Face-assigned empty materials are not exportable")
    before = _fingerprint(source,arrays,attributes)
    source_file = pathlib.Path(bpy.data.filepath).resolve()
    baseline_file_hash = _file_sha256(source_file) if source_file.is_file() else None
    existing = bpy.data.collections.get(collection_name)
    _require(existing is None or all(obj.get(OWNER_TAG) is True for obj in existing.all_objects),
             "Target collection contains non-owned data; refusing replacement")
    old_objects = _owned_objects(existing) if existing else []
    _require(not any(obj.name in export.all_objects for obj in old_objects),
             "Previously generated groups are active in Export; build before activation")
    if asset_base_name is None:
        stem = pathlib.Path(row["source_file"]).stem
        stem = stem.removeprefix("SK_").removeprefix("SM_")
        asset_base_name = "SM_"+stem+"_Terrain"
    _require(str(asset_base_name).startswith("SM_") and all(c.isalnum() or c=="_" for c in str(asset_base_name)),
             "Static asset base must be an SM_ name using letters, digits or underscores")
    # Send2UE CHILD_MESHES uses the Empty parent's name as the static asset
    # name. Keep that final name on the root, with an explicit mesh suffix on
    # the child; otherwise the exported asset accidentally ends in _Root.
    final_names = [(f"{asset_base_name}_G{group_id+1:02d}_Mesh",f"{asset_base_name}_G{group_id+1:02d}")
                   for group_id in group_ids]
    old_set = set(old_objects)
    _require(all(bpy.data.objects.get(name) is None or bpy.data.objects.get(name) in old_set
                 for pair in final_names for name in pair),"Generated object names collide with non-owned data")
    staging = bpy.data.collections.new(collection_name+"__staging")
    context.scene.collection.children.link(staging)
    created = []
    records = []
    hashes = {}
    normal_rebase_diagnostic = None
    attributes_with_normals = dict(attributes)
    attributes_with_normals["__corner_normals"] = arrays["corner_normals"]
    try:
        for group_id in group_ids:
            group = recommended[group_id]
            vertex_indexes = np.flatnonzero(vertex_groups==group_id)
            face_indexes = np.flatnonzero(face_groups==group_id)
            index_remap = np.full(len(source.data.vertices),-1,dtype=np.int32)
            index_remap[vertex_indexes] = np.arange(len(vertex_indexes),dtype=np.int32)
            edge_mask = (vertex_groups[arrays["edges"][:,0]]==group_id)
            _require(np.all(vertex_groups[arrays["edges"][edge_mask,1]]==group_id),"Edge crosses requested groups")
            edge_indexes = np.flatnonzero(edge_mask)
            loop_indexes = np.concatenate([np.arange(arrays["loop_start"][i],
                                arrays["loop_start"][i]+arrays["loop_total"][i],dtype=np.int32) for i in face_indexes])
            pivot_world = Vector(group["mec_pivot_world_m"])
            pivot_local = source.matrix_world.inverted()@pivot_world
            vertices = arrays["co"][vertex_indexes].astype(np.float64)-np.array(pivot_local,dtype=np.float64)
            edges = index_remap[arrays["edges"][edge_indexes]].tolist()
            faces = [index_remap[arrays["loop_vertices"][int(arrays["loop_start"][i]):
                       int(arrays["loop_start"][i]+arrays["loop_total"][i])]].tolist() for i in face_indexes]
            mesh_name,root_name = final_names[group_id]
            mesh = bpy.data.meshes.new(mesh_name+"__staging")
            root = bpy.data.objects.new(root_name+"__staging",None)
            child = bpy.data.objects.new(mesh_name+"__staging",mesh)
            created.extend((root,child))
            for obj in (root,child):
                obj[OWNER_TAG] = True
                obj["terrain_group_id"] = group_id
                obj["terrain_source_sha256"] = row["source_sha256"]
                obj["terrain_evidence_sha256"] = _file_sha256(evidence_path)
                staging.objects.link(obj)
            root.empty_display_type = "PLAIN_AXES"
            root.matrix_world = Matrix.Translation(pivot_world)
            child.parent = root
            child.matrix_parent_inverse = Matrix.Identity(4)
            child.matrix_world = source.matrix_world@Matrix.Translation(pivot_local)
            mesh.from_pydata(vertices.tolist(),edges,faces,shade_flat=False)
            mesh.update()
            _require(len(mesh.vertices)==len(vertex_indexes) and len(mesh.polygons)==len(face_indexes)
                     and len(mesh.loops)==len(loop_indexes),"Direct geometry construction changed counts")
            _require(np.array_equal(_dense(mesh.loops,"vertex_index",1,np.int32),index_remap[arrays["loop_vertices"][loop_indexes]]),
                     "Loop order differs from source face order")
            # Blender may reorder its EDGE domain while calculating topology.
            # Recover an exact edge identity map before copying edge attributes.
            source_edge_lookup = {tuple(sorted(map(int,index_remap[edge]))):int(i)
                                  for i,edge in zip(edge_indexes,arrays["edges"][edge_indexes])}
            output_edges = _dense(mesh.edges,"vertices",2,np.int32)
            mapped_edges = np.array([source_edge_lookup[tuple(sorted(map(int,edge)))] for edge in output_edges],dtype=np.int32)
            _require(len(mapped_edges)==len(edge_indexes) and len(set(mapped_edges.tolist()))==len(edge_indexes),
                     "Edge correspondence is not bijective")
            for material in source.data.materials:
                mesh.materials.append(material)
            mesh.polygons.foreach_set("material_index",arrays["material_index"][face_indexes])
            mesh.polygons.foreach_set("use_smooth",arrays["use_smooth"][face_indexes])
            indexes = {"POINT":vertex_indexes,"EDGE":mapped_edges,"FACE":face_indexes,"CORNER":loop_indexes}
            channel_hashes = _copy_attributes(source.data,mesh,attributes_with_normals,indexes)
            actual_normals = _dense(mesh.corner_normals,"vector",3,np.float32)
            expected_normals = arrays["corner_normals"][loop_indexes]
            normal_delta = float(np.linalg.norm(actual_normals.astype(np.float64)-expected_normals,axis=1).max(initial=0))
            if normal_rebase_diagnostic is None:
                normal_rebase_diagnostic = _normal_rebase_diagnostic(source,mesh,arrays,attributes,loop_indexes)
            # Retain source-local float32 coordinates and packed normal frames.
            # Empty-parent position defines the exported asset pivot; child
            # matrix offset carries the compensation without vertex rounding.
            mesh.vertices.foreach_set("co",np.ascontiguousarray(arrays["co"][vertex_indexes]).ravel())
            child.matrix_world = source.matrix_world.copy()
            mesh.update()
            _require(np.array_equal(_dense(mesh.vertices,"co",3,np.float32),arrays["co"][vertex_indexes]),
                     "Original float32 coordinate bytes were not preserved")
            actual_normals = _dense(mesh.corner_normals,"vector",3,np.float32)
            normal_delta = float(np.linalg.norm(actual_normals.astype(np.float64)-expected_normals,axis=1).max(initial=0))
            _require(normal_delta<=NORMAL_TOLERANCE,f"Group corner-normal parity failed: {normal_delta}")
            actual_co = _dense(mesh.vertices,"co",3,np.float32).astype(np.float64)
            world_matrix = np.array(child.matrix_world,dtype=np.float64)
            reconstructed = actual_co@world_matrix[:3,:3].T+world_matrix[:3,3]
            coordinate_delta = float(np.linalg.norm(reconstructed-source_world[vertex_indexes],axis=1).max(initial=0))
            _require(coordinate_delta<=POSITION_TOLERANCE_M,f"Group world-geometry parity failed: {coordinate_delta}m")
            triangle_count = int(sum(int(arrays["loop_total"][i])-2 for i in face_indexes))
            records.append({"group_id":group_id,"root_object":root_name,"mesh_object":mesh_name,
                            "pivot_world_m":list(pivot_world),"vertices":len(vertex_indexes),
                            "polygons":len(face_indexes),"triangles":triangle_count,
                            "part_indexes":[m["part_index"] for m in members if m["group_id"]==group_id],
                            "maximum_world_coordinate_delta_m":coordinate_delta,
                            "maximum_corner_normal_delta":normal_delta,
                            "attribute_subset_sha256":channel_hashes,
                            "material_slots":material_names})
        _require(sum(g["vertices"] for g in records)==row["vertices"] and
                 sum(g["polygons"] for g in records)==row["polygons"] and
                 sum(g["triangles"] for g in records)==row["triangles"],"Total geometry was not conserved")
        after = _fingerprint(source,_mesh_arrays(source.data),_attribute_cache(source.data))
        wind_after = _source_wind_audit(context,source)
        _require(wind_after["snapshot_sha256"]==wind["snapshot_sha256"],
                 "Terrain grouping changed source scene or saved Wind data")
        _require(before==after,"Original source datablock changed")
        _require(all(obj.type!="ARMATURE" and not any(modifier.type=="ARMATURE" for modifier in obj.modifiers)
                     for obj in created),"Generated static groups contain an armature")
        # Commit only after all staged outputs and the immutable source pass.
        if existing:
            for obj in old_objects:
                data = obj.data
                bpy.data.objects.remove(obj,do_unlink=True)
                if data is not None and data.users==0 and isinstance(data,bpy.types.Mesh):
                    bpy.data.meshes.remove(data)
            for obj in list(staging.objects):
                existing.objects.link(obj)
                staging.objects.unlink(obj)
            bpy.data.collections.remove(staging)
            collection = existing
        else:
            staging.name = collection_name
            collection = staging
        for group_id,(mesh_name,root_name) in enumerate(final_names):
            root,child = created[2*group_id:2*group_id+2]
            root.name,child.name = root_name,mesh_name
            child.data.name = mesh_name+"Mesh"
        collection[OWNER_TAG] = True
        collection["terrain_source_fingerprint"] = before
        receipt = {"schema_version":1,"status":"built","collection":collection.name,
                   "source_asset_suffix":str(source_asset_suffix),"group_count":len(records),
                   "source":{"object":source.name,"file":str(source_file),"baseline_file_sha256":baseline_file_hash,
                              "evidence_original_sha256":row["source_sha256"],"fingerprint_before_after":[before,after],
                              "vertices":row["vertices"],"polygons":row["polygons"],"triangles":row["triangles"],
                              "bounds_world_m":[source_world.min(axis=0).tolist(),source_world.max(axis=0).tolist()]},
                   "groups":records,"members":members,"support_samples":supports,
                   "support_contract":"Actual original min-Z/XY-extreme vertices, at most 5 unique vertices per original component; no contact-success claim.",
                   "source_global_min_z_m":float(source_world[:,2].min()),
                   "wind_none_contract":wind,"source_wind_contract":wind,
                   "source_wind_snapshot_before_after":[wind["snapshot_sha256"],wind_after["snapshot_sha256"]],
                   "source_wind_policy":source_wind_policy,"output_mode":"static_rest_pose",
                   "evaluated_source_gate":evaluated_gate,
                   "normal_storage_contract":"Exact original float32 vertex coordinates and original packed normals; MEC pivot via parent/child matrix offset, no vertex recentering.",
                   "normal_rebase_diagnostic":normal_rebase_diagnostic,
                   "checks":{"exact_native_component_match":True,"whole_components_preserved":True,
                             "all_named_attributes_subset_equal":True,"material_slots_preserved":True,
                             "uv_layers_preserved":True,"world_geometry_preserved":True,
                             "corner_normals_preserved":True,"geometry_counts_conserved":True,
                              "source_datablock_unchanged":True,"original_export_unchanged":True,
                              "source_wind_data_unchanged":True,
                             "maximum_20_groups":len(records)<=20,
                             "requested_budget_respected":len(records)<=max_groups,
                             "maximum_group_safety_respected":len(records)<=MAX_GROUPS,
                             "no_generated_armatures":True},
                   "elapsed_seconds":time.perf_counter()-started}
        collection["terrain_build_receipt"] = json.dumps(receipt,separators=(",",":"))
        return receipt
    except Exception:
        for obj in reversed(created):
            if obj.name in bpy.data.objects:
                data = obj.data
                bpy.data.objects.remove(obj,do_unlink=True)
                if data is not None and data.users==0 and isinstance(data,bpy.types.Mesh):
                    bpy.data.meshes.remove(data)
        if staging.name in bpy.data.collections:
            bpy.data.collections.remove(staging)
        raise


def activate_terrain_group_export(context, *, collection_name="DebrisTerrainGroups"):
    """Expose only the requested proven static groups to Send to Unreal in RAM.

    The worker saves the non-Export production state before this operation.
    Original objects remain alive and linked to their original non-Export
    collections; objects owned only by Export receive a retention collection.
    """
    collection = bpy.data.collections.get(collection_name)
    export = bpy.data.collections.get("Export")
    _require(collection is not None and collection.get(OWNER_TAG) is True,
             "Requested generated collection is not owned by this operation")
    _require(export is not None,"Export collection is absent")
    receipt = json.loads(str(collection.get("terrain_build_receipt") or "{}"))
    _require(receipt.get("status")=="built" and 1<=receipt.get("group_count",0)<=MAX_GROUPS,
             "Generated collection has no validated build receipt")
    groups = receipt["groups"]
    requested = {bpy.data.objects.get(name) for group in groups for name in (group["root_object"],group["mesh_object"])}
    _require(None not in requested and set(collection.all_objects)==requested,
             "Generated collection membership differs from its build receipt")
    _require(all(obj.get(OWNER_TAG) is True and obj.type in {"EMPTY","MESH"} for obj in requested),
             "Unowned or skeletal object in static group collection")
    source_members = list(export.all_objects)
    _require(not any(obj.get(OWNER_TAG) is True and obj not in requested for obj in source_members),
             "Export contains generated groups from another unrequested collection")
    retention = bpy.data.collections.get("DebrisTerrainGroups_SourceRetention")
    if retention is not None:
        _require(retention.get(OWNER_TAG) is True,"Source-retention collection name collides with unowned data")
    direct_objects = list(export.objects)
    direct_children = list(export.children)
    retained = []
    removed_objects = []
    removed_children = []
    linked_objects = []
    linked = False
    try:
        for obj in direct_objects:
            if obj not in requested:
                if len(obj.users_collection)==1:
                    if retention is None:
                        retention = bpy.data.collections.new("DebrisTerrainGroups_SourceRetention")
                        retention[OWNER_TAG] = True
                        context.scene.collection.children.link(retention)
                    retention.objects.link(obj)
                    retained.append(obj)
                export.objects.unlink(obj)
                removed_objects.append(obj)
        for child in direct_children:
            if child != collection:
                # Unlink only the edge owned by Export. Nested collection contents
                # and all other collection links remain untouched.
                export.children.unlink(child)
                removed_children.append(child)
                if child.name not in context.scene.collection.children:
                    context.scene.collection.children.link(child)
        if collection.name not in export.children:
            export.children.link(collection)
            linked = True
        # Current Send2UE treats Export as explicit activation: nested
        # membership alone is deliberately excluded by get_from_collection.
        for obj in requested:
            if obj.name not in export.objects:
                export.objects.link(obj)
                linked_objects.append(obj)
        context.view_layer.update()
        _require(set(export.all_objects)==requested,"Memory-only Export activation includes unrelated data")
        _require(all(obj.type!="ARMATURE" for obj in export.all_objects),"Export activation contains an armature")
        return {"schema_version":1,"status":"activated","export_collection":export.name,
                "group_count":len(groups),"groups":groups,
                "source_unlinked_from_export":[obj.name for obj in source_members if obj not in requested],
                "retained_original_objects":[obj.name for obj in retained],"memory_only":True,
                "source_datablocks_deleted":False}
    except Exception:
        for obj in linked_objects:
            if obj.name in export.objects:
                export.objects.unlink(obj)
        if linked:
            export.children.unlink(collection)
        for child in removed_children:
            export.children.link(child)
        for obj in removed_objects:
            export.objects.link(obj)
        for obj in retained:
            if retention and obj.name in retention.objects:
                retention.objects.unlink(obj)
        raise


def activate_terrain_control_export(context, *, collection_name="DebrisTerrainGroups",
                                    asset_name="SM_weed_deadleaves_10_Terrain_Control"):
    """Activate a fair single-mesh control from the proven current source in RAM.

    Existing deployed single meshes may contain additional historical geometry.
    Clone the receipt's unchanged original source, keeping its exact raw mesh,
    attributes and corner normals. Only the clone loses its undeformed armature
    modifier and vertex groups. No file or preferences are saved. The identity
    Empty makes CHILD_MESHES use the original source-world asset origin (zero).
    """
    started = time.perf_counter()
    groups_collection = bpy.data.collections.get(collection_name)
    export = bpy.data.collections.get("Export")
    _require(groups_collection is not None and groups_collection.get(OWNER_TAG) is True,
             "Control requires an owned validated terrain-group collection")
    _require(export is not None,"Control requires the original Export collection")
    receipt = json.loads(str(groups_collection.get("terrain_build_receipt") or "{}"))
    _require(receipt.get("status")=="built" and 1<=receipt.get("group_count",0)<=MAX_GROUPS,
             "Control requires a validated terrain-group build receipt")
    source_receipt = receipt.get("source") or {}
    source = bpy.data.objects.get(source_receipt.get("object",""))
    _require(source is not None and source.type=="MESH" and source.data.library is None,
             "Validated original source mesh is missing or linked")
    _require(source.name in export.all_objects,"Validated original source is not active in Export")
    _require(not any(obj.get(OWNER_TAG) is True for obj in export.all_objects),
             "Build the control before activating any generated Export objects")
    _require(context.scene.unit_settings.system=="METRIC"
             and abs(context.scene.unit_settings.scale_length-1.0)<1e-9,
             "Control requires the validated metric metre scene")
    _require(str(asset_name).startswith("SM_")
             and all(c.isalnum() or c=="_" for c in str(asset_name)),
             "Control asset must be an SM_ name using letters, digits or underscores")
    control_name = collection_name+"_ControlExport"
    _require(bpy.data.collections.get(control_name) is None,
             "A control Export collection already exists; refusing to replace memory data")
    mesh_name = str(asset_name)+"_Mesh"
    _require(bpy.data.objects.get(str(asset_name)) is None and bpy.data.objects.get(mesh_name) is None,
             "Control object names collide with existing data")
    wind = _wind_none_contract(context,source)
    arrays = _mesh_arrays(source.data)
    attributes = _attribute_cache(source.data)
    before = _fingerprint(source,arrays,attributes)
    fingerprints = source_receipt.get("fingerprint_before_after") or []
    _require(len(fingerprints)==2 and fingerprints[0]==fingerprints[1]==before,
             "Current original source differs from the validated build receipt")
    triangles = int(np.sum(arrays["loop_total"].astype(np.int64)-2))
    _require((len(source.data.vertices),len(source.data.polygons),triangles)==
             (source_receipt["vertices"],source_receipt["polygons"],source_receipt["triangles"]),
             "Control source geometry counts differ from the build receipt")
    evaluated_gate = _evaluated_gate(context,source,arrays)
    original_direct_objects = list(export.objects)
    original_direct_children = list(export.children)
    original_scene_children = set(context.scene.collection.children)
    original_retention = bpy.data.collections.get("DebrisTerrainGroups_SourceRetention")
    original_retention_objects = set(original_retention.objects) if original_retention else set()
    control = bpy.data.collections.new(control_name)
    context.scene.collection.children.link(control)
    control[OWNER_TAG] = True
    root = None
    child = None
    clone_mesh = None
    try:
        root = bpy.data.objects.new(str(asset_name),None)
        clone_mesh = source.data.copy()
        clone_mesh.name = mesh_name+"Mesh"
        child = source.copy()
        child.data = clone_mesh
        child.name = mesh_name
        child.animation_data_clear()
        child.constraints.clear()
        for modifier in list(child.modifiers):
            _require(modifier.type=="ARMATURE","Unexpected control clone modifier")
            child.modifiers.remove(modifier)
        child.vertex_groups.clear()
        root.empty_display_type = "PLAIN_AXES"
        root.matrix_world = Matrix.Identity(4)
        child.parent = root
        child.matrix_parent_inverse = Matrix.Identity(4)
        child.matrix_world = source.matrix_world.copy()
        for obj in (root,child):
            obj[OWNER_TAG] = True
            obj["terrain_group_id"] = 0
            obj["terrain_control_only"] = True
            control.objects.link(obj)
        context.view_layer.update()
        clone_arrays = _mesh_arrays(clone_mesh)
        clone_attributes = _attribute_cache(clone_mesh)
        clone_fingerprint = _fingerprint(child,clone_arrays,clone_attributes)
        _require(clone_fingerprint==before,
                 "Control clone does not preserve exact source geometry, attributes, materials and normals")
        _require(not child.modifiers and not child.vertex_groups and child.type=="MESH",
                 "Control clone retained rigging")
        after = _fingerprint(source,_mesh_arrays(source.data),_attribute_cache(source.data))
        _require(before==after,"Control construction changed the original source")
        record = {"group_id":0,"root_object":root.name,"mesh_object":child.name,
                  "pivot_world_m":[0.0,0.0,0.0],"vertices":source_receipt["vertices"],
                  "polygons":source_receipt["polygons"],"triangles":triangles,
                  "part_indexes":[member["part_index"] for member in receipt.get("members",[])],
                  "maximum_world_coordinate_delta_m":0.0,"maximum_corner_normal_delta":0.0,
                  "material_slots":[material.name if material else None for material in clone_mesh.materials]}
        control_receipt = {"schema_version":1,"status":"built","collection":control.name,
                           "group_count":1,"groups":[record],"source":source_receipt,
                           "control_only":True,"source_fingerprint_before_after":[before,after],
                           "clone_fingerprint":clone_fingerprint,"wind_none_contract":wind,
                           "evaluated_source_gate":evaluated_gate,"memory_only":True}
        control["terrain_build_receipt"] = json.dumps(control_receipt,separators=(",",":"))
        activated = activate_terrain_group_export(context,collection_name=control.name)
        final = _fingerprint(source,_mesh_arrays(source.data),_attribute_cache(source.data))
        _require(final==before,"Control activation changed the original source")
        activated.update(control_only=True,source_collection=collection_name,
                         control_collection=control.name,asset_name=str(asset_name),
                         source_fingerprint_before_after=[before,final],clone_fingerprint=clone_fingerprint,
                         source_geometry_preserved=True,clone_geometry_preserved=True,
                         elapsed_seconds=time.perf_counter()-started)
        return activated
    except Exception:
        # Generic activation rolls itself back on failure. Also restore the
        # initial links if a later immutable-source postcondition failed.
        for obj in (child,root):
            if obj is not None and obj.name in export.objects:
                export.objects.unlink(obj)
        if control.name in export.children:
            export.children.unlink(control)
        for obj in original_direct_objects:
            if obj.name not in export.objects:
                export.objects.link(obj)
        for collection in original_direct_children:
            if collection.name not in export.children:
                export.children.link(collection)
            if collection not in original_scene_children and collection.name in context.scene.collection.children:
                context.scene.collection.children.unlink(collection)
        retention = bpy.data.collections.get("DebrisTerrainGroups_SourceRetention")
        if retention and retention.get(OWNER_TAG) is True:
            for obj in list(retention.objects):
                if obj not in original_retention_objects:
                    retention.objects.unlink(obj)
            if original_retention is None and not retention.objects and not retention.children:
                bpy.data.collections.remove(retention)
        # Exclusively operation-owned clone/Empty/collection datablocks.
        for obj in (child,root):
            if obj is not None and obj.name in bpy.data.objects:
                bpy.data.objects.remove(obj,do_unlink=True)
        if clone_mesh is not None and clone_mesh.users==0:
            bpy.data.meshes.remove(clone_mesh)
        if control.name in bpy.data.collections:
            bpy.data.collections.remove(control)
        raise
