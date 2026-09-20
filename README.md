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

An explicit `<SPM stem>.rigid_generators.json` sidecar can preserve intentional
zero-bone generators. It contains `schema_version: 1`, the exact `spm_name`, and
unique `generator_guids`. Both bone-normalization policies validate these
generators and preserve their zero values. The native exporter binds their
vertices to the existing top-level deform bone, and the wind writer excludes
`Bone_1_Start` from simulation while preserving the full skeleton contract.
Exports without this sidecar retain their previous behavior.

For Perforce-managed SPM changes, set `SPEEDTREE_PERFORCE_RECOVERY=1` and
`SPEEDTREE_PERFORCE_CLIENT=ArtSources` (or the actual explicitly selected client).
The bundled verifier checks the exact source mapping and compares server bytes
with the saved SPM before either bone-normalization policy writes it. The default
recovery is the submitted have revision; `SPEEDTREE_PERFORCE_SHELF=<number>` can
select an already uploaded shelf. Missing or different server bytes stop the
operation. It never creates a shelf, submits, moves changelists, or restores files.
The receipt stores a `p4://` reference which is revalidated before cached reuse.
Do not use the legacy local-backup mode for this workspace's managed sources.

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

## Structural wind modifier export

The final-export writer computes `evaluated_structure_art_v4` from the current
SPM generator graph, evaluated native FBX/XML support data and final FBX bind
positions. It writes an independent `WindStructureModifierContract` sibling to
the normal Wind JSON and a detailed audit in the rich JSON. It preserves the
base simulation groups, Influence and GustAttenuation; root and excluded joints
remain neutral. Regenerating the contract replaces it rather than multiplying
previous gains. There are no asset-name correction tables.

The recipe is a bounded artistic approximation of bend amplitude, not measured
material dynamics. `BendRateScale`, `TorsionGain` and `FlutterGain` stay at 1;
leaf-aspect timing and physical damping are not implemented. Source or skeleton
errors must be checked in `SourceValidationStatus` and the rich audit.
SpeedTree Batch checks the installed recipe before selecting cached exports or
publishing a new handoff. Old Wind JSON requires Repair/export.

## Blender location

- Module: `speedtree_bone_weight_repair`
- Panel: `View3D > Sidebar > SpeedTree > Assembly`
- Installed junction:
  `C:\Users\PARK\AppData\Roaming\Blender Foundation\Blender\5.2\scripts\addons\speedtree_bone_weight_repair`

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
