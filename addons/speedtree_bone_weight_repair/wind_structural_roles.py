"""Resolve scan/stitch trunk roles from the authored SPM graph, not names."""
import copy
import gzip
import hashlib
import xml.etree.ElementTree as ET
from pathlib import Path


def apply_scan_trunk_roles(bone_records, info, source_spm):
    if not source_spm:
        return bone_records, info
    path = Path(source_spm)
    raw = path.read_bytes()
    document = (gzip.decompress(raw) if raw.startswith(b'\x1f\x8b') else raw).decode('utf-8')
    root = ET.fromstring(document)
    sections = {}
    for tag in ('Generators', 'Links'):
        # Force/Extra can contain its own empty Generators element. Only the
        # direct SpeedTree children describe the authored generator graph.
        matches = root.findall('./' + tag)
        if len(matches) != 1:
            raise RuntimeError(f'Cannot classify wind structure: expected one {tag} in {path}')
        sections[tag] = matches[0]
    generators = list(sections['Generators'].findall('Generator'))
    by_id = {g.findtext('GUID'): g for g in generators}
    if len(by_id) != len(generators):
        raise RuntimeError('Cannot classify wind structure: duplicate generator GUID')
    parents = {}
    for link in sections['Links'].findall('Link'):
        child, parent = link.findtext('TargetGUID'), link.findtext('SourceGUID')
        if child in parents and parents[child] != parent:
            raise RuntimeError('Cannot classify wind structure: multiple generator parents')
        parents[child] = parent
    stems = []
    for gid, generator in by_id.items():
        if generator.get('Type') not in ('Branch', 'Spline Branch'):
            continue
        chain = [generator]
        current = gid
        for _ in range(3):
            current = parents.get(current)
            parent = by_id.get(current)
            if parent is None:
                break
            chain.append(parent)
        if [g.get('Type') for g in chain[1:]] == ['Stitch', 'BranchMesh', 'Tree']:
            name = generator.findtext('Name')
            # XML names are the exporter join key. A duplicate name is ambiguous.
            if sum(g.findtext('Name') == name for g in generators) != 1:
                raise RuntimeError(f'Ambiguous scan continuation generator: {name}')
            stems.append({'generator': name, 'guid': gid,
                          'source_chain': [{'name': g.findtext('Name'), 'type': g.get('Type')} for g in chain]})
    if not stems:
        return bone_records, info
    names = {s['generator'] for s in stems}
    source_records = [b for b in bone_records if b.get('generator') in names]
    if not source_records:
        return bone_records, info  # graph contains an unused authoring alternative
    roots = [b for b in bone_records if b.get('native_role') == 'fbx_wrapper_root']
    if len(roots) != 1 or roots[0]['parent_index'] != -1:
        raise RuntimeError('Scan trunk classification requires the exact native Root anchor')
    # The stem must begin at a direct child of the native root, not at a twig
    # imported from a similarly named cluster or a disconnected graph branch.
    active_names = {b['generator'] for b in source_records}
    for name in active_names:
        if not any(b['generator'] == name and b['parent_index'] == roots[0]['bone_index'] for b in source_records):
            raise RuntimeError(f'Scan continuation is not attached to native Root: {name}')
    result, metadata = copy.deepcopy(bone_records), copy.deepcopy(info)
    groups = metadata['simulation_groups']
    for group in groups:
        members = set(group.get('generators', []))
        if not members.intersection(active_names):
            continue
        if not members.issubset(active_names):
            raise RuntimeError('Cannot promote a mixed scan-stem/branch simulation group')
        group['is_trunk_group'] = True
        group['structural_role'] = 'scan_trunk_continuation'
    anchors = [g for g in groups if g.get('structural_role') == 'scan_trunk_fixed_anchor']
    if len(anchors) > 1:
        raise RuntimeError('Multiple scan trunk anchor groups require repair')
    if anchors:
        anchor_index = anchors[0]['index']
        if (anchors[0].get('generators') != ['NativeRootWrapper']
                or not anchors[0].get('is_trunk_group')
                or any(b.get('group') == anchor_index
                       and b.get('native_role') != 'fbx_wrapper_root' for b in result)):
            raise RuntimeError('Scan trunk anchor group contains non-root data')
    else:
        anchor_index = len(groups)
        groups.append({'index': anchor_index, 'generators': ['NativeRootWrapper'],
                       'is_trunk_group': True, 'bone_count': 1, 'mean_mass': 0.0,
                       'mean_radius': 0.0, 'structural_role': 'scan_trunk_fixed_anchor'})
    for bone in result:
        if bone.get('native_role') == 'fbx_wrapper_root':
            bone['group'] = anchor_index
            bone['structural_role'] = 'scan_trunk_fixed_anchor'
        elif bone.get('generator') in active_names:
            bone['structural_role'] = 'scan_trunk_continuation'
    metadata['structural_role_contract'] = {
        'schema_version': 1, 'source_spm': str(path.resolve()),
        'source_sha256': hashlib.sha256(raw).hexdigest(),
        'scan_stems': [s for s in stems if s['generator'] in active_names],
        'fixed_anchor_group': anchor_index,
        'note': 'Native scan/stitch skin stays fixed on Root; no bones or weights are synthesized.'}
    return result, metadata
