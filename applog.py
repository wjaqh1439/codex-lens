"""운영 로그 — JSON Lines, 비동기 기록, 5MB×3 로테이션.

본문·비밀번호·토큰·쿠키는 넣지 않는다. 같은 이벤트가 초당 수십 번 오면 빈도를 제한한다.
"""

import json
import logging
import os
import queue
import threading
import time
from datetime import datetime, timezone
from logging.handlers import QueueHandler, QueueListener, RotatingFileHandler

_logger = logging.getLogger("codex-lens")
_logger.propagate = False
_logger.setLevel(logging.INFO)
_listener = None
_context = {}
_rate = {}
_rate_lock = threading.Lock()
RATE_PER_MIN = 120


def setup(path, **context):
    """로그 파일을 연다. context(버전·환경)는 모든 줄에 붙는다."""
    global _listener
    _context.clear()
    _context.update(context)
    for h in list(_logger.handlers):
        _logger.removeHandler(h)
    if _listener:
        _listener.stop()
        _listener = None
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fh = RotatingFileHandler(path, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8")
    fh.setFormatter(logging.Formatter("%(message)s"))
    q = queue.Queue(10000)
    _logger.addHandler(QueueHandler(q))
    _listener = QueueListener(q, fh)
    _listener.start()


def flush():
    if _listener:
        _listener.stop()
        _listener.start()


def _allowed(event):
    now = time.time()
    with _rate_lock:
        start, n = _rate.get(event, (now, 0))
        if now - start > 60:
            start, n = now, 0
        n += 1
        _rate[event] = (start, n)
        return n <= RATE_PER_MIN


def log(event, level="info", **fields):
    if not _logger.handlers or not _allowed(event):
        return
    rec = {"ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"), "event": event, "level": level}
    rec.update(_context)
    rec.update(fields)
    try:
        _logger.info(json.dumps(rec, ensure_ascii=False, default=str))
    except Exception:
        pass
