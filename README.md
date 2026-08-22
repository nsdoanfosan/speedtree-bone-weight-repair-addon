# SpeedTree Assembly Add-on

Blender assembly and Unreal handoff tooling for the native FBX/XML outputs
produced by `speedtree_collision_cli`.

## Native skeleton contract

SpeedTree export owns the complete skeleton and skin data. Blender imports and
merges that data without changing:

- bone hierarchy;
- vertex-group names;
- per-vertex influences;
- per-vertex weight values.

If an FBX skeletal-data defect is found, it is fixed at the native exporter
boundary. Blender does not synthesize or replace skeletal data.

## Active pipeline

The one-button path is `SpeedTree -> Import -> Assemble`:

1. Export FBX, Raw XML, and the native runtime receipt through the configured
   SpeedTree CLI as one transactional cache bundle.
2. Import the FBX native skin data and preserve serializer geometry/local
   vertex identity for downstream Assembly binding.
3. Apply material, texture, instance-profile, and dummy-geometry contracts.
4. Join the already-skinned source meshes.
5. Build the `Export` collection hierarchy used by Send to Unreal.
6. Write MegaPlant grouping and optional Dynamic Wind JSON.
7. Optionally export the assembled skeletal FBX.

Cluster sources may use the dedicated Export-collection parking contract for
Cluster Normalizer ownership. That path changes export ownership only.

## Blender location

- Module: `speedtree_bone_weight_repair`
- Panel: `View3D > Sidebar > SpeedTree > Assembly`
- Installed junction:
  `C:\Users\PARK\AppData\Roaming\Blender Foundation\Blender\5.1\scripts\addons\speedtree_bone_weight_repair`

## Main controls

- `SPM`: source model for native export and metadata.
- `FBX Export INI` / `XML Export INI`: SpeedTree 10.1 presets.
- `Source FBX`: manual import/debug path.
- `Armature`: imported armature object, normally `Root`.
- `Mesh Regex`: optional assembly-source filter.
- `Export FBX`: optionally write the assembled skeletal FBX.
- `Build Export Structure`: create the Send to Unreal hierarchy.
- `Write MegaPlant JSON`: emit deterministic grouping metadata.

## Tests

```powershell
python -m pytest tests -q
```

One-shot timing benchmark:

```powershell
blender --factory-startup --background --python tools/benchmark_native_assembly.py -- <fbx> <spm>
```
