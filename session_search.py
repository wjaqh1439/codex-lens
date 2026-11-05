"""세션 검색 — 목록에 보이는 제목과 폴더 이름으로 Codex·Claude 세션을 찾는다.

Codex app-server 의 `thread/list searchTerm` 은 검색에 쓰기 어렵다(0.165 실측): 대소문자를 가리고("Claude" 39건,
"CLAUDE" 3건), 첫 요청 전문(붙여 넣은 컨텍스트까지)에서 통째 문자열로만 찾으며("멘션 추천" 0건), 한 번에 2.5~4초가
걸린다. 그래서 Codex 목록은 이 앱이 한 번 끝까지 읽어 메모리에 두고 그 뒤로는 갱신된 앞쪽만 이어 받는다.
찾기는 띄어쓰기로 나눈 낱말이 모두 제목이나 폴더 이름에 들어 있는 세션이다(대소문자 무시, 순서 무관).
"""

import os
import threading
import time

import applog

PAGE = 100           # 목록을 읽는 한 쪽 크기
SYNC_SEC = 5         # 이 간격 안의 검색은 갱신분을 다시 받지 않는다(타자 치는 동안 매번 기다리지 않게)
REBUILD_SEC = 600    # 지워지거나 보관된 세션을 떨어내려고 이 간격마다 뒤에서 처음부터 다시 읽는다
BUILD_WAIT_SEC = 60  # 첫 색인을 기다리는 한도(1300여 개에 9초 남짓)


def session_title(th):
    """목록에 보이는 제목 — 이름이 있으면 이름, 없으면 첫 요청의 첫 줄."""
    preview = (th.get("preview") or "").strip()
    return (th.get("name") or "").strip() or preview.split("\n")[0][:120]


def terms(query):
    return [t for t in (query or "").lower().split() if t]


def matches(th, words):
    hay = "%s\n%s" % (session_title(th), os.path.basename((th.get("cwd") or "").rstrip("/")))
    hay = hay.lower()
    return all(w in hay for w in words)


class CodexIndex:
    """Codex 세션 목록의 메모리 사본. `request(params)` 는 `thread/list` 를 부르는 함수다."""

    def __init__(self, request, source_kinds):
        self.request = request
        self.source_kinds = list(source_kinds)
        self.items = {}            # id -> thread(turns 뺌)
        self.top = None            # 색인에 든 가장 늦은 updatedAt
        self.built_at = 0.0
        self.synced_at = 0.0
        self.sync_lock = threading.Lock()   # 앞쪽 갱신은 한 번에 하나
        self.build_lock = threading.Lock()  # 처음부터 읽기도 한 번에 하나
        self.ready = threading.Event()
        self.error = None

    def _pages(self, stop_before=None):
        cursor = None
        while True:
            p = {"limit": PAGE, "sourceKinds": self.source_kinds, "sortKey": "updated_at"}
            if cursor:
                p["cursor"] = cursor
            res = self.request(p)
            data = res.get("data") or []
            yield data
            cursor = res.get("nextCursor")
            if not cursor or not data:
                return
            if stop_before is not None and (data[-1].get("updatedAt") or 0) < stop_before:
                return

    @staticmethod
    def _slim(th):
        return {k: v for k, v in th.items() if k != "turns"}

    def remember(self, threads):
        """일반 목록에서 이미 받은 원본 Codex 행을 다음 검색에 바로 반영한다."""
        with self.sync_lock:
            for th in threads:
                tid = th.get("id")
                if not tid:
                    continue
                old = self.items.get(tid)
                if old and (old.get("updatedAt") or 0) > (th.get("updatedAt") or 0):
                    continue
                self.items[tid] = self._slim(th)

    def _build(self):
        if not self.build_lock.acquire(blocking=False):
            return
        t0 = time.monotonic()
        try:
            with self.sync_lock:
                known_before = dict(self.items)
            items = {}
            for data in self._pages():
                for th in data:
                    if th.get("id"):
                        items[th["id"]] = self._slim(th)
            with self.sync_lock:
                # 읽는 동안 목록·검색으로 받은 행은 유지한다. 예전 사본에만 남은 삭제 행은 떨어낸다.
                for tid, th in self.items.items():
                    changed = th is not known_before.get(tid)
                    if (tid in items or changed) and (tid not in items or (th.get("updatedAt") or 0) >= (items[tid].get("updatedAt") or 0)):
                        items[tid] = th
                self.items = items
                self.top = max((th.get("updatedAt") or 0 for th in items.values()), default=None)
                self.built_at = self.synced_at = time.monotonic()
                self.error = None
            applog.log("session_index_built", count=len(items), ms=int((time.monotonic() - t0) * 1000))
        except Exception as e:   # noqa: BLE001 — 색인이 실패해도 검색은 Claude 쪽으로라도 답한다
            self.error = str(e) or repr(e)
            applog.log("session_index_failed", level="warn", error=self.error[:300])
        finally:
            self.build_lock.release()
            self.ready.set()

    def warm(self):
        """처음 목록을 열 때 뒤에서 색인을 만들어 둔다 — 검색할 때는 이미 다 읽혀 있게."""
        if not self.ready.is_set() and not self.build_lock.locked():
            threading.Thread(target=self._build, daemon=True, name="session-index").start()

    def snapshot(self):
        """갱신분까지 반영한 목록. 첫 색인이 없으면 만들 때까지 기다린다."""
        if not self.ready.is_set():
            self.warm()
            self.ready.wait(BUILD_WAIT_SEC)
            if not self.ready.is_set():
                raise TimeoutError("Codex 세션 색인을 아직 만드는 중입니다")
        if not self.built_at:
            # 첫 색인이 실패했다 — 다음 검색에서 다시 해 본다
            err = self.error
            self.ready.clear()
            raise RuntimeError(err or "Codex 세션 색인을 만들지 못했습니다")
        now = time.monotonic()
        if now - self.built_at > REBUILD_SEC and not self.build_lock.locked():
            threading.Thread(target=self._build, daemon=True, name="session-index").start()
        with self.sync_lock:
            if now - self.synced_at >= SYNC_SEC:
                top = self.top
                for data in self._pages(top):
                    for th in data:
                        if th.get("id"):
                            self.items[th["id"]] = self._slim(th)
                            if self.top is None or (th.get("updatedAt") or 0) > self.top:
                                self.top = th.get("updatedAt") or 0
                self.synced_at = time.monotonic()
            return list(self.items.values())
