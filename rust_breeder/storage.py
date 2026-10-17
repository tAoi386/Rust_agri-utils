"""Portable, versioned project storage. Project files are data, never executable."""

from __future__ import annotations

from copy import deepcopy
import json
import os
from pathlib import Path
import re
import tempfile
from typing import Any


PROJECT_VERSION = 1
MAX_FILE_BYTES = 2 * 1024 * 1024
MAX_FILE_WITH_CHECKPOINT_BYTES = 4 * 1024 * 1024
MAX_INVENTORY_ITEMS = 5000
_GENES = re.compile(r"[GYHWX]{6}\Z")
_CROPS = {"hemp", "blue", "red", "yellow", "green", "white", "black", "corn", "pumpkin", "potato"}


class ProjectError(ValueError):
    """A project is invalid, unsupported, or cannot be read."""


def _text(value: Any, label: str, *, maximum: int = 128, empty: bool = False) -> str:
    if not isinstance(value, str) or len(value) > maximum or (not empty and not value.strip()):
        raise ProjectError(f"{label}必须是{'非空' if not empty else ''}文本，最多 {maximum} 个字符。")
    return value


def _integer(value: Any, label: str, minimum: int, maximum: int) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise ProjectError(f"{label}必须是 {minimum}–{maximum} 的整数。")
    return value


def _genes(value: Any, label: str) -> str:
    if not isinstance(value, str) or _GENES.fullmatch(value) is None:
        raise ProjectError(f"{label}必须恰好包含 6 个大写字母，只能使用 G、Y、H、W、X。")
    return value


def _reject_constant(value: str) -> None:
    raise ProjectError(f"项目中不允许出现非有限数字 {value}。")


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ProjectError(f"项目中含有重复字段：{key}。")
        result[key] = value
    return result


def _without_checkpoint(data: dict[str, Any]) -> dict[str, Any] | None:
    """Recognize only the recovery snapshot's envelope, not arbitrary extras.

    The journey model verifies the snapshot's complete typed contents and its
    stock replay. Storage separately keeps the original project within its
    old size limit, so adding even a plausible snapshot cannot expand it.
    """
    base = dict(data)
    found = False
    for key in ("journey", "parallel"):
        state = data.get(key)
        checkpoint = state.get("undo_checkpoint") if isinstance(state, dict) else None
        if (not isinstance(checkpoint, dict)
                or not isinstance(checkpoint.get("inventory_before"), list)
                or not isinstance(checkpoint.get("pending"), dict)
                or not isinstance(checkpoint.get("after_inventory_fingerprint"), str)
                or (key == "journey" and not isinstance(checkpoint.get("completed_token"), str))):
            continue
        clean = dict(state)
        del clean["undo_checkpoint"]
        base[key] = clean
        found = True
    return base if found else None


def _project_payload(data: dict[str, Any], *, indent: int | None = None, newline: bool = False) -> bytes:
    suffix = "\n" if newline else ""
    payload = (json.dumps(data, ensure_ascii=False, indent=indent, allow_nan=False) + suffix).encode("utf-8")
    base = _without_checkpoint(data)
    if base is None:
        if len(payload) > MAX_FILE_BYTES:
            raise ProjectError("项目文件超过 2 MB，请减少项目记录。")
        return payload
    if len(payload) > MAX_FILE_WITH_CHECKPOINT_BYTES:
        raise ProjectError("含撤回快照的项目文件超过 4 MB，请减少项目记录。")
    base_payload = (json.dumps(base, ensure_ascii=False, indent=indent, allow_nan=False) + suffix).encode("utf-8")
    if len(base_payload) > MAX_FILE_BYTES:
        raise ProjectError("除去撤回快照后，项目记录仍超过 2 MB，请减少项目记录。")
    return payload


def validate_project(data: Any) -> dict[str, Any]:
    """Validate JSON-shaped data and return an independent canonical copy.

    Required schema: version, inventory, target, slots. Each inventory item has
    id, genes, quantity and optional name; slots is a nine-element list of null
    or {seed_id: str, remove: bool}. Optional crop, step, steps, target_mode and
    ui fields hold the editor's state. Unknown fields remain available to newer
    editors but are restricted to ordinary, finite JSON values by the encoder.
    """
    if not isinstance(data, dict):
        raise ProjectError("项目文件的最外层必须是对象。")
    version = data.get("version")
    if type(version) is not int or version != PROJECT_VERSION:
        raise ProjectError(f"不支持的项目版本：{version!r}；此程序支持版本 {PROJECT_VERSION}。")
    items = data.get("inventory")
    if not isinstance(items, list) or len(items) > MAX_INVENTORY_ITEMS:
        raise ProjectError(f"种子库必须是列表，最多 {MAX_INVENTORY_ITEMS} 项。")
    ids: set[str] = set()
    clean_items: list[dict[str, Any]] = []
    for index, item in enumerate(items, 1):
        if not isinstance(item, dict):
            raise ProjectError(f"第 {index} 项种子必须是对象。")
        seed_id = _text(item.get("id"), f"第 {index} 项种子的 ID")
        if seed_id in ids:
            raise ProjectError(f"种子 ID 重复：{seed_id}。")
        ids.add(seed_id)
        clean_item = dict(item)
        clean_item.update(
            id=seed_id,
            name=_text(item.get("name", ""), f"第 {index} 项种子的名称", maximum=200, empty=True),
            genes=_genes(item.get("genes"), f"第 {index} 项种子的基因"),
            quantity=_integer(item.get("quantity"), f"第 {index} 项种子的数量", 1, 1_000_000_000),
        )
        clean_items.append(clean_item)
    target = _genes(data.get("target"), "目标基因")
    step = _integer(data.get("step", 0), "当前步骤", 0, 4)
    slots = data.get("slots")
    if not isinstance(slots, list) or len(slots) != 9:
        raise ProjectError("种植箱必须恰好包含 9 个格子。")
    clean_slots: list[dict[str, Any] | None] = []
    quantities = {item["id"]: item["quantity"] for item in clean_items}
    used: dict[str, int] = {}
    for index, slot in enumerate(slots, 1):
        if slot is None:
            clean_slots.append(None)
            continue
        if not isinstance(slot, dict):
            raise ProjectError(f"第 {index} 格必须是空值或种子放置对象。")
        seed_id = _text(slot.get("seed_id"), f"第 {index} 格的种子 ID")
        if seed_id not in ids:
            raise ProjectError(f"第 {index} 格引用了种子库中不存在的种子：{seed_id}。")
        remove = slot.get("remove", False)
        if type(remove) is not bool:
            raise ProjectError(f"第 {index} 格的移除标记必须为布尔值。")
        if remove and index == 5:
            raise ProjectError("中心 5 号格是受体，不能标记为移除。")
        if remove and step < 3:
            raise ProjectError("读取杂交结果之前不能标记移除供体；请先完成前面的步骤。")
        used[seed_id] = used.get(seed_id, 0) + 1
        if used[seed_id] > quantities[seed_id]:
            raise ProjectError(f"种子 {seed_id} 的格子用量超过库存数量 {quantities[seed_id]}。")
        clean_slots.append(dict(slot, seed_id=seed_id, remove=remove))
    if "crop" in data:
        crop = _text(data["crop"], "作物品种", maximum=80)
        if crop not in _CROPS:
            raise ProjectError("项目包含不支持的作物品种。")
    if "steps" in data:
        if not isinstance(data["steps"], list) or len(data["steps"]) > 10000:
            raise ProjectError("步骤必须是列表，最多 10000 项。")
    if "target_mode" in data and data["target_mode"] not in ("exact", "counts"):
        raise ProjectError("目标匹配方式只能为 exact 或 counts。")
    if "ui" in data and not isinstance(data["ui"], dict):
        raise ProjectError("界面状态必须是对象。")
    if "seed_numbers" in data:
        numbers = data["seed_numbers"]
        if (not isinstance(numbers, dict) or len(numbers) > 20000
                or any(not isinstance(key, str) or not key or len(key) > 128
                       or type(value) is not int or not 1 <= value <= 1_000_000_000
                       for key, value in numbers.items())
                or len(set(numbers.values())) != len(numbers)):
            raise ProjectError("苗编号记录无效。")
    result = dict(data, version=PROJECT_VERSION, inventory=clean_items, target=target, slots=clean_slots)
    try:
        _project_payload(result)
    except (TypeError, ValueError, RecursionError) as exc:
        if isinstance(exc, ProjectError):
            raise
        raise ProjectError("项目中存在不支持的数据；只能保存普通 JSON 数据。") from exc
    return deepcopy(result)


def load_project(path: str | Path) -> dict[str, Any]:
    """Read a UTF-8 JSON project, rejecting malformed or unsupported data."""
    source = Path(path)
    try:
        with source.open("rb") as handle:
            content = handle.read(MAX_FILE_WITH_CHECKPOINT_BYTES + 1)
        if len(content) > MAX_FILE_WITH_CHECKPOINT_BYTES:
            raise ProjectError("项目文件超过 4 MB，请选择有效的项目文件。")
        data = json.loads(
            content.decode("utf-8-sig"),
            parse_constant=_reject_constant,
            object_pairs_hook=_unique_object,
        )
    except ProjectError:
        raise
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, RecursionError) as exc:
        raise ProjectError(f"无法读取项目文件：{exc}") from exc
    validated = validate_project(data)
    if _without_checkpoint(validated) is None and len(content) > MAX_FILE_BYTES:
        raise ProjectError("项目文件超过 2 MB，请选择有效的项目文件。")
    return validated


def save_project(path: str | Path, data: dict[str, Any]) -> None:
    """Validate first, then atomically replace a project in its own directory."""
    validated = validate_project(data)
    destination = Path(path).expanduser()
    temporary: Path | None = None
    try:
        payload = _project_payload(validated, indent=2, newline=True)
        with tempfile.NamedTemporaryFile(mode="wb", prefix=f".{destination.name}.", suffix=".tmp", dir=destination.parent, delete=False) as handle:
            temporary = Path(handle.name)
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, destination)
        temporary = None
    except OSError as exc:
        raise ProjectError(f"无法保存项目文件：{exc}") from exc
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
