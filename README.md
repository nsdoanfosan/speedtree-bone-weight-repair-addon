# SpeedTree Bone/Weight Repair Add-on

Blender add-on for repairing SpeedTree skeletal export data before sending it to Unreal Dynamic Wind workflows.

## Blender Location

- Repository path:
  `C:\Users\PARK\Documents\GitHub\speedtree-bone-weight-repair-addon`
- Add-on module: `speedtree_bone_weight_repair`
- Panel: `View3D > Sidebar > SpeedTree > Bone/Weight Repair`
- Installed by junction into Blender 5.1 user add-ons:
  `C:\Users\PARK\AppData\Roaming\Blender Foundation\Blender\5.1\scripts\addons\speedtree_bone_weight_repair`

## What It Fixes

This add-on is not hardcoded to `Branch_426`, `Cap_11`, or any single elm asset. Those were observed examples of a general data problem.

The repair pipeline handles four related failure cases:

1. Orphan branch root bones
   - SpeedTree FBX exports can leave many `_Start` bones with parent `-1`/no parent.
   - The add-on reads the original `.spm` file and recovers `Base` / `BaseRef` branch relationships.
   - It maps those SPM branches back to Blender bones by comparing branch coordinates to bone head/tail positions.
   - Only the configured true root, usually `Bone_1_Start`, remains a real root.

2. Disconnected loose leaf/cap meshes
   - Some meshes are separate instance objects, so they do not inherit usable skeletal deformation.
   - The add-on can rebuild those loose instances into one skinned mesh.
   - Each instance is assigned to a nearby valid branch bone using parent mesh weights and bone positions.

3. Invalid vertex groups / weights
   - Some vertices can be weighted to non-bone groups such as the armature object name.
   - The add-on preserves valid bone influences, normalizes them, removes invalid influences, and fills zero-weight vertices with the nearest valid bone.

4. Merged skeletal FBX export
   - The add-on merges valid skinned meshes into one mesh, preserves materials and UV layers, copies only valid bone weights, and exports a skeletal FBX.
   - FBX export disables generated leaf bones.

## Typical Use

1. Open the original `.blend`.
2. Enable `SpeedTree Bone/Weight Repair`.
3. In the panel, set:
   - `SPM`: matching original SpeedTree `.spm`
   - `Armature`: usually `Root`
   - `True Root`: usually `Bone_1_Start`
   - `Loose Mesh Filter`: usually `leaf`; adjust when loose cap/branch instance names need a different substring
4. Run `Run Full Repair Pipeline`.

Outputs are written next to the `.blend` unless `Output Directory` is set.

Default output names:

- `{blend}_fixed_codex.blend`
- `{blend}_fixed_codex_skinned_leaves.blend`
- `{blend}_fixed_codex_skinned_weights_fixed.blend`
- `{blend}_codex_merged_skinned_weights_fixed_export.blend`
- `{blend}_codex_merged_skinned_weights_fixed.fbx`
- JSON reports for each step

## Step Operators

The panel also exposes individual operators:

- `Reparent`: only repair bone parents from SPM.
- `Skin Loose`: only rebuild loose leaf/cap instance skinning.
- `Weights`: only repair invalid/zero vertex weights.
- `Merge/FBX`: only merge skinned meshes and optionally export FBX.

## Validation Command

```powershell
& "C:\Program Files\Blender Foundation\Blender 5.1\blender.exe" --factory-startup --background --python-expr "import addon_utils; addon_utils.enable('speedtree_bone_weight_repair', default_set=False); print('ENABLED')"
```
