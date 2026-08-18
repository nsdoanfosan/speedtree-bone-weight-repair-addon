# SpeedTree Bone/Weight Repair Add-on

Blender add-on for repairing SpeedTree skeletal export data before sending it to Unreal Dynamic Wind workflows.

## MegaPlant Conversion Roadmap

This repository is the working place for a staged SpeedTree-to-MegaPlant conversion process. The stages below are the intended order of work and should stay visible as the add-on grows.

### Cluster source contract

SK Batch can mark a repaired `Cluster/SK_*.spm` export with
`cluster_source_skin_contract`. In that mode the add-on uses the matching Raw
XML structural roots selected from the first rendered Branch geometry below
each Tree root; Generator names and prototype counts are report data, not
selection rules. Meshless placement splines are excluded.

- Existing multi-axis weights are preserved and normalized by connected deform
  cluster.
- A completely unskinned source is rigid-bound only when the XML proves exactly
  one render-root axis. Multi-axis membership is never guessed.
- The repaired unsuffixed Full SK mesh stays in `SpeedTree_Source`.
  Cluster Normalizer-generated ordinal pivots (`*_01`, `*_02`, ...) are the
  only objects exposed through `Export`, including the one-prototype case.

### Pipeline Boundary

The Blender add-on owns deterministic conversion artifacts:

- Repaired armature hierarchy derived from the source `.spm`.
- Generated 3D branch and leaf replacement geometry.
- Valid skin weights, material slots, UVs, and skeletal FBX export.
- JSON metadata that describes where generated data came from and how it should be grouped.
- A Blender viewport preview of the same JSON group classifications before Unreal import.

The Unreal project owns runtime behavior:

- Importing the exported FBX/JSON into a separate test path first.
- Reading grouping metadata for MegaPlant-style setup.
- Driving TreeWind / DynamicWind actors, preview wind flags, gust behavior, and runtime material/actor parameters.

Do not use the conversion JSON as a scratchpad for live Unreal wind tuning. Runtime fixes in Unreal should not require regenerating the FBX or rewriting source conversion JSON unless the geometry grouping or binding contract changes.

The add-on assigns one immutable response category (`TREE`, `BUSH`, `WEED`, or
`NONE`) and exports the per-group basis Unreal needs. Unreal exposes one shared
numeric response profile per category; changing `TREE` affects every imported
mesh assigned to `TREE`, never one Skeletal Mesh override. `NONE` follows the
same contract and merely starts with zero-valued defaults. These asset-response
profiles are separate from level/weather wind speed, amplitude, direction, and
gust controls.

### SpeedTree Export Input Recommendation

For MegaPlant JSON grouping, prefer a SpeedTree FBX export that preserves useful mesh/object hierarchy. The final Blender output can still become one merged skeletal mesh, but the JSON should be captured from the repaired source objects before that merge happens.

Material-based splitting is still useful when material names clearly identify trunk, branch, frond, leaf, cap, or other categories. Hierarchy/object splitting is better when different tree parts share a material but still need separate JSON groups.

Whichever split is used, preserve:

- The armature and all bone names.
- Valid vertex groups / skin weights.
- Stable material slots for trunk, branch, frond, leaf, cap, or other asset categories.
- Enough object/material names for reports and debugging.

The pipeline now snapshots JSON grouping after parent/skinning/weight repair and before the final merge. This keeps hierarchy/material grouping metadata useful even when the final export object is one merged `SK_*_Codex_MergedSkinned_WeightsFixed` mesh.

For hierarchy-style SpeedTree exports such as `*_hi.fbx`, use `Grouping Mode: Hierarchy`. This classifies by object/mesh/parent names before the final merge, so `Branch_*`, `Big_*`, `Bifurcating_*`, `Cap_*`, `Leaf_*`, `Roots_*`, and `Trunk` objects remain meaningful in JSON.

Cap geometry should not be grouped as leaves. SpeedTree documentation describes caps as extensions that close branch breaks or open branch ends, while fronds are meshes placed along a branch spine. Therefore the default grouping treats `Cap_*` as branch geometry, not leaf geometry.

1. **Repair branch bone hierarchy**
   - Source data: original SpeedTree `.spm` plus the imported Blender armature.
   - Problem: SpeedTree exports can produce branch `_Start` bones whose parent relationship is effectively missing.
   - Output: a repaired armature where Base/BaseRef-backed branches are connected, while authored independent roots remain sibling stems under the exported FBX armature-object root. All later generated geometry can bind to stable bones.

2. **Parked note: convert branch/frond cards to 3D branch clusters**
   - Source data: SpeedTree XML `Frond_*` records, branch atlas material IDs, and pre-grouped 3D cluster meshes.
   - Previous prototype: match the card by name/cutout/material data, then place the corresponding `branch_elm_01_A/B/C` style 3D cluster at the original card start point.
   - Current decision: this is not part of the active add-on UI/operator set. Keep it as a reference only unless the workflow returns to this direction.
   - If revived later: source-card bone weight transfer must be solved before it is useful for Unreal.

3. **Convert leaf cards/instances to 3D leaf geometry**
   - Source data: leaf material records, leaf instance transforms, and any original 3D leaf source meshes.
   - Process: likely separate from branch clusters because leaves may be exported as instances rather than the same frond-card mesh structure.
   - Required output: 3D leaf replacements skinned to the repaired existing bones.

4. **Merge and export skinned 3D geometry**
   - Source data: repaired armature, repaired trunk/branch meshes, and active generated 3D replacements.
   - Process: preserve valid bone weights/materials/UVs, remove invalid groups, fill zero-weight vertices, and export a skeletal FBX.
   - Required rule: generated 3D geometry must bind to the repaired original bones, not to temporary placement objects.

5. **Emit Unreal/MegaPlant JSON grouping data**
   - Source data: the repaired pre-merge Blender source objects plus SpeedTree/MegaPlant grouping requirements.
   - Process: inspect the existing JSON format, group trunk/branch/frond/leaf data correctly, and write metadata Unreal can use for MegaPlant-style setup.
   - Required output: skeletal mesh export plus deterministic JSON grouping data that stays traceable back to the conversion stages.
   - Pipeline rule: `Run Full Repair Pipeline` writes JSON from a post-repair, pre-merge grouping snapshot. Do not classify the final merged mesh as one object and call that the tree grouping.
   - Preview rule: Blender's JSON group preview uses the same grouping mode and regex rules, but it previews the current scene state. The final pipeline JSON is captured after repair and before merge.
   - Required schema direction:
     - Source provenance: `.spm`, XML, `.blend`, cluster library, export timestamps/tool version.
     - Repaired skeleton summary: true root, recovered parent links, unresolved/orphan bone counts.
     - Branch replacement records only if that parked direction is revived: source `Frond_*`, selected cluster asset, material/atlas match, transform, source weight donor, generated object name.
     - Leaf replacement records: source leaf/card/instance identifier, generated mesh/object, transform, binding bone or copied weight source.
     - Validation summary: invalid groups removed, zero-weight vertices filled, unbound generated vertices, export file paths.
     - Runtime hints only when stable: grouping names, expected Unreal actor/controller class names, and import labels. Per-session wind strength/speed/gust tuning belongs in Unreal.

6. **Validate Unreal import/runtime behavior**
   - Source data: exported skeletal FBX plus the conversion JSON.
   - Process: import into a separate Unreal test path without touching existing production assets.
   - Required checks: repaired bone hierarchy imports as one usable skeleton, generated branch/leaf geometry stays skinned, JSON groups resolve back to imported geometry, and TreeWind / DynamicWind behavior uses the Unreal actor/controller as the authority.
   - Runtime rule: preview wind or editor-only flags must not fight the TreeWind/DynamicWind source, and the default motion should avoid hard sine-like stepping or abrupt on/off transitions.

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
   - A parent is created only when a matching `Base` / `BaseRef` pair provides positive attachment evidence.
   - If an SPM contains no `BaseRef` records, its FBX root bones remain independent siblings. Unreal receives them beneath the exported armature-object root instead of collapsing every stem under the first deforming bone.

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

5. Parked 3D branch cluster placement note
   - Branch frond cards can be replaced with pre-grouped 3D cluster meshes such as `branch_elm_01_A/B/C_mesh`.
   - The add-on reads the SpeedTree XML export to match `Frond_*` object names and branch atlas material IDs.
   - This path is currently parked and not exposed as an active operator.
   - The previous prototype reached placement only; source-card bone weight transfer was not completed.

## Typical Use

1. Open Blender and enable `SpeedTree Bone/Weight Repair`.
2. In the panel, set:
   - `Source FBX`: original SpeedTree FBX
   - `SPM`: matching original SpeedTree `.spm`
   - `Armature`: usually `Root`
   - `True Root`: usually `Bone_1_Start`
   - `Loose Mesh Filter`: usually `leaf`; adjust when loose cap/branch instance names need a different substring
3. Run `Import Source FBX` if the source FBX is not already in the scene.
4. In `JSON Grouping / Preview`, set the grouping mode and regex rules for trunk/branch/frond/leaf/other.
5. Run `Preview JSON Groups` and check the viewport colors.
6. Run `Run Full Repair Pipeline`.

Outputs are written next to the `.blend` unless `Output Directory` is set.

Default output names:

- `{blend}_fixed_codex.blend`
- `{blend}_fixed_codex_skinned_leaves.blend`
- `{blend}_fixed_codex_skinned_weights_fixed.blend`
- `{blend}_codex_merged_skinned_weights_fixed_export.blend`
- `{blend}_codex_merged_skinned_weights_fixed.fbx` only when `Export FBX` is enabled
- `{blend}_megaplant_tree_groups.json`
- JSON reports for each step

## Step Operators

The panel also exposes individual operators:

- `Reparent`: only repair bone parents from SPM.
- `Skin Loose`: only rebuild loose leaf/cap instance skinning.
- `Weights`: only repair invalid/zero vertex weights.
- `Preview JSON Groups`: color the Blender viewport by the exact group rules that will be written to the MegaPlant tree grouping JSON.
- `Write JSON Only`: write the MegaPlant tree grouping JSON without exporting FBX.
- `Merge/FBX`: only merge skinned meshes and optionally export FBX.

## JSON Group Preview

The preview is not a cosmetic material pass. It is a viewport view of the grouping rules that will be used for `{blend}_megaplant_tree_groups.json`.

`Run Full Repair Pipeline` writes the JSON from a repaired pre-merge snapshot, then creates the final merged mesh. This avoids classifying the final merged object as one giant trunk/branch/leaf group just because one regex matched first.

This JSON is intentionally separate from any material binding JSON used by Send to Unreal or the Unreal material pipeline. Do not merge tree grouping data and material binding data into one file.

Grouping modes:

- `Hierarchy`: classify by object, mesh, and parent hierarchy names. This is the preferred mode for SpeedTree hierarchy exports such as `*_hi.fbx`.
- `Material`: classify by material slot names. This is the preferred mode when SpeedTree export is split by material.
- `Object`: classify by object and mesh names.
- `Material + Object`: classify by material names first, then object/mesh names.

The regex fields control the actual handoff groups:

- `Trunk Regex`
- `Branch Regex`
- `Frond Regex`
- `Leaf Regex`
- `Other Regex`

Default hierarchy grouping treats `Trunk` and `Roots*` as trunk, `Branch*`, `Big*`, `Bifurcating*`, `Cap*`, `Root_Twigs*`, `Cavity*`, `Knot*`, and `Decal*` as branch/wood geometry, `Leaf*` as leaf, and `Frond*` as frond. `Cap*` is intentionally excluded from the leaf regex, and the armature object name `Root` is not used as a grouping candidate.

For material-based modes, the preview tints material viewport colors so a merged mesh with multiple material slots can still show multiple JSON groups. For object mode, the preview uses object viewport colors. The operator also writes `codex_json_group` custom properties onto previewed objects or materials for inspection.

## Parked 3D Branch Cluster Notes

The branch cluster replacement work is currently parked. It is kept as notes only so the previous investigation is not lost, but it should not be treated as part of the active conversion pipeline.

Current data contract:

- Input tree XML: SpeedTree XML export containing `Frond_*` objects and triangle material IDs.
- Input cluster blend: grouped cluster `.blend` containing meshes named like `branch_elm_01_A_mesh`, `branch_elm_01_B_mesh`, and `branch_elm_01_C_mesh`.
- Output collection: `Codex_NameMatched_BranchCluster_Replacements` by default.
- Output report: `{blend}_branch_cluster_replacement_report_codex.json`.

Current limitation:

- The Elm test XML currently maps all branch frond cards to the first atlas island, so all 214 replacements resolve to `A`.
- The replacement objects were placed and named by the prototype, but not yet skinned to the source frond card bone weights.

Revival requirement:

- Transfer the original `Frond_*` card vertex group influence structure onto each 3D replacement cluster so the replacements deform under the existing SpeedTree armature like MegaPlant-style branch geometry.

## Unreal Integration Contract

The handoff to Unreal should consist of:

- A skeletal FBX containing the repaired armature and final skinned trunk/branch/leaf geometry.
- A conversion JSON report containing grouping, provenance, replacement, binding, and validation metadata.
- Optional import labels or stable names that let Unreal scripts find the generated parts without guessing.

The handoff should not contain:

- Live TreeWind strength, speed, gust, or preview wind override values that were only used for a local test.
- Unreal actor state copied back into Blender-side source data.
- Runtime fixes that silently mutate the conversion JSON after import.

Unreal-side validation should use a separate test content path first. Existing production assets should only be touched after the FBX/JSON import, grouping lookup, and DynamicWind/TreeWind interaction are verified together.

## Validation Command

```powershell
& "C:\Program Files\Blender Foundation\Blender 5.1\blender.exe" --factory-startup --background --python-expr "import addon_utils; addon_utils.enable('speedtree_bone_weight_repair', default_set=False); print('ENABLED')"
```
