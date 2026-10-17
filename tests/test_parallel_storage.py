"""Large concurrent-planter recovery snapshots retain the base file limits."""

from copy import deepcopy
from dataclasses import asdict
import json
from pathlib import Path
import tempfile
import unittest

from rust_breeder.engine import predict
from rust_breeder.journey import (
    JourneyState, complete_round, prepare_round, start_journey, undo_last_round,
)
from rust_breeder.models import InventoryItem, Recipe, Target
from rust_breeder.parallel_models import (
    ParallelState, ParallelStep, ParallelWave, prepare_wave, complete_wave, undo_wave,
)
from rust_breeder.storage import (
    MAX_FILE_BYTES, MAX_FILE_WITH_CHECKPOINT_BYTES,
    ProjectError, load_project, save_project, validate_project,
)


class ParallelCheckpointStorageTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="parallel-storage-", dir=Path(__file__).resolve().parents[1] / "artifacts")
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / "project.json"

    def inventory(self, count, *, quantities=(2, 1, 1)):
        return tuple(InventoryItem(
            f"seed-{index}", ("GYYGGG", "YHYGGG", "YGGGGG")[index] if index < 3 else "WHYGXW",
            quantities[index] if index < 3 else 1, "苗" * 200,
        ) for index in range(count))

    def recipe(self, inventory):
        by_id = {row.id: row for row in inventory}
        center = by_id["seed-0"]
        donors = tuple(by_id[f"seed-{index}"] for index in (1, 2))
        return Recipe(center, donors, predict(center.genes, [row.genes for row in donors]), Target())

    def completed(self, inventory):
        recipe = self.recipe(inventory)
        wave = ParallelWave(1, (ParallelStep(1, recipe, "YYYGGG", "parallel-output", 1),))
        ready = prepare_wave(ParallelState(), inventory, wave)
        return complete_wave(ready, inventory, {1: ("YYYGGG", 3)})

    def project(self, result, *, journey=None):
        data = {"version": 1, "inventory": [asdict(row) for row in result.inventory],
                "slots": [None] * 9, "target": "YYYGGG", "target_mode": "counts",
                "parallel": result.state.to_dict()}
        if journey is not None:
            data["journey"] = journey.to_dict()
        return data

    def stock(self, inventory):
        return sorted((row.id, row.genes, row.quantity, row.name) for row in inventory)

    def test_large_parallel_checkpoint_saves_loads_and_atomically_undoes(self):
        original = self.inventory(2500)
        completion = self.completed(original)
        project = self.project(completion)
        base = deepcopy(project)
        del base["parallel"]["undo_checkpoint"]
        save_project(self.path, base)
        self.assertLess(self.path.stat().st_size, MAX_FILE_BYTES)
        save_project(self.path, project)
        self.assertGreater(self.path.stat().st_size, MAX_FILE_BYTES)
        self.assertLess(self.path.stat().st_size, MAX_FILE_WITH_CHECKPOINT_BYTES)
        loaded = load_project(self.path)
        self.assertEqual(loaded, project)
        state = ParallelState.from_dict(loaded["parallel"])
        rows = tuple(InventoryItem(**row) for row in loaded["inventory"])
        restored = undo_wave(state, rows)
        self.assertEqual(self.stock(restored.inventory), self.stock(original))
        self.assertEqual(restored.state.pending, completion.state.undo_checkpoint.pending)
        self.assertEqual(restored.state.waves, ())
        self.assertIsNone(restored.state.undo_checkpoint)
        with self.assertRaises(ValueError):
            undo_wave(restored.state, restored.inventory)

    def test_parallel_snapshot_does_not_expand_the_original_base_limit(self):
        project = self.project(self.completed(self.inventory(4)))
        project["notes"] = "x" * MAX_FILE_BYTES
        small = self.project(self.completed(self.inventory(4)))
        save_project(self.path, small)
        before = self.path.read_bytes()
        with self.assertRaisesRegex(ProjectError, "除去撤回快照.*2 MB"):
            validate_project(project)
        with self.assertRaisesRegex(ProjectError, "2 MB"):
            save_project(self.path, project)
        self.assertEqual(self.path.read_bytes(), before)
        self.path.write_text(json.dumps(project, ensure_ascii=False), encoding="utf-8")
        with self.assertRaisesRegex(ProjectError, "2 MB"):
            load_project(self.path)

    def test_incomplete_parallel_checkpoint_has_no_extra_allowance(self):
        for checkpoint in ({}, None, [], {"padding": "x" * MAX_FILE_BYTES}):
            project = self.project(self.completed(self.inventory(4)))
            project["parallel"]["undo_checkpoint"] = checkpoint
            project["notes"] = "x" * MAX_FILE_BYTES
            with self.subTest(checkpoint_type=type(checkpoint).__name__), self.assertRaisesRegex(ProjectError, "2 MB"):
                validate_project(project)

    def test_parallel_total_limit_is_four_megabytes(self):
        project = self.project(self.completed(self.inventory(4)))
        project["parallel"]["undo_checkpoint"]["padding"] = "x" * MAX_FILE_WITH_CHECKPOINT_BYTES
        with self.assertRaisesRegex(ProjectError, "4 MB"):
            validate_project(project)
        with self.assertRaisesRegex(ProjectError, "4 MB"):
            save_project(self.path, project)

    def test_real_pretty_printed_base_limit_preserves_existing_save(self):
        project = self.project(self.completed(self.inventory(4)))
        base = deepcopy(project)
        del base["parallel"]["undo_checkpoint"]
        project["padding"] = "x" * (MAX_FILE_BYTES - len(json.dumps(base, ensure_ascii=False).encode("utf-8")) - 30)
        validate_project(project)
        small = self.project(self.completed(self.inventory(4)))
        save_project(self.path, small)
        before = self.path.read_bytes()
        with self.assertRaisesRegex(ProjectError, "2 MB"):
            save_project(self.path, project)
        self.assertEqual(self.path.read_bytes(), before)

    def test_both_modes_keep_full_snapshots_and_can_undo_in_reverse_order(self):
        original = self.inventory(1700, quantities=(3, 2, 2))
        recipe = self.recipe(original)
        ready = prepare_round(start_journey(recipe.center_id, recipe.target), original, recipe)
        sequential = complete_round(ready, original, "YYYGGG", 1)
        parallel = self.completed(sequential.inventory)
        project = self.project(parallel, journey=sequential.state)
        base = deepcopy(project)
        del base["journey"]["undo_checkpoint"]
        del base["parallel"]["undo_checkpoint"]
        save_project(self.path, base)
        self.assertLess(self.path.stat().st_size, MAX_FILE_BYTES)
        save_project(self.path, project)
        self.assertGreater(self.path.stat().st_size, MAX_FILE_BYTES)
        self.assertLess(self.path.stat().st_size, MAX_FILE_WITH_CHECKPOINT_BYTES)
        loaded = load_project(self.path)
        restored_parallel = undo_wave(ParallelState.from_dict(loaded["parallel"]),
                                     tuple(InventoryItem(**row) for row in loaded["inventory"]))
        self.assertEqual(self.stock(restored_parallel.inventory), self.stock(sequential.inventory))
        restored_sequential = undo_last_round(JourneyState.from_dict(loaded["journey"]), restored_parallel.inventory)
        self.assertEqual(self.stock(restored_sequential.inventory), self.stock(original))


if __name__ == "__main__":
    unittest.main()
