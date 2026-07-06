# Parked 3D Branch Cluster prototype notes.
#
# This module intentionally contains no executable implementation.
#
# The previous prototype tried to replace SpeedTree `Frond_*` branch cards with
# pre-grouped 3D branch cluster meshes such as `branch_elm_01_A/B/C_mesh`.
# That direction is currently not part of the active add-on workflow.
#
# If this work is revived later, the missing requirement is source-card weight
# transfer: generated cluster vertices must inherit usable influences from the
# original frond/card mesh and deform under the repaired SpeedTree armature.
#
# Keep the active pipeline focused on:
# - SPM-based branch bone hierarchy repair.
# - Loose leaf/cap skin reconstruction when needed.
# - Invalid/zero weight repair.
# - Merged skeletal FBX export.
# - Deterministic JSON metadata generated at export time.
