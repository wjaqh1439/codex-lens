"""수신한 텍스트의 최근 속도. 토크나이저 없이 UTF-8 4바이트를 1토큰으로 추정한다.

실제 모델 토큰 수·추론 속도가 아니며 도구 출력과 대기 시간은 포함하지 않는다.
본문은 보관하지 않고 시각과 누적 바이트 수만 최대 128개 보관한다.
"""
from collections import deque

WINDOW_SEC = 3
IDLE_SEC = 2
MIN_SEC = 0.5


class StreamSpeed:
    def __init__(self):
        self.clear()

    def clear(self):
        self.key = None
        self.samples = deque(maxlen=128)
        self.total = 0

    def add(self, key, text, now):
        if not text:
            return
        if key != self.key or (self.samples and not 0 <= now - self.samples[-1][0] < IDLE_SEC):
            self.clear()
            self.key = key
        self.total += len(text.encode('utf-8'))
        # 재접속 때 한꺼번에 재생된 조각을 속도로 세지 않도록 짧은 버스트는 한 표본으로 묶는다.
        if self.samples and now - self.samples[-1][0] < 0.1:
            self.samples[-1] = (self.samples[-1][0], self.total)
        else:
            self.samples.append((now, self.total))
        while len(self.samples) > 2 and self.samples[1][0] <= now - WINDOW_SEC:
            self.samples.popleft()

    def snapshot(self, now):
        if len(self.samples) < 3:
            return None
        first, last = self.samples[0], self.samples[-1]
        span = last[0] - first[0]
        if span < MIN_SEC or not 0 <= now - last[0] < IDLE_SEC:
            return None
        return {"tps": round((last[1] - first[1]) / 4 / span, 1),
                "at": last[0], "expires_at": last[0] + IDLE_SEC, "estimated": True}
