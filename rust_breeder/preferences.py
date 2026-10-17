"""Small, atomic application preferences kept beside the local project store."""
from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile

LANGUAGE_CODES = frozenset({'zh_CN', 'en', 'ru'})
MAX_PREFERENCE_BYTES = 64 * 1024


def _reject_constant(value):
    raise ValueError('Non-finite preference value.')


def load_preferences(path: Path) -> dict:
    try:
        with path.open('rb') as stream:
            content = stream.read(MAX_PREFERENCE_BYTES + 1)
        if len(content) > MAX_PREFERENCE_BYTES:
            return {}
        value = json.loads(content.decode('utf-8-sig'), parse_constant=_reject_constant)
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError, RecursionError):
        return {}


def preferred_language(path: Path) -> str:
    code = load_preferences(path).get('language', 'zh_CN')
    return code if isinstance(code, str) and code in LANGUAGE_CODES else 'zh_CN'


def save_language(path: Path, code: str) -> None:
    if not isinstance(code, str) or code not in LANGUAGE_CODES:
        raise ValueError('Unsupported language.')
    values = load_preferences(path)
    values['language'] = code
    content = json.dumps(values, ensure_ascii=False, indent=2, allow_nan=False)
    if len(content.encode('utf-8')) > MAX_PREFERENCE_BYTES:
        raise OSError('Preference file exceeds 64 KB; previous settings were kept.')
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=path.parent,
                                         prefix='.settings-', suffix='.tmp', delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
