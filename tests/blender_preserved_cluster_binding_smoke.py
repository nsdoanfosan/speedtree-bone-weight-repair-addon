"""A verified source receipt must produce usable bindings, preserving Opacity."""
import json,sys,tempfile
from pathlib import Path
import bpy
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'addons'))
from speedtree_bone_weight_repair import core

def write_image(path):
 image=bpy.data.images.new(path.stem,width=2,height=2)
 image.filepath_raw=str(path);image.file_format='PNG';image.save()
 bpy.data.images.remove(image)

with tempfile.TemporaryDirectory() as directory:
 root=Path(directory);color=root/'cluster_color.png';normal=root/'cluster_normal.png';opacity=root/'cluster_opacity.png'
 for path in (color,normal,opacity):write_image(path)
 m=bpy.data.materials.new('M_cluster_probe');m.use_nodes=True;m['codex_source_fbx']=str(root/'probe.fbx')
 bsdf=next(n for n in m.node_tree.nodes if n.type=='BSDF_PRINCIPLED')
 op=m.node_tree.nodes.new('ShaderNodeTexImage');op.image=bpy.data.images.load(str(opacity))
 mesh=bpy.data.meshes.new('probe');mesh.materials.append(m);obj=bpy.data.objects.new('probe',mesh)
 binding={'material':m.name,'status':'ok','texture_contract_status':core.ATLAS_BLENDER_CLUSTER_BAKE_STATUS,'source_paths':{'color':str(color),'normal':str(normal),'opacity':str(opacity)},'origin_receipt':{'kind':'verified_fixture'}}
 contract={'atlas_manifest_prevalidated':True,'bindings':[binding]}
 result=core.normalize_speedtree_material_textures([obj],contract)
 assert bsdf.inputs['Base Color'].is_linked,result
 assert bsdf.inputs['Normal'].is_linked,result
 assert not bsdf.inputs['Alpha'].is_linked
 assert op in list(m.node_tree.nodes)
 links=[(l.from_node.name,l.from_socket.name,l.to_node.name,l.to_socket.name) for l in m.node_tree.links]
 second=core.normalize_speedtree_material_textures([obj],contract)
 assert second['changed_count']==0,second
 assert links==[(l.from_node.name,l.from_socket.name,l.to_node.name,l.to_socket.name) for l in m.node_tree.links]
 # The STMAT resolver calls physical captures unmanaged; exact proof must
 # promote that input to a usable binding rather than keep leave_unassigned.
 unresolved=dict(binding,status='not_managed',binding_disposition='leave_unassigned')
 proof={'source_maps':binding['source_paths'],'origin_receipt':binding['origin_receipt']}
 with patch.object(core,'_speedtree_manifest_texture_binding',return_value=None), patch.object(core,'_speedtree_preserved_cluster_sources',return_value=proof):
  preflight=core.preflight_speedtree_material_texture_contracts([obj],{'bindings':[unresolved]})
 resolved=preflight['texture_contract']['bindings'][0]
 assert resolved['status']=='ok',resolved
 assert resolved['texture_source_mode']=='preserve_declared_sources',resolved
 assert resolved['binding_disposition']=='bind_available',resolved
 core.normalize_speedtree_material_textures([obj],preflight['texture_contract'])
 assert bsdf.inputs['Base Color'].is_linked
 # Invalid proof still fails in strict publication mode.
 with patch.object(core,'_speedtree_manifest_texture_binding',return_value=None), patch.object(core,'_speedtree_preserved_cluster_sources',return_value=None):
  try:core.preflight_speedtree_material_texture_contracts([obj],{'bindings':[unresolved],'strict_speedtree_pipeline_contract':True})
  except RuntimeError:pass
  else:raise AssertionError('Invalid capture proof was accepted')
 # A renamed/consolidated material can retain exact exported STMAT image identity.
 export=root/'renamed_export.png';write_image(export)
 stmat=root/'probe.stmat'
 stmat.write_text('<SpeedTreeMaterials><Material Name="M_original_Mat"><Map Name="Color" File="renamed_export.png" Source="original.png" /></Material></SpeedTreeMaterials>')
 bsdf.inputs['Base Color'].links[0].from_node.image=bpy.data.images.load(str(export))
 proof=core._speedtree_connected_declared_sources(m,core._speedtree_stmat_materials(root/'probe.fbx'))
 assert proof and proof['source_material']=='M_original_Mat',proof
print('PRESERVED_CLUSTER_BINDING_OK')
