"""Local projects with atomic writes and identity-checked recovery copies."""

from __future__ import annotations

from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import tempfile
from uuid import uuid4

from .storage import (MAX_FILE_WITH_CHECKPOINT_BYTES, ProjectError,
                      load_project, save_project, _unique_object, _reject_constant)


class ProjectLibrary:
    def __init__(self, data_dir, *, recover_empty=False):
        self.root = Path(data_dir) / 'projects'
        self.index_path = self.root / 'library.json'
        self.entries = []
        self.current_id = ''
        self.recovery_messages = []
        if self.index_path.exists():
            index = None
            try:
                if self.index_path.stat().st_size > 256 * 1024:
                    raise ValueError('项目列表过大。')
                index = json.loads(self.index_path.read_text(encoding='utf-8-sig'),
                                   object_pairs_hook=_unique_object, parse_constant=_reject_constant)
                self._read_index(index)
            except (OSError, ValueError, KeyError, TypeError, RecursionError) as exc:
                self.entries, self.current_id = [], ''
                if not self._recover_index(index):
                    if not recover_empty:
                        raise ProjectError(f'无法读取软件内项目列表：{exc}') from exc
                    # Explicit new-project recovery only: never replace an
                    # unreadable index merely because the application started.
                    self._preserve_file(self.index_path)
                    self._write_index()
                    self.recovery_messages.append('已保留损坏项目文件并重建空项目列表，可创建新项目。')
        elif self.root.exists():
            # An interrupted first index write can leave intact project files.
            self._recover_index(None)

    def _read_index(self, index):
        if not isinstance(index, dict) or type(index.get('version')) is not int or index['version'] != 1:
            raise ValueError('项目列表版本无效。')
        rows = index['projects']
        if not isinstance(rows, list) or len(rows) > 1000:
            raise ValueError('项目列表无效。')
        entries = []
        ids = set()
        for row in rows:
            project_id = row['id']
            self.path(project_id)
            if project_id in ids or not isinstance(row['name'], str) or not 1 <= len(row['name'].strip()) <= 80:
                raise ValueError('项目列表含重复或无效记录。')
            if not isinstance(row.get('updated', ''), str) or len(row.get('updated', '')) > 40:
                raise ValueError('项目更新时间无效。')
            if row.get('source_hash') and (not isinstance(row['source_hash'], str)
                    or re.fullmatch(r'[a-f0-9]{64}', row['source_hash']) is None):
                raise ValueError('旧项目标记无效。')
            ids.add(project_id)
            entries.append(dict(row))
        current = index.get('current_id', '')
        if not isinstance(current, str) or (current and current not in ids):
            raise ValueError('当前项目不存在。')
        self.entries, self.current_id = entries, current

    def _preserve_file(self, path):
        """Keep the original bytes before replacing any damaged document."""
        if not path.exists():
            return
        recovery = self.root / 'recovery'
        recovery.mkdir(parents=True, exist_ok=True)
        destination = recovery / (path.name + '.' + uuid4().hex + '.bak')
        shutil.copy2(path, destination)

    def _recover_index(self, previous):
        candidates = {}
        previous_rows = previous.get('projects', []) if isinstance(previous, dict) else []
        previous_names = {}
        if isinstance(previous_rows, list):
            for row in previous_rows:
                if (isinstance(row, dict) and isinstance(row.get('id'), str)
                        and isinstance(row.get('name'), str) and 1 <= len(row['name'].strip()) <= 80):
                    previous_names[row['id']] = row['name']
        autosave = None
        try:
            autosave = load_project(self.root.parent / 'autosave.json')
        except (OSError, ValueError):
            pass
        for path in sorted(self.root.glob('*.json')):
            if re.fullmatch(r'[a-f0-9]{32}', path.stem) is None:
                continue
            try:
                data = load_project(path)
                if data.get('local_project_id', path.stem) != path.stem:
                    continue
                candidates[path.stem] = self._recovered_entry(path.stem, data, previous_names)
            except (OSError, ValueError):
                continue
        autosave_id = autosave.get('local_project_id', '') if autosave is not None else ''
        if isinstance(autosave_id, str) and re.fullmatch(r'[a-f0-9]{32}', autosave_id):
            if autosave_id not in candidates:
                candidates[autosave_id] = self._recovered_entry(autosave_id, autosave, previous_names)
        if not candidates or len(candidates) > 1000:
            return False
        previous_id = previous.get('current_id', '') if isinstance(previous, dict) else ''
        if isinstance(previous_id, str) and previous_id in candidates:
            current = previous_id
        elif isinstance(autosave_id, str) and autosave_id in candidates:
            current = autosave_id
        else:
            # Recover the list without guessing which of several projects was
            # active. The application can then present its recovery workflow.
            current = ''
        self._preserve_file(self.index_path)
        self.entries = list(candidates.values())
        self.current_id = current
        self._write_index()
        self.recovery_messages.append('项目列表已从有效项目和匹配的自动保存中恢复，原文件已备份。')
        return True

    def _recovered_entry(self, project_id, data, previous_names):
        name = data.get('local_project_name', previous_names.get(project_id, ''))
        if not isinstance(name, str) or not 1 <= len(name.strip()) <= 80:
            name = '恢复的项目 ' + project_id[:8]
        path = self.path(project_id)
        updated = datetime.fromtimestamp(path.stat().st_mtime).isoformat(timespec='seconds') if path.exists() else datetime.now().isoformat(timespec='seconds')
        entry = {'id': project_id, 'name': name, 'updated': updated}
        source_hash = data.get('local_project_source_hash')
        if isinstance(source_hash, str) and re.fullmatch(r'[a-f0-9]{64}', source_hash):
            entry['source_hash'] = source_hash
        return entry

    def path(self, project_id):
        if not isinstance(project_id, str) or re.fullmatch(r'[a-f0-9]{32}', project_id) is None:
            raise ProjectError('软件内项目编号无效。')
        return self.root / f'{project_id}.json'

    def _write_index(self):
        content = json.dumps({'version': 1, 'current_id': self.current_id, 'projects': self.entries},
                             ensure_ascii=False, indent=2, allow_nan=False)
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
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass

    def create(self, name, data, *, activate=True, source_hash=''):
        name = str(name).strip()[:80]
        if not name:
            raise ProjectError('项目名称不能为空。')
        if len(self.entries) >= 1000:
            raise ProjectError('软件内项目最多保存 1000 个。')
        if source_hash and (not isinstance(source_hash, str) or re.fullmatch(r'[a-f0-9]{64}', source_hash) is None):
            raise ProjectError('旧项目标记无效。')
        project_id = uuid4().hex
        self.root.mkdir(parents=True, exist_ok=True)
        document = dict(data, local_project_id=project_id, local_project_name=name)
        if source_hash:
            document['local_project_source_hash'] = source_hash
        save_project(self.path(project_id), document)
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
        document = dict(data, local_project_id=project_id, local_project_name=entry['name'])
        if entry.get('source_hash'):
            document['local_project_source_hash'] = entry['source_hash']
        save_project(self.path(project_id), document)
        previous_entry, previous_id = dict(entry), self.current_id
        entry['updated'] = datetime.now().isoformat(timespec='seconds')
        self.current_id = project_id
        try:
            self._write_index()
        except Exception:
            entry.clear()
            entry.update(previous_entry)
            self.current_id = previous_id
            raise

    def load(self, project_id, *, validator=None):
        if not any(row['id'] == project_id for row in self.entries):
            raise ProjectError('软件内项目不存在。')
        def read(path):
            data = load_project(path)
            if validator is not None:
                validator(data)
            return data
        path = self.path(project_id)
        try:
            data = read(path)
            if data.get('local_project_id', project_id) != project_id:
                raise ProjectError('项目内容与软件内项目编号不一致。')
            return data
        except (OSError, ValueError) as original:
            if project_id != self.current_id:
                raise
            try:
                recovered = read(self.root.parent / 'autosave.json')
                if recovered.get('local_project_id') != project_id:
                    raise ProjectError('自动保存与当前项目编号不一致，未替换原项目。')
            except (OSError, ValueError):
                raise original
            self._preserve_file(path)
            self.recovery_messages.append('当前项目已从匹配的自动保存中恢复，原项目文件已备份。')
            return recovered

    def name(self, project_id):
        return next((row['name'] for row in self.entries if row['id'] == project_id), '未命名项目')

    def import_legacy(self, paths):
        imported = 0
        for source in paths:
            source = Path(source)
            try:
                with source.open('rb') as stream:
                    content = stream.read(MAX_FILE_WITH_CHECKPOINT_BYTES + 1)
                if len(content) > MAX_FILE_WITH_CHECKPOINT_BYTES:
                    continue
                digest = hashlib.sha256(content).hexdigest()
                if any(row.get('source_hash') == digest for row in self.entries):
                    continue
                data = load_project(source)
                self.create(source.stem, data, activate=False, source_hash=digest)
                imported += 1
            except (OSError, ValueError):
                # A corrupt optional old file must not discard the active work.
                continue
        return imported
