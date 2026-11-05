"""작은 JSON 파일 저장소 — 원자적 쓰기(임시 파일 → rename), 프로세스 안 잠금."""

import json
import os
import tempfile
import threading


class JsonStore:
    def __init__(self, path, default):
        self.path = path
        self.default = default
        self.lock = threading.RLock()
        self._data = None

    def _load(self):
        if self._data is None:
            try:
                with open(self.path, encoding="utf-8") as f:
                    self._data = json.load(f)
            except (OSError, ValueError):
                self._data = json.loads(json.dumps(self.default))
        return self._data

    def read(self):
        with self.lock:
            return self._load()

    def save(self):
        with self.lock:
            data = self._load()
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            fd, tmp = tempfile.mkstemp(prefix=".tmp-", dir=os.path.dirname(self.path))
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    json.dump(data, f, ensure_ascii=False, indent=1)
                os.replace(tmp, self.path)
            except BaseException:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                raise

    def update(self, fn):
        """fn(data) 로 고치고 저장한다. fn 의 반환값을 돌려준다."""
        with self.lock:
            out = fn(self._load())
            self.save()
            return out
