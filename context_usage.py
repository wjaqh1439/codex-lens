"""마지막 API 호출이 채운 컨텍스트. 턴·세션 누적 토큰과 캐시는 더하지 않는다."""

import math


def count(value):
    return int(value) if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value >= 0 else None


def context_usage(usage):
    last = usage.get("last") or {}
    tokens = count(last.get("totalTokens"))
    if tokens is None:
        inp, out = count(last.get("inputTokens")), count(last.get("outputTokens"))
        if inp is None or out is None:
            return None
        tokens = inp + out
    window = count(usage.get("modelContextWindow"))
    window = window or None
    return {"tokens": tokens, "window": window,
            "percent": round(tokens * 100 / window, 1) if window else None}
