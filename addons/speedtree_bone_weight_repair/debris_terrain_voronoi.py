"""Deterministic adaptive XY Voronoi over whole, already connected leaf parts.

No Blender dependencies or geometry mutation. Twenty is the default work budget,
not a requested cluster count. Source min-Z spread is a contact proxy; it is not
a claim about contact with an arbitrary terrain surface.
"""
from __future__ import annotations

import math
import random
import statistics

SAFETY_GROUP_LIMIT = 256


def _distance2(a, b):
    return (a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2


def _contains(circle, point):
    return _distance2(circle, point) <= (circle[2] + 1e-9) ** 2


def _pair(a, b):
    return ((a[0] + b[0]) / 2, (a[1] + b[1]) / 2,
            math.sqrt(_distance2(a, b)) / 2)


def _triple(a, b, c):
    bx, by, cx, cy = b[0]-a[0], b[1]-a[1], c[0]-a[0], c[1]-a[1]
    divisor = 2*(bx*cy-by*cx)
    if abs(divisor) < 1e-15:
        return min((circle for circle in (_pair(a,b), _pair(a,c), _pair(b,c))
                    if all(_contains(circle, p) for p in (a,b,c))), key=lambda x:x[2])
    ux = a[0]+((bx*bx+by*by)*cy-(cx*cx+cy*cy)*by)/divisor
    uy = a[1]+(bx*(cx*cx+cy*cy)-cx*(bx*bx+by*by))/divisor
    return ux, uy, math.sqrt(_distance2((ux,uy), a))


def enclosing_circle(points):
    points = sorted(set(points))
    random.Random(20261001).shuffle(points)
    circle = None
    for i, point in enumerate(points):
        if circle is None or not _contains(circle, point):
            circle = (*point, 0.)
            for j in range(i):
                if not _contains(circle, points[j]):
                    circle = _pair(point, points[j])
                    for h in range(j):
                        if not _contains(circle, points[h]):
                            circle = _triple(point, points[j], points[h])
    if circle is None or not all(_contains(circle, p) for p in points):
        raise ValueError("Could not cover source XY footprint")
    return circle


def nearest_labels(centers, seeds):
    return [min(range(len(seeds)), key=lambda j:(_distance2(point,seeds[j]), j))
            for point in centers]


def _starts(centers):
    centroid = [statistics.mean(p[a] for p in centers) for a in range(2)]
    result = [min(range(len(centers)),key=lambda i:(_distance2(centers[i],centroid),i)),
              max(range(len(centers)),key=lambda i:(_distance2(centers[i],centroid),-i))]
    for a in range(2):
        result.extend([min(range(len(centers)),key=lambda i:(centers[i][a],i)),
                       max(range(len(centers)),key=lambda i:(centers[i][a],-i))])
    return list(dict.fromkeys(result))


def _lloyd(centers, weights, k, start):
    seeds = [centers[start][:]]
    closest = [_distance2(p, seeds[0]) for p in centers]
    for _ in range(1,k):
        i = max(range(len(centers)),key=lambda i:(closest[i],-i))
        if closest[i] <= 1e-20:
            break
        seeds.append(centers[i][:])
        closest = [min(d,_distance2(p,seeds[-1])) for d,p in zip(closest,centers)]
    previous = None
    for _ in range(100):
        labels = nearest_labels(centers,seeds)
        if labels == previous:
            break
        previous = labels
        for j in range(len(seeds)):
            indexes = [i for i,label in enumerate(labels) if label == j]
            if indexes:
                total = sum(weights[i] for i in indexes)
                seeds[j] = [sum(weights[i]*centers[i][a] for i in indexes)/total for a in range(2)]
    labels = nearest_labels(centers,seeds)
    used = sorted(set(labels),key=lambda j:min(i for i,value in enumerate(labels) if value==j))
    remap = {old:new for new,old in enumerate(used)}
    return [remap[value] for value in labels], [seeds[j] for j in used]


def _groups(parts, labels, seeds):
    result = []
    for label,seed in enumerate(seeds):
        indexes = [i for i,value in enumerate(labels) if value == label]
        low = [min(parts[i]["world_aabb_min_m"][a] for i in indexes) for a in range(3)]
        high = [max(parts[i]["world_aabb_max_m"][a] for i in indexes) for a in range(3)]
        corners = [(x,y) for i in indexes
                   for x in (parts[i]["world_aabb_min_m"][0],parts[i]["world_aabb_max_m"][0])
                   for y in (parts[i]["world_aabb_min_m"][1],parts[i]["world_aabb_max_m"][1])]
        circle = enclosing_circle(corners)
        contact_heights = [parts[i]["world_aabb_min_m"][2] for i in indexes]
        result.append({"group_id":label,"part_count":len(indexes),
                       "triangle_count":sum(parts[i]["triangle_count"] for i in indexes),
                       "voronoi_seed_xy_m":list(seed),
                       "mec_pivot_world_m":[circle[0],circle[1],low[2]],
                       "world_aabb_min_m":low,"world_aabb_max_m":high,
                       "mec_aabb_corner_radius_m":circle[2],
                       "source_contact_height_span_m":max(contact_heights)-min(contact_heights)})
    return result


def adaptive_voronoi(parts, *, max_groups=20, target_radius_cm=30., target_height_span_cm=10.):
    """Return the smallest passing group count, or the best budget-limited result."""
    if isinstance(max_groups,bool) or not isinstance(max_groups,int) or not 1<=max_groups<=SAFETY_GROUP_LIMIT:
        raise ValueError("max_groups must be an integer in 1..256; default budget is 20")
    if any(not math.isfinite(v) or v<=0 for v in (target_radius_cm,target_height_span_cm)):
        raise ValueError("Radius and source contact-height targets must be finite and positive")
    if not parts or [p["part_index"] for p in parts] != list(range(len(parts))):
        raise ValueError("Source parts require nonempty, contiguous part_index values")
    for part in parts:
        lo,hi = part["world_aabb_min_m"],part["world_aabb_max_m"]
        if len(lo)!=3 or len(hi)!=3 or any(not math.isfinite(v) for v in (*lo,*hi)):
            raise ValueError("Source bounds must contain finite XYZ values")
        if any(a>b for a,b in zip(lo,hi)) or part["triangle_count"]<0:
            raise ValueError("Source bounds/counts are invalid")
    centers = [[(p["world_aabb_min_m"][a]+p["world_aabb_max_m"][a])/2 for a in range(2)] for p in parts]
    areas = [(p["world_aabb_max_m"][0]-p["world_aabb_min_m"][0])*
             (p["world_aabb_max_m"][1]-p["world_aabb_min_m"][1]) for p in parts]
    floor = max(statistics.median(areas)*.25,1e-10)
    weights = [max(a,floor) for a in areas]
    radius_target,height_target = target_radius_cm/100,target_height_span_cm/100
    candidates = []
    chosen = None
    for requested in range(1,min(max_groups,len(parts))+1):
        trials = []
        for start in _starts(centers):
            labels,seeds = _lloyd(centers,weights,requested,start)
            groups = _groups(parts,labels,seeds)
            radius = max(g["mec_aabb_corner_radius_m"] for g in groups)
            span = max(g["source_contact_height_span_m"] for g in groups)
            quality = max(radius/radius_target,span/height_target)
            trials.append(((quality,radius,span,start),labels,seeds,groups))
        ranking,labels,seeds,groups = min(trials,key=lambda x:x[0])
        candidate = {"requested_groups":requested,"group_count":len(groups),
                     "maximum_radius_m":ranking[1],"maximum_source_contact_height_span_m":ranking[2],
                     "normalized_quality":ranking[0],"targets_met":ranking[0]<=1+1e-10}
        candidates.append((candidate,labels,seeds,groups))
        if candidate["targets_met"]:
            chosen = candidates[-1]
            break
    if chosen is None:
        chosen = min(candidates,key=lambda x:(x[0]["normalized_quality"],x[0]["group_count"],
                                              x[0]["maximum_radius_m"],x[0]["requested_groups"]))
    summary,labels,seeds,groups = chosen
    if labels != nearest_labels(centers,seeds) or len(labels)!=len(parts):
        raise ValueError("Voronoi assignment/whole-part preservation failed")
    return {"algorithm":"adaptive_xy_area_weighted_lloyd_mec_v1","max_groups":max_groups,
            "target_radius_cm":target_radius_cm,"target_height_span_cm":target_height_span_cm,
            "group_count":len(groups),"labels":labels,"groups":groups,
            "budget_insufficient":not summary["targets_met"],"targets_met":summary["targets_met"],
            "selected_candidate":summary,"candidates":[x[0] for x in candidates],
            "requested_budget_respected":len(groups)<=max_groups,
            "nearest_seed_assignment_verified":True,"no_parts_lost":True,
            "contact_proxy":"Original connected-part minimum-Z spread; no terrain contact guarantee",
            "weight_area_floor_m2":floor}
