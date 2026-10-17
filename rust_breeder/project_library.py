"""Local project list with atomic files and no user-selected save paths."""

from __future__ import annotations

from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
from uuid import uuid4

from .storage import ProjectError, load_project, save_project


class ProjectLibrary:
    def __init__(self, data_dir):
        self.root = Path(data_dir) / 'projects'
        self.index_path = self.root / 'library.json'
        self.entries = []
        self.current_id = ''
        if self.index_path.exists():
            try:
                if self.index_path.stat().st_size > 256 * 1024:
                    raise ValueError('项目列表过大。')
                index = json.loads(self.index_path.read_text(encoding='utf-8'))
                rows = index['projects']
                if not isinstance(rows, list) or len(rows) > 1000:
                    raise ValueError('项目列表无效。')
                ids = set()
                for row in rows:
                    project_id = row['id']
                    self.path(project_id)
                    if project_id in ids or not isinstance(row['name'], str) or not 1 <= len(row['name']) <= 80:
                        raise ValueError('项目列表含重复或无效记录。')
                    if not isinstance(row.get('updated', ''), str) or len(row.get('updated', '')) > 40:
                        raise ValueError('项目更新时间无效。')
                    if row.get('source_hash') and (not isinstance(row['source_hash'], str)
                            or re.fullmatch(r'[a-f0-9]{64}', row['source_hash']) is None):
                        raise ValueError('旧项目标记无效。')
                    ids.add(project_id)
                    self.entries.append(dict(row))
                self.current_id = index.get('current_id', '')
                if self.current_id and self.current_id not in ids:
                    raise ValueError('当前项目不存在。')
            except (OSError, ValueError, KeyError, TypeError) as exc:
                raise ProjectError(f'无法读取软件内项目列表：{exc}') from exc

    def path(self, project_id):
        if not isinstance(project_id, str) or re.fullmatch(r'[a-f0-9]{32}', project_id) is None:
            raise ProjectError('软件内项目编号无效。')
        return self.root / f'{project_id}.json'

    def _write_index(self):
        content = json.dumps({'version': 1, 'current_id': self.current_id, 'projects': self.entries},
                             ensure_ascii=False, indent=2)
        if len(content.encode('utf-8')) > 256 * 1024:
            raise ProjectError('软件内项目列表已满，请减少项目名称长度。')
        self.root.mkdir(parents=True, exist_ok=True)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile('w', encoding='utf-8', dir=self.root,
                                             suffix='.tmp', delete=False) as handle:
                temporary = Path(handle.name)
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.index_path)
        except OSError as exc:
            raise ProjectError(f'无法保存软件内项目列表：{exc}') from exc
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    def create(self, name, data, *, activate=True, source_hash=''):
        name = str(name).strip()[:80]
        if not name:
            raise ProjectError('项目名称不能为空。')
        if len(self.entries) >= 1000:
            raise ProjectError('软件内项目最多保存 1000 个。')
        project_id = uuid4().hex
        self.root.mkdir(parents=True, exist_ok=True)
        save_project(self.path(project_id), dict(data, local_project_id=project_id))
        entry = {'id': project_id, 'name': name, 'updated': datetime.now().isoformat(timespec='seconds')}
        if source_hash:
            entry['source_hash'] = source_hash
        self.entries.append(entry)
        previous_id = self.current_id
        if activate:
            self.current_id = project_id
        try:
            self._write_index()
        except Exception:
            self.entries.pop()
            self.current_id = previous_id
            raise
        return project_id

    def save(self, project_id, data):
        entry = next((row for row in self.entries if row['id'] == project_id), None)
        if entry is None:
            raise ProjectError('软件内项目不存在。')
        save_project(self.path(project_id), dict(data, local_project_id=project_id))
        entry['updated'] = datetime.now().isoformat(timespec='seconds')
        self.current_id = project_id
        self._write_index()

    def load(self, project_id):
        if not any(row['id'] == project_id for row in self.entries):
            raise ProjectError('软件内项目不存在。')
        return load_project(self.path(project_id))

    def name(self, project_id):
        return next((row['name'] for row in self.entries if row['id'] == project_id), '未命名项目')

    def import_legacy(self, paths):
        imported = 0
        for source in paths:
            source = Path(source)
            try:
                digest = hashlib.sha256(source.read_bytes()).hexdigest()
                if any(row.get('source_hash') == digest for row in self.entries):
                    continue
                data = load_project(source)
                self.create(source.stem, data, activate=False, source_hash=digest)
                imported += 1
            except (OSError, ValueError):
                # A corrupt optional old file must not discard the active work.
                continue
        return imported
