import itertools
import random
import unittest
from flowwrench.n8n_refs.core import Graph


class GraphTests(unittest.TestCase):
    def test_diamond_dominators(self):
        g = Graph(["Start", "A", "B", "Join", "End"], [("Start", "A"), ("Start", "B"), ("A", "Join"), ("B", "Join"), ("Join", "End")])
        self.assertTrue(g.dominates("Start", "End"))
        self.assertTrue(g.dominates("Join", "End"))
        self.assertFalse(g.dominates("A", "Join"))
        self.assertTrue(g.is_upstream("A", "Join"))
        self.assertFalse(g.is_upstream("Join", "A"))
        self.assertFalse(g.is_upstream("A", "A"))

    def test_multiple_entries(self):
        g = Graph(["A", "B", "C"], [("A", "C"), ("B", "C")])
        self.assertFalse(g.dominates("A", "C"))
        self.assertFalse(g.dominates("B", "C"))

    def test_loop_with_entry(self):
        g = Graph(["Start", "A", "B", "End"], [("Start", "A"), ("A", "B"), ("B", "A"), ("B", "End")])
        self.assertTrue(g.dominates("Start", "End"))
        self.assertTrue(g.dominates("A", "End"))
        self.assertFalse(g.dominates("B", "A"))

    def test_rootless_cycles_enter_at_every_member(self):
        g = Graph(["A", "B", "C", "D"], [("A", "B"), ("B", "A"), ("B", "C"), ("C", "D")])
        self.assertFalse(g.dominates("A", "B"))
        self.assertTrue(g.dominates("B", "C"))
        self.assertTrue(g.dominates("C", "D"))

    def test_trigger_entry_with_feedback(self):
        g = Graph(["A", "Trigger", "B"], [("A", "Trigger"), ("Trigger", "B")], ["Trigger"])
        self.assertFalse(g.dominates("A", "B"))
        self.assertTrue(g.dominates("Trigger", "B"))

    def test_long_chain_no_recursion(self):
        names = [str(i) for i in range(1100)]
        g = Graph(names, zip(names, names[1:]))
        self.assertTrue(g.dominates("0", "1099"))
        self.assertTrue(g.is_upstream("0", "1099"))

    def test_dominators_against_independent_path_enumeration(self):
        # Seeded DAGs: compare bitset algorithm with exhaustive root->node paths.
        rng = random.Random(17)
        for _ in range(50):
            names = list("ABCDEF")
            links = [(a, b) for a, b in itertools.combinations(names, 2) if rng.random() < .35]
            preds = {name: [a for a, b in links if b == name] for name in names}
            def paths(target):
                if not preds[target]:
                    return [{target}]
                return [p | {target} for parent in preds[target] for p in paths(parent)]
            g = Graph(names, links)
            for target in names:
                all_paths = paths(target)
                for source in names:
                    self.assertEqual(g.dominates(source, target), all(source in p for p in all_paths))

    def test_trigger_in_source_cycle_is_the_known_entry(self):
        g = Graph(["Trigger", "A", "B"], [("Trigger", "A"), ("A", "B"), ("B", "Trigger")], ["Trigger"])
        self.assertTrue(g.dominates("Trigger", "A"))
        self.assertTrue(g.dominates("A", "B"))
        self.assertFalse(g.dominates("B", "A"))

    def test_cyclic_dominators_against_vertex_removal(self):
        rng = random.Random(170)
        names = list("ABCDEF")
        for _ in range(50):
            links = list(zip(names, names[1:]))
            links += [(a, b) for a in names for b in names if a != b and rng.random() < .2]
            g = Graph(names, links, ["A"])
            def reachable_without(removed):
                reached = set() if removed == "A" else {"A"}
                todo = list(reached)
                while todo:
                    v = todo.pop()
                    for source, target in links:
                        if source == v and target != removed and target not in reached:
                            reached.add(target)
                            todo.append(target)
                return reached
            for source in names:
                reachable = reachable_without(source)
                for target in names:
                    self.assertEqual(g.dominates(source, target), target not in reachable)
