"""Small, atomic application preferences kept beside the local project store."""
from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile

LANGUAGE_CODES = frozenset({'zh_CN', 'en', 'ru'})


def load_preferences(path: Path) -> dict:
    try:
        if path.stat().st_size > 64 * 1024:
            return {}
        value = json.loads(path.read_text(encoding='utf-8-sig'))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def preferred_language(path: Path) -> str:
    code = load_preferences(path).get('language', 'zh_CN')
    return code if isinstance(code, str) and code in LANGUAGE_CODES else 'zh_CN'


def save_language(path: Path, code: str) -> None:
    if code not in LANGUAGE_CODES:
        raise ValueError('Unsupported language.')
    values = load_preferences(path)
    values['language'] = code
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=path.parent,
                                         prefix='.settings-', suffix='.tmp', delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(values, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
