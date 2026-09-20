"""Read-only SPM structural audit. Never writes beside production sources.

Geometry metrics describe uninstanced render templates, NOT physical leaves or EI.
Run with bundled Python (numpy) and a Blender installation for its standalone FBX parser.
"""
from __future__ import annotations
import argparse,base64,collections,gzip,hashlib,importlib,json,re,struct,sys,types
from pathlib import Path
import xml.etree.ElementTree as ET
import numpy as np


def fingerprint(path):
    path=Path(path);h=hashlib.sha256()
    with path.open('rb') as f:
        for chunk in iter(lambda:f.read(1048576),b''):h.update(chunk)
    return {'path':str(path.resolve()),'sha256':h.hexdigest(),'size':path.stat().st_size}

def number(value):
    try:return float(str(value).replace(',','.'))
    except (ValueError,TypeError):return None

def json_element(e):
    points=[{c.tag:c.text for c in p if not len(c)} for p in e.iter('ControlPoint')]
    return {'attributes':dict(e.attrib),'source_xml_sha256':hashlib.sha256(ET.tostring(e,encoding='utf-8')).hexdigest(),'control_point_count':len(points),'first_control_point':points[0] if points else None,'last_control_point':points[-1] if points else None,'direct_child_count':len(e),'curve_status':'summary only; evaluate exact source property for interpolation'}

def properties(g):
    result={}
    for p in g.findall('Properties/*'):
        name=p.findtext('Name','')
        if name:result[name]={'value':p.findtext('Value'),**{c.tag:c.text for c in p if not len(c) and c.tag not in ['Name','Value']},'curves':[json_element(c)|{'tag':c.tag} for c in p if len(c)]}
    return result

def geometry_metrics(vertices,faces,uvs=None):
    v=np.asarray(vertices,dtype=float)
    if v.ndim!=2 or v.shape[1]!=3 or not np.all(np.isfinite(v)):raise ValueError('Invalid vertices')
    tris=[];seen=set();raw_area=0.0;duplicate_count=0;ngon_count=0
    for face in faces:
        if any(i<0 or i>=len(v) for i in face):raise ValueError('Out-of-range geometry index')
        if len(face)>3:ngon_count+=1
        for k in range(1,len(face)-1):
            ids=[face[0],face[k],face[k+1]];p=v[ids]
            a=float(np.linalg.norm(np.cross(p[1]-p[0],p[2]-p[0]))/2);raw_area+=a
            key=tuple(sorted(tuple(round(float(x),10) for x in q) for q in p))
            if key in seen:duplicate_count+=1;continue
            seen.add(key)
            if a>0:tris.append((p,a))
    area=sum(a for p,a in tris)
    if area<=0:raise ValueError('Zero-area mesh')
    centroid=sum(a*p.mean(0) for p,a in tris)/area
    second=np.zeros((3,3))
    for p,a in tris:
        s=p.sum(0);second+=a*(p.T@p+np.outer(s,s))/12
    covariance=second/area-np.outer(centroid,centroid)
    eig,axes=np.linalg.eigh(covariance);order=np.argsort(eig)[::-1];axes=axes[:,order];eig=eig[order]
    used=np.array([p for p,a in tris]).reshape(-1,3)
    coords=(used-centroid)@axes;extent=np.ptp(coords,axis=0)
    return {'area_coordinate_units2':area,'raw_area_coordinate_units2':raw_area,
            'principal_length_coordinate_units':float(extent[0]),'principal_width_coordinate_units':float(extent[1]),
            'principal_aspect_ratio':float(extent[0]/extent[1]) if extent[1]>0 else None,
            'principal_third_extent_coordinate_units':float(extent[2]),
            'area_centroid_from_mesh_origin':centroid.tolist(),
            'area_radius_gyration_about_origin_coordinate_units':float(np.sqrt(np.trace(second/area))),
            'area_covariance_coordinate_units2':covariance.tolist(),
            'principal_axes_columns':axes.tolist(),'vertex_count':len(v),'triangle_count_unique':len(tris),
            'identical_geometric_triangle_duplicates_removed':duplicate_count,'ngon_fan_triangulations':ngon_count,
            'triangulation_confidence':'exact source triangles' if ngon_count==0 else 'fan approximation; requires convex planar polygon validation',
            'uv_bounds':None if uvs is None or not len(uvs) else [np.min(uvs,axis=0).tolist(),np.max(uvs,axis=0).tolist()],
            'origin_interpretation':'authored mesh origin only; physical attachment unverified',
            'area_interpretation':'unique geometric triangle surface sum; no opacity or overlap-union correction'}

def child(node,tag):return next((e for e in node.elems if e.id==tag),None)

def fbx_geometry(path,parser):
    root,version=parser.parse(str(path));objs=child(root,b'Objects');settings=child(root,b'GlobalSettings')
    prop=child(settings,b'Properties70') if settings else None
    unit={n.props[0].decode():n.props[-1] for n in prop.elems if b'UnitScale' in n.props[0]} if prop else {}
    models=[]
    for model in objs.elems:
        if model.id!=b'Model':continue
        ps=child(model,b'Properties70')
        transforms={n.props[0].decode():list(n.props[4:]) for n in ps.elems if n.props[0] in [b'Lcl Translation',b'Lcl Rotation',b'Lcl Scaling',b'GeometricTranslation',b'GeometricRotation',b'GeometricScaling',b'RotationPivot',b'ScalingPivot']} if ps else {}
        models.append({'id':model.props[0],'name':model.props[1].decode(errors='replace'),'transform_properties':transforms})
    result=[]
    for geo in objs.elems:
        if geo.id!=b'Geometry':continue
        ve=child(geo,b'Vertices');pe=child(geo,b'PolygonVertexIndex')
        if ve is None or pe is None:continue
        verts=np.asarray(ve.props[0]).reshape(-1,3);faces=[];face=[]
        for i in pe.props[0]:
            face.append(int(i if i>=0 else -i-1))
            if i<0:faces.append(face);face=[]
        uv_layer=child(geo,b'LayerElementUV');uv=child(uv_layer,b'UV') if uv_layer else None
        metric=geometry_metrics(verts,faces,None if uv is None else np.asarray(uv.props[0]).reshape(-1,2))
        result.append({'geometry_id':geo.props[0],**metric})
    return {'source':fingerprint(path),'geometry_space':'fbx_geometry_local_unscaled','fbx_version':version,'fbx_units_raw':unit,'models':models,'geometry':result,'unit_status':'unvalidated; FBX Model/SPM instance transforms are not applied'}

def b64_exact(text,size):
    s=''.join((text or '').split())
    try:data=base64.b64decode(s,validate=True)
    except Exception as exc:raise ValueError('Noncanonical Embedded v7 base64; no repair attempted') from exc
    if len(data)!=size:raise ValueError(f'Embedded v7 payload has {len(data)} bytes, expected {size}; no repair attempted')
    return data

def embedded_geometry(mesh):
    data=mesh.find('EmbeddedData_v7')
    if data is None:raise ValueError('No top-level EmbeddedData_v7')
    nv=int(data.get('NumVertices'));ni=int(data.get('NumIndices'))
    if ni%3:raise ValueError('Index count is not triangular')
    vertex=np.frombuffer(b64_exact(data.findtext('Vertices'),nv*148),dtype='<f4').reshape(nv,37)
    indices=np.frombuffer(b64_exact(data.findtext('Indices'),ni*4),dtype='<u4').reshape(-1,3)
    return {'geometry_space':'spm_embedded_native','geometry':[geometry_metrics(vertex[:,:3],indices,vertex[:,12:14])],'unit_status':'unvalidated; native template dimensions are not instanced physical dimensions','cutout_lod0':dict(mesh.find('Cutout/LOD0').attrib) if mesh.find('Cutout/LOD0') is not None else None}

def audit(row,parser,cache):
    source=Path(row['spm']);raw=source.read_bytes();root=ET.fromstring(gzip.decompress(raw) if raw[:2]==b'\x1f\x8b' else raw)
    gens={g.findtext('GUID'):g for g in root.findall('Generators/Generator')};props={guid:properties(g) for guid,g in gens.items()}
    links=collections.defaultdict(list);parents=collections.defaultdict(list);warnings=[]
    for l in root.findall('Links/Link'):
        s=l.findtext('SourceGUID');t=l.findtext('TargetGUID')
        if s not in gens or t not in gens:warnings.append('Unresolved generator link');continue
        links[s].append({'target_guid':t,'hidden_raw':l.findtext('Hidden'),'link_guid':l.findtext('GUID')});parents[t].append(s)
    nodes=collections.defaultdict(list)
    for n in root.findall('Nodes/Node'):nodes[n.findtext('GeneratorGUID')].append(n)
    materials={a.get('ID'):a for a in root.findall('Assets/Material_v8')};meshes={a.get('ID'):a for a in root.findall('Assets/Mesh')}
    outputs={};used_materials=set();used_meshes=set()
    def generator(guid):
        if guid in outputs:return outputs[guid]
        g=gens[guid];p=props[guid];slots=[]
        for key,value in p.items():
            match=re.fullmatch(r'((?:Material:[^:]+)|Leaves:Type):(\d+):Material',key)
            if not match:continue
            kind,index=match.groups();prefix=f'{kind}:{index}:';mid=value['value'];meshraw=p.get(prefix+'Mesh',{}).get('value');m=materials.get(mid)
            ids=[];mode='explicit'
            if meshraw=='-10':
                mode='material_cutout_list'
                if m is not None:ids=[m.findtext('CutoutMeshID')]+[n.get('ID') for n in m.findall('SupplementalCutoutMeshIDs/CutoutMesh')]
            elif meshraw is not None and not meshraw.startswith('-'):ids=[meshraw]
            else:mode='unresolved_or_builtin'
            ids=[i for i in ids if i is not None and i!='-1'];used_materials.add(mid);used_meshes.update(ids)
            slots.append({'kind':kind,'slot':int(index),'material_id':mid,'material_name':None if m is None else m.get('Name'),'mesh_raw':meshraw,'mesh_mode':mode,'mesh_ids':ids,'weight':p.get(prefix+'Weight')})
        n=nodes[guid];bad=collections.Counter()
        for nd in n:
            for tag in ['m_bDeleted','m_bCulled','m_bValidPosition']:
                val=nd.findtext('Extra/'+tag)
                if val=='true' and tag!='m_bValidPosition' or val=='false' and tag=='m_bValidPosition':bad[tag]+=1
        selected={k:v for k,v in p.items() if k.startswith(('Spine:Length:','Skin:Radius:','Shape:Scale:','Shape:Width:','Transform:')) or k in ['Leaves:Size','Leaves:Scale','Leaves:Aspect ratio','Skin:Type','Material:Two sided','Material:Flip','Generation:Mode','Generation:Frequency','Generation:Size scalar','Generation:Style']}
        outputs[guid]={'guid':guid,'name':g.findtext('Name'),'type':g.get('Type'),'hidden_raw':g.findtext('Hidden'),'level_raw':g.findtext('Level'),'parent_guids':parents[guid],'node_count':len(n),'node_flag_counts':dict(bad),'properties':selected,'material_slots':slots,'activity_status':'authored graph and saved node-table evidence; live procedural/export activity not proven'}
        return outputs[guid]
    supports=[]
    for guid,g in gens.items():
        if props[guid].get('Skin:Type',{}).get('value')!='3':continue
        descendants=[];queue=[(guid,[guid],False)]
        while queue:
            current,path,hidden_path=queue.pop(0)
            for l in links[current]:
                nxt=l['target_guid']
                if nxt in path:warnings.append('Generator cycle rejected');continue
                pth=path+[nxt];hidden=hidden_path or l['hidden_raw']=='true' or gens[nxt].findtext('Hidden')=='true'
                if gens[nxt].get('Type') in ['Frond','Leaf Mesh']:
                    item=generator(nxt)
                    descendants.append({'generator_guid':nxt,'name':item['name'],'type':item['type'],'path_guids':pth,'has_hidden_ancestor_or_link':hidden,'intermediate_branch_guids':[q for q in pth[1:-1] if gens[q].get('Type')=='Branch']})
                queue.append((nxt,pth,hidden))
        support=generator(guid)
        supports.append({'support':{k:support[k] for k in ['guid','name','type','hidden_raw','node_count','node_flag_counts']},'descendants':descendants,'stiffness_status':'unknown; render thickness/zero radius is not physical EI','aggregation_status':'do not sum across ancestor supports without unique load ownership'})
    # Preserve leaf/tube comparison inputs even for reed with no Spine Only support.
    for guid,g in gens.items():
        if g.get('Type') in ['Frond','Leaf Mesh','Branch']:generator(guid)
    material_out={}
    for mid in sorted(used_materials,key=str):
        m=materials.get(mid)
        if m is None:material_out[mid]={'error':'material missing'};continue
        maps=[]
        for mp in m.findall('Map'):
            if mp.get('Name') not in ['Color','Opacity','Height']:continue
            filename=mp.findtext('TexFilename');path=(source.parent/filename).resolve() if filename else None
            maps.append({'name':mp.get('Name'),'stored_filename':filename,'path':None if path is None else str(path),'exists':False if path is None else path.exists(),'enabled_raw':mp.findtext('TexEnabled'),'source_channel_raw':mp.findtext('TexSource'),'invert_raw':mp.findtext('TexInvert')})
        material_out[mid]={'name':m.get('Name'),'two_sided_raw':m.findtext('TwoSided'),'maps':maps,'opacity_status':'not integrated; disabled maps are not implicitly enabled'}
    mesh_out={}
    for ident in sorted(used_meshes,key=str):
        m=meshes.get(ident)
        if m is None:mesh_out[ident]={'error':'mesh asset missing'};continue
        item={'name':m.get('Name'),'scale_raw':m.findtext('Scale'),'unitized_raw':m.findtext('Unitized'),'embedded_raw':m.findtext('Embedded'),'orient_raw':m.findtext('Orient'),'spine_style_raw':m.findtext('SpineStyle'),'pivot_style_raw':m.findtext('PivotStyle')}
        try:
            if m.findtext('Embedded')=='true':item.update(embedded_geometry(m))
            else:
                filename=m.findtext('Filename')
                if not filename:raise ValueError('External mesh filename missing')
                path=(source.parent/filename).resolve();key=str(path).casefold()
                if key not in cache:cache[key]=fbx_geometry(path,parser)
                item.update(cache[key])
        except Exception as exc:item['error']=f'{type(exc).__name__}: {exc}'
        mesh_out[ident]=item
    export={}
    richpath=Path(row.get('rich',''))
    if richpath.is_file():
        rich=json.loads(richpath.read_text(encoding='utf-8-sig'));xm=rich.get('skeleton',{}).get('xml',{});xp=Path(xm.get('source',''))
        export={'rich_source':fingerprint(richpath),'xml_metadata':xm,'warning':'rich radius/mass are exporter values, not measured biological material properties'}
        if xp.is_file():
            xr=ET.parse(xp).getroot();export['xml_source']=fingerprint(xp);export['object_bounds_raw']=dict(xr.find('Objects').attrib) if xr.find('Objects') is not None else None
            export['bone_sample']=[dict(b.attrib) for b in xr.findall('Bones/Bone')[:3]]
    return {'stem':row['stem'],'source_spm':fingerprint(source),'source_unreal_asset':row.get('asset'),'wind_enabled_inventory':row.get('enabled'),'scene_unit_scale_raw':root.findtext('SceneUnitScale'),'spm_version':root.attrib,'spine_only_supports':supports,'generators':outputs,'mesh_geometries':mesh_out,'materials':material_out,'export_linkage':export,'warnings':sorted(set(warnings)),'scope_limits':['No physical EI, thickness, mass density, damping or runtime gain inferred.','Template metrics do not include generator deformation, per-node scale, world transform or instancing.','Area excludes identical triangle duplicates only; opacity holes/overlap-union/physical leaflet segmentation are unresolved.','SPM node table may be stale. Hidden/raw generation state must be reconciled against current exporter.','Base/BaseRef subtree expansion is not reconstructed; generator connectivity alone cannot resolve reused cluster instances.']}
