"""Project files must remain valid and preserve previous saves on failure."""

from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from rust_breeder.engine import predict
from rust_breeder.journey import JourneyState, complete_round, prepare_round, start_journey, undo_last_round
from rust_breeder.models import InventoryItem, Recipe, Target
from rust_breeder.storage import (
    MAX_FILE_BYTES, MAX_FILE_WITH_CHECKPOINT_BYTES,
    ProjectError, load_project, save_project, validate_project,
)


class ProjectStorageTests(unittest.TestCase):
    def setUp(self):
        self.workspace = Path(__file__).resolve().parents[1]
        self.temporary = tempfile.TemporaryDirectory(prefix=".storage-test-", dir=self.workspace)
        self.folder = Path(self.temporary.name).resolve()
        # Cleanup may recursively remove only the freshly created test directory.
        self.assertTrue(self.folder.is_relative_to(self.workspace))
        self.assertNotEqual(self.folder, self.workspace)
        self.addCleanup(self.temporary.cleanup)
        self.path = self.folder / "种子项目.json"
        self.project = {
            "version": 1,
            "inventory": [
                {"id": "seed-a", "name": "第一株", "genes": "YYYGGG", "quantity": 2},
                {"id": "seed-b", "name": "供体", "genes": "GYYHWX", "quantity": 1},
            ],
            "target": "YYYYGG",
            "target_mode": "counts",
            "crop": "hemp",
            "step": 3,
            "slots": [
                {"seed_id": "seed-b", "remove": True},
                None, None, None,
                {"seed_id": "seed-a", "remove": False},
                None, None, None, None,
            ],
        }

    def test_unicode_round_trip_preserves_project_and_returns_independent_data(self):
        save_project(self.path, self.project)
        loaded = load_project(self.path)
        self.assertEqual(loaded, self.project)
        self.assertIn("第一株", self.path.read_text(encoding="utf-8"))
        loaded["inventory"][0]["quantity"] = 10
        self.assertEqual(self.project["inventory"][0]["quantity"], 2)
        self.assertEqual(load_project(self.path)["inventory"][0]["quantity"], 2)

    def test_invalid_references_and_ids_are_rejected(self):
        missing_reference = deepcopy(self.project)
        missing_reference["slots"][0]["seed_id"] = "not-in-inventory"
        duplicate_id = deepcopy(self.project)
        duplicate_id["inventory"][1]["id"] = "seed-a"
        for invalid in (missing_reference, duplicate_id):
            with self.subTest(invalid=invalid), self.assertRaises(ProjectError):
                validate_project(invalid)

    def test_slot_allocation_counts_every_placement_against_inventory(self):
        exact = deepcopy(self.project)
        exact["slots"][0] = {"seed_id": "seed-a", "remove": True}
        self.assertEqual(validate_project(exact), exact)
        for marked in (False, True):
            overallocated = deepcopy(exact)
            overallocated["slots"][1] = {"seed_id": "seed-a", "remove": marked}
            with self.subTest(marked=marked), self.assertRaisesRegex(ProjectError, "超过库存"):
                validate_project(overallocated)

    def test_removal_flags_protect_center_and_donors_before_observation(self):
        center_removed = deepcopy(self.project)
        center_removed["slots"][4]["remove"] = True
        with self.assertRaisesRegex(ProjectError, "中心"):
            validate_project(center_removed)
        for step in (0, 1, 2):
            early = dict(self.project, step=step)
            with self.subTest(step=step), self.assertRaisesRegex(ProjectError, "读取杂交结果之前"):
                validate_project(early)
        missing_step = dict(self.project)
        del missing_step["step"]
        with self.assertRaises(ProjectError):
            validate_project(missing_step)

    def test_unknown_json_extensions_are_preserved(self):
        extended = deepcopy(self.project)
        extended["future_options"] = {"note": "可扩展", "enabled": True}
        extended["inventory"][0]["future_label"] = "种子扩展"
        extended["slots"][0]["future_style"] = {"color": "red"}
        save_project(self.path, extended)
        self.assertEqual(load_project(self.path), extended)

    def test_schema_rejects_bad_genes_quantities_slots_and_versions(self):
        invalid_projects = []
        for genes in ("YYYGG", "YYYGGGG", "yyyggg", "YYYY?G"):
            item = deepcopy(self.project)
            item["inventory"][0]["genes"] = genes
            invalid_projects.append(item)
        for quantity in (0, -1, 1.5, True, "2"):
            item = deepcopy(self.project)
            item["inventory"][0]["quantity"] = quantity
            invalid_projects.append(item)
        invalid_projects.extend([
            dict(self.project, version=2),
            dict(self.project, version=True),
            dict(self.project, slots=[None] * 8),
            dict(self.project, target="GGGGG?"),
            dict(self.project, target_mode="unknown"),
            dict(self.project, step=5),
            dict(self.project, crop="unknown"),
        ])
        marker = deepcopy(self.project)
        marker["slots"][0]["remove"] = "false"
        invalid_projects.append(marker)
        for invalid in invalid_projects:
            with self.subTest(invalid=invalid), self.assertRaises(ProjectError):
                validate_project(invalid)

    def test_invalid_save_never_changes_existing_project(self):
        save_project(self.path, self.project)
        before = self.path.read_bytes()
        invalid = deepcopy(self.project)
        invalid["inventory"][0]["quantity"] = 0
        with self.assertRaises(ProjectError):
            save_project(self.path, invalid)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(list(self.folder.iterdir()), [self.path])

    def test_failed_atomic_replace_keeps_old_file_and_removes_temporary_file(self):
        save_project(self.path, self.project)
        before = self.path.read_bytes()
        updated = deepcopy(self.project)
        updated["inventory"][0]["quantity"] = 9
        with patch("rust_breeder.storage.os.replace", side_effect=PermissionError("file is busy")):
            with self.assertRaises(ProjectError):
                save_project(self.path, updated)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(list(self.folder.iterdir()), [self.path])

    def test_corrupted_json_and_nonfinite_values_are_rejected(self):
        contents = [
            b"not JSON",
            b"[]",
            b'{"version":1,"version":1}',
            b'{"version":NaN}',
            b"\xff\xfe",
        ]
        for content in contents:
            with self.subTest(content=content):
                self.path.write_bytes(content)
                with self.assertRaises(ProjectError):
                    load_project(self.path)

    def completed_project(self, count=4):
        inventory = tuple(InventoryItem(f"seed-{index}",
                                        ("GYYGGG", "YHYGGG", "YGGGGG")[index] if index < 3 else "WHYGXW",
                                        2 if index == 0 else 1, "苗" * 200)
                          for index in range(count))
        target = Target("YYYGGG", "counts")
        recipe = Recipe(inventory[0], inventory[1:3],
                        predict(inventory[0].genes, [row.genes for row in inventory[1:3]]), target)
        ready = prepare_round(start_journey(inventory[0].id, target), inventory, recipe)
        result = complete_round(ready, inventory, "YYYGGG", 1)
        project = {"version": 1, "target": target.genes, "target_mode": target.mode,
                   "slots": [None] * 9,
                   "inventory": [{"id": row.id, "genes": row.genes, "quantity": row.quantity,
                                  "name": row.name} for row in result.inventory],
                   "journey": result.state.to_dict()}
        return project, inventory, result

    def test_large_existing_project_can_save_reload_and_undo_its_full_checkpoint(self):
        project, original, result = self.completed_project(2500)
        base = deepcopy(project)
        del base["journey"]["undo_checkpoint"]
        save_project(self.path, base)
        self.assertLess(self.path.stat().st_size, MAX_FILE_BYTES)
        save_project(self.path, project)
        self.assertGreater(self.path.stat().st_size, MAX_FILE_BYTES)
        self.assertLess(self.path.stat().st_size, MAX_FILE_WITH_CHECKPOINT_BYTES)
        loaded = load_project(self.path)
        self.assertEqual(loaded, project)
        state = JourneyState.from_dict(loaded["journey"])
        inventory = tuple(InventoryItem(**row) for row in loaded["inventory"])
        undone = undo_last_round(state, inventory)
        self.assertEqual(sorted((row.id, row.genes, row.quantity, row.name) for row in undone.inventory),
                         sorted((row.id, row.genes, row.quantity, row.name) for row in original))
        self.assertEqual(undone.state.pending, result.state.undo_checkpoint.pending)
        self.assertEqual(undone.state.rounds, ())
        self.assertIsNone(undone.state.undo_checkpoint)

    def test_checkpoint_cannot_expand_the_original_project_limit(self):
        project, _, _ = self.completed_project()
        project["notes"] = "x" * MAX_FILE_BYTES
        with self.assertRaisesRegex(ProjectError, "除去撤回快照.*2 MB"):
            validate_project(project)
        save_project(self.path, self.project)
        before = self.path.read_bytes()
        with self.assertRaisesRegex(ProjectError, "2 MB"):
            save_project(self.path, project)
        self.assertEqual(self.path.read_bytes(), before)
        self.path.write_text(json.dumps(project, ensure_ascii=False), encoding="utf-8")
        with self.assertRaisesRegex(ProjectError, "2 MB"):
            load_project(self.path)

    def test_small_fake_checkpoint_cannot_bypass_base_project_limit(self):
        for checkpoint in (
            {},
            None,
            [],
            {"inventory_before": [], "pending": {}, "after_inventory_fingerprint": "", "completed_token": ""},
        ):
            project = deepcopy(self.project)
            project["journey"] = {"undo_checkpoint": checkpoint}
            project["notes"] = "x" * MAX_FILE_BYTES
            with self.subTest(checkpoint=checkpoint), self.assertRaisesRegex(ProjectError, "2 MB"):
                validate_project(project)

    def test_incomplete_checkpoint_envelope_does_not_receive_extra_file_allowance(self):
        project = deepcopy(self.project)
        project["journey"] = {"undo_checkpoint": {"padding": "x" * MAX_FILE_BYTES}}
        with self.assertRaisesRegex(ProjectError, "2 MB"):
            validate_project(project)
        with self.assertRaisesRegex(ProjectError, "2 MB"):
            save_project(self.path, project)

    def test_total_checkpoint_file_is_limited_to_four_megabytes(self):
        project, _, _ = self.completed_project()
        project["journey"]["undo_checkpoint"]["future_padding"] = "x" * MAX_FILE_WITH_CHECKPOINT_BYTES
        with self.assertRaisesRegex(ProjectError, "4 MB"):
            validate_project(project)
        with self.assertRaisesRegex(ProjectError, "4 MB"):
            save_project(self.path, project)
        self.path.write_bytes(b" " * (MAX_FILE_WITH_CHECKPOINT_BYTES + 1))
        with self.assertRaisesRegex(ProjectError, "4 MB"):
            load_project(self.path)

    def test_large_whitespace_file_without_checkpoint_retains_two_megabyte_limit(self):
        content = json.dumps(self.project, ensure_ascii=False).encode("utf-8")
        self.path.write_bytes(b" " * MAX_FILE_BYTES + content)
        with self.assertRaisesRegex(ProjectError, "2 MB"):
            load_project(self.path)

    def test_pretty_printing_cannot_exceed_either_real_saved_file_limit(self):
        # Compact validation succeeds; the actual indented save must still
        # obey the same limit and preserve an existing file on failure.
        project, _, _ = self.completed_project()
        base = deepcopy(project)
        del base["journey"]["undo_checkpoint"]
        project["padding"] = "x" * (MAX_FILE_BYTES - len(json.dumps(base, ensure_ascii=False).encode("utf-8")) - 30)
        validate_project(project)
        save_project(self.path, self.project)
        before = self.path.read_bytes()
        with self.assertRaisesRegex(ProjectError, "2 MB"):
            save_project(self.path, project)
        self.assertEqual(self.path.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
