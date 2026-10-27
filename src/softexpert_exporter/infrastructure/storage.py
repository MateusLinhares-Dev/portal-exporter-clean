from __future__ import annotations
import asyncio
import os
import re
import shutil
import uuid
from pathlib import Path, PurePosixPath
from softexpert_exporter.domain.models import StoredFile

_INVALID_CHARS = re.compile(r'[<>:"|?*\x00-\x1f]')

def sanitize_component(value: str, fallback: str = '_unnamed') -> str:
    normalized = _INVALID_CHARS.sub('_', value.strip().replace('/', '_').replace('\\', '_'))
    if normalized.strip(' .') in {'', '.', '..'}:
        return fallback
    return normalized.strip(' .')[:160]

def safe_relative_path(path: str) -> str:
    raw = PurePosixPath(path.replace('\\', '/'))
    if raw.is_absolute() or any(part in ('..', '.') for part in raw.parts):
        raise ValueError('Invalid relative path')
    parts = [sanitize_component(p) for p in raw.parts]
    if not parts:
        raise ValueError('Empty path')
    return '/'.join(parts)

class NetworkShareStorage:
    def __init__(self, root: Path, *, require_sentinel: bool, sentinel_name: str):
        self.root = root
        self.require_sentinel = require_sentinel
        self.sentinel_name = sanitize_component(sentinel_name)

    def _assert_ready(self) -> None:
        if not self.root.is_dir():
            raise OSError(f'Share directory missing: {self.root}')
        if self.require_sentinel and not (self.root / self.sentinel_name).is_file():
            raise OSError('Share sentinel missing; refusing local fallback')

    async def is_ready(self) -> bool:
        def probe() -> bool:
            try:
                self._assert_ready()
                probe_file = self.root / f'.write-test-{uuid.uuid4().hex}'
                try:
                    probe_file.write_text('ok', encoding='utf-8')
                finally:
                    probe_file.unlink(missing_ok=True)
                return True
            except OSError:
                return False
        return await asyncio.to_thread(probe)

    async def exists(self, relative_path: str) -> bool:
        target = self.root.joinpath(*safe_relative_path(relative_path).split('/'))
        return await asyncio.to_thread(target.is_file)

    async def store(self, source: Path, relative_path: str, sha256: str, size_bytes: int) -> StoredFile:
        name = safe_relative_path(relative_path)
        dst = self.root.joinpath(*name.split('/'))
        temp = dst.with_name(f'.{dst.name}.{uuid.uuid4().hex}.part')
        def copy() -> None:
            self._assert_ready()
            dst.parent.mkdir(parents=True, exist_ok=True)
            try:
                shutil.copyfile(source, temp)
                os.replace(temp, dst)
            finally:
                temp.unlink(missing_ok=True)
        await asyncio.to_thread(copy)
        return StoredFile(name, dst, sha256, size_bytes)
