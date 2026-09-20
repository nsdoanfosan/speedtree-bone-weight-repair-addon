"""Read-only evaluated export load descriptors and bounded ART bend modifiers.

Material priors below are declared art assumptions, not biological measurements.
The gain only concerns amplitude; it never changes Influence/group/time fields.
"""
from pathlib import Path
import sys,types,importlib,json,collections,math,argparse,hashlib,os,xml.etree.ElementTree as ET
import numpy as np
BASE=Path(__file__).parent
RECIPE={'id':'evaluated_structure_art_v4','mode':'bounded_art_approximation','material_priors_measured':False,'foliage_E_Pa':1e9,'nominal_branch_E_Pa':6e9,'rachis_min_diameter_m':.001,'rachis_max_diameter_m':.012,'flat_min_thickness_m':.00025,'flat_reinforcement_reference_aspect':8.,'nominal_min_diameter_m':.0005,'reference_pressure_Pa':1.,'gain_min':.85,'gain_max':1.25,'transition_proxy_radians':.25,'coordinate_tolerance_cm':.01,'final_bind_position_source':'final_fbx_cluster_TransformLink_after_native_XML_validation','missing_frond_aspect_policy':'neutral_support','load_ownership':'normalize_valid_native_skin_weights_for_area_only','generator_mapping':'unique_current_source_name_to_GUID','no_asset_name_overrides':True}
RECIPE_SHA256=hashlib.sha256(json.dumps(RECIPE,sort_keys=True,separators=(',',':')).encode()).hexdigest()
def child(node,tag):return next((e for e in node.elems if e.id==tag),None) if node is not None else None
def fingerprint(path):
 path=Path(path);h=hashlib.sha256()
 with path.open('rb') as stream:
  for block in iter(lambda:stream.read(1048576),b''):h.update(block)
 return {'path':str(path.resolve()),'sha256':h.hexdigest(),'size':path.stat().st_size}
def load_fbx_parser():
 """Load Blender's pure Python FBX reader without importing bpy or launching Blender."""
 directory=Path(os.environ.get('WIND_FBX_PARSER_DIR',''))
 if not (directory/'parse_fbx.py').is_file():
  directory=next((Path(p)/'io_scene_fbx' for p in sys.path if (Path(p)/'io_scene_fbx/parse_fbx.py').is_file()),directory)
 if not (directory/'parse_fbx.py').is_file():raise FileNotFoundError('Set WIND_FBX_PARSER_DIR to the pure Python io_scene_fbx parser directory')
 pkg=types.ModuleType('wind_fbx');pkg.__path__=[str(directory)];sys.modules['wind_fbx']=pkg
 return importlib.import_module('wind_fbx.parse_fbx')

def num(x):return float(str(x).replace(',','.'))
def name(o):return o.props[1].split(b'\x00')[0].decode(errors='replace')
def xyz(attrs,prefix):return np.array([num(attrs[prefix+x]) for x in 'XYZ'])
def fbx_to_xml(v):return np.asarray(v)[...,[0,2,1]]*np.array([1,-1,1])
def xml_to_fbx(v):return np.asarray(v)[...,[0,2,1]]*np.array([1,1,-1])

def final_export_positions(path,fbx_parser):
 root,_=fbx_parser.parse(str(path));objects=child(root,b'Objects');objs={o.props[0]:o for o in objects.elems};down=collections.defaultdict(list)
 properties=child(child(root,b'GlobalSettings'),b'Properties70')
 units={p.props[0].decode():p.props[-1] for p in properties.elems if b'UnitScale' in p.props[0]}
 if units.get('UnitScaleFactor')!=1.:raise ValueError('Final FBX is not in the validated centimeter convention')
 for link in child(root,b'Connections').elems:
  if link.props[0]==b'OO':down[link.props[2]].append(link.props[1])
 positions={}
 for cluster in objects.elems:
  if cluster.id!=b'Deformer' or cluster.props[-1]!=b'Cluster':continue
  bind=child(cluster,b'TransformLink');models=[objs[x] for x in down[cluster.props[0]] if x in objs and objs[x].id==b'Model']
  if bind is None or len(models)!=1:continue
  bn=name(models[0]);raw=np.asarray(bind.props[0],dtype=float).reshape(4,4)[3,:3];position=fbx_to_xml(raw)*[1,-1,1]
  if not np.all(np.isfinite(position)):raise ValueError('Nonfinite final FBX bind position')
  if bn in positions and np.linalg.norm(positions[bn]-position)>.001:raise ValueError('Conflicting final bind positions for '+bn)
  positions[bn]=position
 return positions

def validate_final_export_bind(path,bones,fbx_parser):
 positions=final_export_positions(path,fbx_parser);errors=[];wrapper=None
 for bone in bones:
  actual=positions.get(bone['name'])
  if actual is None:raise ValueError('Final FBX has no exact Cluster bind for '+bone['name'])
  bone['expected_bind_position_ue_cm']=actual.tolist()
  bone['bind_source_status']='actual final FBX Cluster TransformLink; native XML validates source lineage and units'
  expected=bone['expected_bind_position_xml_cm']
  if expected is None:
   if bone['parent_index']!=-1:raise ValueError('Non-wrapper bone lacks source endpoint')
   wrapper=actual.tolist();continue
  errors.append(float(np.linalg.norm(actual-np.asarray(expected)*[1,-1,1])))
 if max(errors or [float('inf')])>=RECIPE['coordinate_tolerance_cm']:raise ValueError('Final FBX/native XML bind mismatch')
 return {'validated':True,'bones_checked':len(bones),'max_error_cm':max(errors),'wrapper_position_ue_cm':wrapper,'bind_space':'unreal_component_cm','source':'Final FBX Cluster TransformLink, validated against native XML'}

def analyze(row,audit,*,rich_data=None,wind_data=None,fbx_parser=None,final_fbx_path=None):
 parser=fbx_parser or load_fbx_parser()
 if fingerprint(row['spm'])['sha256']!=audit['source_spm']['sha256']:raise ValueError('SPM audit source hash is stale; regenerate source structure audit')
 rich=rich_data if rich_data is not None else json.loads(Path(row['rich']).read_text(encoding='utf-8-sig'));xp=Path(rich['skeleton']['xml']['source']);xr=ET.parse(xp).getroot();xml={int(b.get('ID')):dict(b.attrib) for b in xr.findall('Bones/Bone')}
 wind=wind_data if wind_data is not None else json.loads(Path(row['wind']).read_text(encoding='utf-8-sig'));contract=wind['SkeletonContract']
 expected_topology=[(b['BoneIndex'],b['BoneName'],b['ParentIndex']) for b in contract['Bones']]
 rich_topology=[(b['bone_index'],b['name'],b['parent_index']) for b in rich['skeleton']['bones']]
 if expected_topology!=rich_topology:raise ValueError('Wind and rich source skeleton topology differ')
 native=Path(rich['asset']['source_fbx'])
 final_fbx=Path(final_fbx_path or rich['asset']['exported_fbx'])
 native_receipt=native.with_suffix('.speedtree_native_receipt.json')
 source_hashes={label:fingerprint(path) for label,path in {'spm':Path(row['spm']),'native_fbx':native,'xml':xp,'final_fbx':final_fbx}.items()}
 if native_receipt.is_file():source_hashes['native_receipt']=fingerprint(native_receipt)
 root,version=parser.parse(str(native));obj=child(root,b'Objects');objs={o.props[0]:o for o in obj.elems};links=child(root,b'Connections');up=collections.defaultdict(list);down=collections.defaultdict(list)
 for l in links.elems:
  if l.props[0]==b'OO':up[l.props[1]].append(l.props[2]);down[l.props[2]].append(l.props[1])
 rich_by_name={b['name']:b for b in rich['skeleton']['bones']};guid_by_name=collections.defaultdict(list)
 for gid,g in audit['generators'].items():guid_by_name[g['name']].append(gid)
 foliage_names={audit['materials'].get(s['material_id'],{}).get('name') for g in audit['generators'].values() if g['type'] in ['Frond','Leaf Mesh'] for s in g['material_slots']}
 foliage_names.discard(None)
 accum={i:{'area':0.,'first':np.zeros(3)} for i in xml};details=[];coords=[];errors=[];weight_errors=[]
 gs=child(root,b'GlobalSettings');gp=child(gs,b'Properties70');unit={p.props[0].decode():p.props[-1] for p in gp.elems if b'UnitScale' in p.props[0]}
 if unit.get('UnitScaleFactor')!=1.:raise ValueError('Native FBX unit is not the validated centimeter convention')
 for geo in obj.elems:
  if geo.id!=b'Geometry' or child(geo,b'Vertices') is None:continue
  gid=geo.props[0];v=fbx_to_xml(np.asarray(child(geo,b'Vertices').props[0],dtype=float).reshape(-1,3));coords.append(v)
  poly=np.array(child(geo,b'PolygonVertexIndex').props[0],dtype=np.int64)
  # Evaluated native exports in this task use triangles. Refuse an implicit ngon triangulation.
  if len(poly)%3 or not np.all(poly.reshape(-1,3)[:,2]<0) or np.any(poly.reshape(-1,3)[:,:2]<0):raise ValueError('Native evaluated geometry contains nontriangles')
  faces=poly.reshape(-1,3).copy();faces[:,2]=-faces[:,2]-1
  if not np.all(np.isfinite(v)) or np.any(faces<0) or np.any(faces>=len(v)):raise ValueError('Invalid native vertex/index data')
  tri=v[faces];area=np.linalg.norm(np.cross(tri[:,1]-tri[:,0],tri[:,2]-tri[:,0]),axis=1)*.5
  dual=np.zeros(len(v));np.add.at(dual,faces.ravel(),np.repeat(area/3,3));is_foliage=name(geo) in foliage_names
  skinids=[sid for sid in down[gid] if sid in objs and objs[sid].id==b'Deformer' and objs[sid].props[-1]==b'Skin'];sums=np.zeros(len(v));valid_sums=np.zeros(len(v));contributions=[]
  for sid in skinids:
   for cid in down[sid]:
    cluster=objs.get(cid)
    if cluster is None or cluster.id!=b'Deformer' or cluster.props[-1]!=b'Cluster':continue
    bmodels=[objs[bid] for bid in down[cid] if bid in objs and objs[bid].id==b'Model']
    if len(bmodels)!=1:continue
    bn=name(bmodels[0]);rb=rich_by_name.get(bn);xi=None if rb is None else rb.get('xml_id')
    ids=child(cluster,b'Indexes');weights=child(cluster,b'Weights')
    if ids is None or weights is None:continue
    ids=np.asarray(ids.props[0],dtype=int);weights=np.asarray(weights.props[0],dtype=float)
    if len(ids)!=len(weights) or np.any(ids<0) or np.any(ids>=len(v)) or not np.all(np.isfinite(weights)) or np.any(weights<0):raise ValueError('Invalid native skin indices/weights')
    sums[ids]+=weights
    if xi not in xml:continue
    bind=child(cluster,b'TransformLink')
    if bind is not None:
     bindpos=fbx_to_xml(np.asarray(bind.props[0]).reshape(4,4)[3,:3]);target=xyz(xml[xi],'End' if bn.endswith('_End') else 'Start');errors.append(float(np.linalg.norm(bindpos-target)))
    if is_foliage:
     valid_sums[ids]+=weights;contributions.append((xi,ids,weights))
  for xi,ids,weights in contributions:
   fractions=np.divide(weights,valid_sums[ids],out=np.zeros_like(weights),where=valid_sums[ids]>1e-12)
   w=dual[ids]*fractions;accum[xi]['area']+=float(w.sum());accum[xi]['first']+=(v[ids]*w[:,None]).sum(0)
  relevant=dual>1e-12
  weight_errors.append(float(np.max(np.abs(sums[relevant]-1))) if np.any(relevant) else 0)
  details.append({'geometry_name':name(geo),'vertex_count':len(v),'triangles':len(faces),'surface_area_cm2':float(area.sum()),'counted_as_foliage':is_foliage,'skin_weight_max_error':weight_errors[-1],'load_ownership':'native bone weights normalized only for area ownership; runtime skin weights unchanged','unassigned_foliage_surface_cm2':float(dual[valid_sums<=1e-12].sum()) if is_foliage else 0.,'area_semantics':'evaluated source triangle surface; opacity, directional exposure and Assembly replacement leaf area not inferred'})
 allv=np.vstack(coords);obsmin=allv.min(0);obsmax=allv.max(0);bounds=xr.find('Objects').attrib;expectedmin=xyz(bounds,'BoundsMin');expectedmax=xyz(bounds,'BoundsMax');boundserror=float(max(np.max(np.abs(obsmin-expectedmin)),np.max(np.abs(obsmax-expectedmax))))
 if max(errors or [float('inf')])>=RECIPE['coordinate_tolerance_cm'] or boundserror>=RECIPE['coordinate_tolerance_cm']:raise ValueError('Native FBX/XML bind or evaluated geometry bounds mismatch')
 # Aggregate assigned foliage along the native XML parent hierarchy.
 children=collections.defaultdict(list)
 for i,b in xml.items():children[int(b['ParentID'])].append(i)
 memo={}
 def aggregate(i,stack=()):
  if i in memo:return memo[i]
  if i in stack:raise ValueError('Bone hierarchy cycle')
  a=accum[i]['area'];f=accum[i]['first'].copy()
  for c in children[i]:ca,cf=aggregate(c,stack+(i,));a+=ca;f+=cf
  memo[i]=(a,f);return a,f
 def same_generator_span(i):
  own=float(np.linalg.norm(xyz(xml[i],'End')-xyz(xml[i],'Start')))
  return own+max([same_generator_span(c) for c in children[i] if xml[c]['Generator']==xml[i]['Generator']] or [0.])
 support_by_guid={s['support']['guid']:s for s in audit['spine_only_supports']}
 def aspect_info(guid):
  s=support_by_guid.get(guid);values=[];broadleaf=False
  if s:
   broadleaf=any(q['type']=='Leaf Mesh' for q in s['descendants'])
   for desc in s['descendants']:
    if desc['type']!='Frond':continue
    g=audit['generators'][desc['generator_guid']]
    for slot in g['material_slots']:
     for mid in slot['mesh_ids']:
      values += [m['principal_aspect_ratio'] for m in audit['mesh_geometries'].get(mid,{}).get('geometry',[]) if m['principal_aspect_ratio']]
  return float(np.median(values)) if values else None,broadleaf
 native_responses=[]
 for i,b in xml.items():
  ids=guid_by_name[b['Generator']];guid=ids[0] if len(ids)==1 else None;spine=guid in support_by_guid;A,first=aggregate(i);anchor=xyz(b,'Start');L=max(same_generator_span(i)*.01,1e-5);cent=first/A if A>0 else anchor;lever=float(np.linalg.norm(cent-anchor))*.01;AR,branch_leaf=aspect_info(guid);width=L/max(AR or 8.,1.0);radius=num(b['Radius'])*.01
  if spine and branch_leaf:
   diameter=float(np.clip(width,RECIPE['rachis_min_diameter_m'],RECIPE['rachis_max_diameter_m']));E=RECIPE['foliage_E_Pa'];I=math.pi*diameter**4/64;prior={'section':'circular_rachis','E_Pa':E,'diameter_m':diameter,'derivation':'support span / source stem-template aspect, clamped 1-12 mm; art prior, not measured thickness'}
  elif spine:
   t=RECIPE['flat_min_thickness_m']*max(1.,RECIPE['flat_reinforcement_reference_aspect']/max(AR or 8.,1.));E=RECIPE['foliage_E_Pa'];I=max(width,1e-5)*t**3/12;prior={'section':'effective_flat_frond','E_Pa':E,'width_m':width,'thickness_m':t,'derivation':'0.25 mm effective base, wider-frond reinforcement factor max(1,8/aspect); explicitly ART material prior'}
  else:
   E=RECIPE['nominal_branch_E_Pa'];diameter=max(2*radius,RECIPE['nominal_min_diameter_m']);I=math.pi*diameter**4/64;prior={'section':'solid_circular_nominal','E_Pa':E,'diameter_m':diameter,'derivation':'exported radius as geometric/art section proxy; tissue properties and hollow core unmeasured'}
  EI=E*I;moment=A*1e-4*lever*RECIPE['reference_pressure_Pa']
  theta=moment*L/max(EI,1e-12);gain=RECIPE['gain_min']+(RECIPE['gain_max']-RECIPE['gain_min'])*theta/(theta+RECIPE['transition_proxy_radians'])
  neutral_reasons=[]
  if A<=0:neutral_reasons.append('no_assigned_downstream_foliage')
  if guid is None:neutral_reasons.append('ambiguous_or_missing_generator_GUID')
  if spine and AR is None:neutral_reasons.append('missing_source_frond_aspect')
  if not spine and radius<=0:neutral_reasons.append('missing_non_spine_nominal_radius')
  if neutral_reasons:gain=1.
  native_responses.append({'xml_id':i,'parent_xml_id':int(b['ParentID']),'generator':b['Generator'],'generator_guid':guid,'generator_mapping_status':'unique exported generator name to current SPM GUID' if guid is not None else 'ambiguous name; neutral gain','spine_only':spine,'span_cm':L*100,'direct_assigned_foliage_area_cm2':accum[i]['area'],'downstream_foliage_area_cm2':A,'area_centroid_cm':cent.tolist(),'load_lever_cm':lever*100,'frond_template_aspect':AR,'nominal_export_radius_cm':num(b['Radius']),'effective_section_prior':prior,'proxy_EI_Nm2':EI,'unit_pressure_moment_Nm':moment,'small_deflection_angle_proxy_per_Pa':theta,'art_bend_gain':gain,'gain_status':'bounded_artist_approximation_not_physical_prediction'})
  native_responses[-1]['neutral_reasons']=neutral_reasons
 nr={r['xml_id']:r for r in native_responses};bone_out=[]
 for rb in rich['skeleton']['bones']:
  response=nr.get(rb.get('xml_id'));segment=xml.get(rb.get('xml_id'));bind=None if segment is None else xyz(segment,'End' if rb['name'].endswith('_End') else 'Start')
  bone_out.append({'bone_index':rb['bone_index'],'name':rb['name'],'parent_index':rb['parent_index'],'xml_id':rb.get('xml_id'),'generator_guid':None if response is None else response['generator_guid'],'expected_bind_position_xml_cm':None if bind is None else bind.tolist(),'expected_bind_position_native_fbx_cm':None if bind is None else xml_to_fbx(bind).tolist(),'bind_source_status':'wrapper: no XML support position' if bind is None else 'native XML global position; signed UE axis mapping requires live validation','art_bend_gain':1. if response is None else response['art_bend_gain']})
 active=[r for r in native_responses if r['direct_assigned_foliage_area_cm2']>0];ws=np.array([r['direct_assigned_foliage_area_cm2'] for r in active]);gain=float(np.average([r['art_bend_gain'] for r in active],weights=ws)) if len(ws) else 1.
 result={'stem':row['stem'],'unreal_asset':row.get('asset'),'source_hashes':source_hashes,'skeleton_contract':{k:v for k,v in contract.items() if k!='Bones'},'source_skeleton_topology_match':True,'unit_validation':{'fbx_global_unit_metadata':unit,'fbx_to_xml_coordinates':'(x,y,z)->(x,-z,y), scale=1 native cm','bind_comparisons':len(errors),'max_bind_error_cm':max(errors or [0]),'bounds_max_error_cm':boundserror,'native_bounds_cm':[obsmin.tolist(),obsmax.tolist()],'xml_bounds_cm':[expectedmin.tolist(),expectedmax.tolist()],'validated':True,'ue_unit_status':'requires_live_bind_validation','source_export_clue':'source geometry agrees with native XML centimeters; loaded UE bind still requires validation'},'geometry':details,'native_support_segments':native_responses,'bones':bone_out,'asset_bend_gain':gain,'gain_range':[RECIPE['gain_min'],RECIPE['gain_max']],'BendRateScale':1.,'TorsionGain':1.,'FlutterGain':1.,'assumptions':['Evaluated native exported triangle area with skin-weight area lumping, not final deformed exposed projected area.','Source canopy cards are envelope proxies, not resolved Nanite replacement leaf area; no opacity correction.','E, thickness, reinforced frond prior are declared ART assumptions. No species-name category weighting used.','Per-bone gains are amplitude-only suggestions, never modify Influence/group/GustAttenuation.','Do not multiply bone gain and asset gain together: asset gain is a summary or fallback only.']}
 final_binding=validate_final_export_bind(final_fbx,bone_out,parser)
 result['final_export_binding']=final_binding
 result['data_gaps']=[{'kind':'unresolved_authored_material_slot','material_id':ident,'detail':m.get('error')} for ident,m in audit['materials'].items() if 'name' not in m]
 result['support_neutral_reason_counts']=dict(collections.Counter(reason for s in native_responses for reason in s['neutral_reasons']))
 for b in bone_out:
  b['neutral_reasons']=nr[b['xml_id']]['neutral_reasons'] if b['xml_id'] in nr else ['non_support_wrapper']
 result['source_hashes_unchanged']=all(fingerprint(v['path'])['sha256']==v['sha256'] for v in source_hashes.values())
 if not result['source_hashes_unchanged']:raise ValueError('Source changed while evaluating structure')
 return result

def neutral_result(row,reason,*,rich_data=None,wind_data=None,fbx_parser=None,final_fbx_path=None):
 rich=rich_data or {};wind=wind_data or {};positions={};hashes={};issues=[]
 for key in ['rich','wind']:
  if key=='rich' and rich or key=='wind' and wind:continue
  try:
   data=json.loads(Path(row[key]).read_text(encoding='utf-8-sig'))
   if key=='rich':rich=data
   else:wind=data
  except Exception as exc:issues.append(f'{key}: {exc}')
 for label,path in {'spm':row.get('spm'),'native_fbx':rich.get('asset',{}).get('source_fbx'),'final_fbx':final_fbx_path or rich.get('asset',{}).get('exported_fbx'),'xml':rich.get('skeleton',{}).get('xml',{}).get('source')}.items():
  if path and Path(path).is_file():hashes[label]=fingerprint(path)
 try:
  positions=final_export_positions(hashes['final_fbx']['path'],fbx_parser or load_fbx_parser())
 except Exception as exc:issues.append('final bind unavailable: '+str(exc))
 contract=wind.get('SkeletonContract',{});bones=[]
 for b in contract.get('Bones',[]):
  p=positions.get(b['BoneName']);bones.append({'bone_index':b['BoneIndex'],'name':b['BoneName'],'parent_index':b['ParentIndex'],'xml_id':None,'generator_guid':None,'art_bend_gain':1.,'expected_bind_position_ue_cm':None if p is None else p.tolist(),'neutral_reasons':[reason]})
 return {'stem':row['stem'],'unreal_asset':row.get('asset'),'status':'neutral' if reason in {'wind_disabled','native_boneless_rigid'} else 'neutral_error','neutral_reasons':[reason],'data_gaps':issues,'source_hashes':hashes,'source_hashes_unchanged':True,'bones':bones,'skeleton_contract':{k:v for k,v in contract.items() if k!='Bones'},'asset_bend_gain':1.,'BendRateScale':1.,'TorsionGain':1.,'FlutterGain':1.,'final_export_binding':{'validated':bool(bones) and all(b['expected_bind_position_ue_cm'] is not None for b in bones),'source':'Final FBX only; neutral response does not infer missing source support'},'geometry':[],'native_support_segments':[]}

def evaluate_asset(row,audit=None,*,rich_data=None,wind_data=None,fbx_parser=None,template_cache=None,final_fbx_path=None):
 """Evaluate any asset from source geometry; no asset/species allowlist or overrides.

 In-memory metadata supports the Batch export transaction before its JSON is
 written. Native/final FBX files must already exist. This function writes nothing.
 """
 parser=fbx_parser or load_fbx_parser();kwargs={'rich_data':rich_data,'wind_data':wind_data,'fbx_parser':parser,'final_fbx_path':final_fbx_path}
 try:
  wind=wind_data if wind_data is not None else json.loads(Path(row['wind']).read_text(encoding='utf-8-sig'))
  rich=rich_data if rich_data is not None else json.loads(Path(row['rich']).read_text(encoding='utf-8-sig'))
  if row.get('enabled') is False or wind.get('bIsEnabled',True) is False:
   result=neutral_result(row,'wind_disabled',**kwargs)
  elif rich.get('skeleton',{}).get('xml',{}).get('mapping_contract')=='native_boneless_rigid_axis_v1':
   result=neutral_result(row,'native_boneless_rigid',**kwargs)
  else:
   if audit is None:
    try:from . import wind_structure_source
    except ImportError:import wind_structure_source
    # Avoid old rich/Wind outputs in the fresh source audit: current metadata
    # is passed directly to analyze() below.
    audit=wind_structure_source.audit({**row,'rich':''},parser,{} if template_cache is None else template_cache)
   result=analyze(row,audit,**kwargs)
   result['status']='candidate' if any(b['art_bend_gain']!=1. for b in result['bones']) else 'neutral'
   result['neutral_reasons']=[] if result['status']=='candidate' else ['no_resolved_nonzero_foliage_support_response']
 except Exception as exc:
  result=neutral_result(row,f'{type(exc).__name__}: {exc}',**kwargs)
 result['recipe_id']=RECIPE['id'];result['recipe_sha256']=RECIPE_SHA256
 result['is_physical_calibration']=False
 return result

def evaluate_inventory(inventory,audit_assets=None,*,fbx_parser=None,progress=None):
 parser=fbx_parser or load_fbx_parser();audit_assets=audit_assets or {};cache={};results=[]
 if isinstance(audit_assets,list):audit_assets={a['stem']:a for a in audit_assets}
 for row in inventory:
  result=evaluate_asset(row,audit_assets.get(row['stem']),fbx_parser=parser,template_cache=cache);results.append(result)
  if progress:progress(result)
 return {'schema':'evaluated_source_art_bend_candidates','schema_version':3,'recipe':RECIPE,'recipe_sha256':RECIPE_SHA256,'scope':'all_inventory_assets_no_asset_name_overrides','assets':results,'summary':{'assets':len(results),'status_counts':dict(collections.Counter(r['status'] for r in results)),'bones':sum(len(r['bones']) for r in results),'nonneutral_bones':sum(b['art_bend_gain']!=1. for r in results for b in r['bones'])},'writes_production_files':False}

def main():
 cli=argparse.ArgumentParser(description=__doc__);cli.add_argument('--inventory',type=Path,required=True);cli.add_argument('--audit',type=Path);cli.add_argument('--fbx-parser-dir',type=Path);cli.add_argument('--output',type=Path,default=BASE/'evaluated_load_all_assets.json');args=cli.parse_args()
 if args.fbx_parser_dir:os.environ['WIND_FBX_PARSER_DIR']=str(args.fbx_parser_dir)
 inv=json.loads(args.inventory.read_text(encoding='utf-8-sig'));audit=[] if args.audit is None else json.loads(args.audit.read_text(encoding='utf-8'))['assets']
 def progress(r):print(r['stem'],r['status'],len(r['bones']),'bones',round(r['asset_bend_gain'],6),r['neutral_reasons'],flush=True)
 result=evaluate_inventory(inv,audit,progress=progress);result['inputs']={'inventory':fingerprint(args.inventory)}
 if args.audit:result['inputs']['audit']=fingerprint(args.audit)
 args.output.parent.mkdir(parents=True,exist_ok=True);args.output.write_text(json.dumps(result,ensure_ascii=False,separators=(',',':'),allow_nan=False),encoding='utf-8')
 print(json.dumps(result['summary']),flush=True)
if __name__=='__main__':main()
