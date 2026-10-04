import importlib.util
import json
from pathlib import Path
import unittest

ROOT=Path(__file__).resolve().parents[1]
SPEC=importlib.util.spec_from_file_location("terrain_voronoi",ROOT/"addons/speedtree_bone_weight_repair/debris_terrain_voronoi.py")
module=importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(module)


def part(index,low,high):
    return {"part_index":index,"world_aabb_min_m":low,"world_aabb_max_m":high,"triangle_count":2}


class AdaptiveVoronoiTests(unittest.TestCase):
    def test_small_source_needs_one_group_not_default_twenty(self):
        rows=[part(0,[0.,0.,0.],[.04,.04,.01]),part(1,[.05,0.,.01],[.09,.04,.02])]
        result=module.adaptive_voronoi(rows)
        self.assertEqual(result["group_count"],1)
        self.assertFalse(result["budget_insufficient"])

    def test_whole_parts_are_voronoi_assigned_and_budget_reported(self):
        rows=[part(i,[float(i),0.,0.],[i+.01,.01,.01]) for i in range(4)]
        result=module.adaptive_voronoi(rows,max_groups=2,target_radius_cm=1.)
        self.assertLessEqual(result["group_count"],2)
        self.assertTrue(result["budget_insufficient"])
        centers=[[(p["world_aabb_min_m"][a]+p["world_aabb_max_m"][a])/2 for a in range(2)] for p in rows]
        self.assertEqual(result["labels"],module.nearest_labels(centers,[g["voronoi_seed_xy_m"] for g in result["groups"]]))
        self.assertEqual(sum(g["part_count"] for g in result["groups"]),4)

    def test_height_target_participates_in_selection(self):
        rows=[part(0,[0.,0.,0.],[.02,.02,.01]),part(1,[.08,0.,.2],[.1,.02,.21])]
        result=module.adaptive_voronoi(rows,target_height_span_cm=1.)
        self.assertEqual(result["group_count"],2)
        self.assertTrue(result["targets_met"])

    def test_configurable_budget_is_distinct_from_safety_limit(self):
        rows=[part(i,[float(i),0.,0.],[i+.01,.01,.01]) for i in range(21)]
        result=module.adaptive_voronoi(rows,max_groups=21,target_radius_cm=1.)
        self.assertEqual(result["group_count"],21)
        self.assertTrue(result["requested_budget_respected"])
        for invalid in (0,257,True):
            with self.assertRaises(ValueError):
                module.adaptive_voronoi(rows,max_groups=invalid)

    def test_09_10_audit_fixtures_are_generic_and_repeatable(self):
        fixture=json.loads((ROOT/"tests/fixtures/debris_parts_09_10.fixture").read_text())
        observed=[]
        for source in fixture["sources"]:
            result=module.adaptive_voronoi(source["parts"])
            repeated=module.adaptive_voronoi(source["parts"])
            self.assertEqual(result,repeated)
            self.assertEqual(sum(g["triangle_count"] for g in result["groups"]),source["triangles"])
            self.assertEqual(sum(g["part_count"] for g in result["groups"]),source["part_count"])
            self.assertTrue(result["requested_budget_respected"])
            observed.append(result["group_count"])
        self.assertNotEqual(observed,[20,20])


if __name__=="__main__":
    unittest.main()
