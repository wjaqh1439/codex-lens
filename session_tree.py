"""세션 사이의 부모·자식 — 세션 안에서 만든 세션을 목록에서 부모 아래에 둔다.

부모를 아는 곳(추측하지 않는다 — 제목·폴더·시각이 비슷하다는 이유로 묶지 않는다):
- Codex 하위 에이전트: 스레드의 `parentThreadId`(옛 판은 `source.subAgent.thread_spawn.parent_thread_id`).
- 이 앱에서 돈 턴의 명령: 명령(`claude -p`·`codex exec`·API 호출 등)의 출력에 세션 ID 가
  나왔고 그 세션이 **그 명령이 도는 동안** 만들어졌으면 명령을 돌린 세션이 부모다(`launches.json`).
"""

import json
import os
import re

from store import JsonStore

# 세션 ID — UUID(대시 있음) 또는 대시 없는 32자. 명령 구간 안에 생긴 세션만 자식으로 치므로 다른 해시가 걸려도 무해하다
UUID_RE = re.compile(r"\b(?:[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}|[0-9a-fA-F]{32})\b")
LAUNCH_KEEP = 3000
LAUNCH_SLACK_SEC = 3


def thread_parent(th):
    """Codex 스레드(또는 같은 모양)의 부모 스레드 ID 와 하위 에이전트 이름표."""
    parent = th.get("parentThreadId")
    nick = th.get("agentNickname")
    role = th.get("agentRole")
    src = th.get("source")
    if isinstance(src, dict):
        sub = src.get("subAgent") or src.get("subagent")
        spawn = sub.get("thread_spawn") if isinstance(sub, dict) else None
        if isinstance(spawn, dict):
            parent = parent or spawn.get("parent_thread_id")
            nick = nick or spawn.get("agent_nickname")
            role = role or spawn.get("agent_role")
    return parent or None, (nick or role or None)


class Launches:
    """이 앱에서 돈 턴의 명령 출력에 나온 세션 ID 와 그 명령이 돈 구간. ID 마다 구간을 몇 개까지 둔다."""

    def __init__(self, path):
        self.store = JsonStore(path, {"items": {}})

    def observe(self, parent, text, start, end):
        ids = {i.lower() for i in UUID_RE.findall(text or "")}
        ids.discard((parent or "").lower())
        if not parent or not ids:
            return 0
        win = [parent, round(start, 1), round(end, 1)]

        def f(d):
            items = d.setdefault("items", {})
            added = 0
            for i in ids:
                wins = items.setdefault(i, [])
                if not any(w[0] == parent and w[1] <= start <= w[2] + 1 for w in wins):
                    wins.append(win)
                    del wins[:-3]
                    added += 1
            if len(items) > LAUNCH_KEEP:
                for k in sorted(items, key=lambda k: items[k][-1][2])[:len(items) - LAUNCH_KEEP]:
                    items.pop(k, None)
            return added
        if all(any(w[0] == parent and w[1] <= start for w in self.store.read()["items"].get(i, [])) for i in ids):
            return 0   # 같은 명령의 출력이 또 들어왔다 — 파일을 다시 쓰지 않는다
        return self.store.update(f)

    def parent_of(self, ident, created):
        """그 ID 의 세션이 만들어진 시각(created, 초)을 품는 명령 구간의 세션. 없으면 None."""
        if not ident or not created:
            return None
        for parent, start, end in reversed(self.store.read()["items"].get(ident.lower(), [])):
            if start - LAUNCH_SLACK_SEC <= created <= end + LAUNCH_SLACK_SEC and parent != ident:
                return parent
        return None


def group(items, seen, parent_meta):
    """목록 한 쪽(갱신 시각 순 메타)을 부모 아래로 묶는다.

    - 부모가 같은 쪽에 있으면 그 `children` 에 넣는다. 묶음은 그 안에서 가장 늦게 갱신된 세션 자리에 선다
      (자식이 돌고 있으면 오래된 부모도 위로 온다).
    - 부모를 앞 쪽에서 이미 냈으면(`seen`) 자식만 낸다 — 화면이 이미 있는 부모 아래에 붙인다.
    - 부모가 아직 안 나왔으면 `parent_meta(id)` 로 읽어 자식 자리에 부모를 끌어올린다(못 읽으면 자식을 그대로 둔다).
    `seen` 은 이번에 낸 ID 를 더해 돌려준다 — 다음 쪽에서 같은 세션을 다시 내지 않는다.
    """
    items = [dict(m) for m in items if m["id"] not in seen]
    by_id = {m["id"]: m for m in items}

    def cyclic(m):
        cur, hops = m, 0
        while cur and cur.get("parent") and hops < 16:
            cur = by_id.get(cur["parent"])
            hops += 1
            if cur is m:
                return True
        return False

    out = []

    def place(m, depth=0):
        p = m.get("parent")
        if not p or p == m["id"] or depth > 6 or cyclic(m):
            out.append(m)
        elif p in by_id:
            by_id[p].setdefault("children", []).append(m)
        elif p in seen:
            out.append(m)
        else:
            pm = parent_meta(p)
            if not pm:
                out.append(m)
                return
            pm = dict(pm, children=[m])
            by_id[p] = pm
            place(pm, depth + 1)

    for m in items:
        place(m)
    order = {m["id"]: i for i, m in enumerate(items)}

    def rank(m):
        kids = m.get("children") or []
        kids.sort(key=rank)
        return min([order.get(m["id"], len(items))] + [rank(c) for c in kids])

    out.sort(key=rank)
    seen.update(by_id)
    return out
