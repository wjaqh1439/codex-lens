"""프로젝트 탐색기(폴더 트리)와 문서 다듬기 — .md 에 남긴 코멘트대로 에이전트(Codex·Claude Code)에게 문서를 고치게 한다.

다듬기는 프로젝트 폴더에서 새 Codex 세션(또는 연결된 세션의 다음 턴)으로 보낸다. Codex 가 파일을 직접 고치고,
턴이 끝나면 고치기 전 글과 비교해 바뀐 줄을 적어 둔다. 고치기 전 글을 남겨 두어 되돌릴 수 있다.
"""

import difflib
import os
import subprocess
import threading
import time
import uuid

import applog
from codex_rpc import RpcError
from store import JsonStore

REFINE_MARK = "[codex-lens · 문서 다듬기]"
OPEN_STATES = ("none", "input")
BEFORE_LIMIT = 512 * 1024
ANSWER_LIMIT = 6000
KEEP_PER_DOC = 10


def _now():
    return time.time()


def _read(path):
    with open(path, encoding="utf-8", errors="replace") as f:
        return f.read()


def _mtime(path):
    try:
        return os.path.getmtime(path)
    except OSError:
        return None


def git_root(path):
    d = path if os.path.isdir(path) else os.path.dirname(path)
    try:
        out = subprocess.run(["git", "-C", d, "rev-parse", "--show-toplevel"], capture_output=True, text=True, timeout=5)
        if out.returncode == 0 and out.stdout.strip():
            return out.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        pass
    return None


def inside(root, path):
    root = os.path.abspath(root)
    path = os.path.abspath(path)
    return path == root or path.startswith(root.rstrip(os.sep) + os.sep)


class Docs:
    def __init__(self, lens):
        self.lens = lens
        self.refines = JsonStore(os.path.join(lens.home, "md-refines.json"), {"items": []})
        self.lock = threading.Lock()
        lens.turn_listeners.append(self._on_turn_done)

    # --- 탐색기 ---------------------------------------------------------
    def tree(self, root, rel=""):
        root = os.path.abspath(os.path.expanduser(root or ""))
        if not os.path.isdir(root):
            raise ValueError("폴더가 없습니다: %s" % root)
        rel = os.path.normpath(rel or "").replace(os.sep, "/")
        if rel in (".", ""):
            rel = ""
        if rel.startswith("../") or rel == ".." or os.path.isabs(rel):
            raise ValueError("프로젝트 밖 경로입니다")
        files = self.lens.project_files(root)
        prefix = rel + "/" if rel else ""
        dirs, names = {}, []
        for f in files:
            if not f.startswith(prefix):
                continue
            rest = f[len(prefix):]
            if "/" in rest:
                d = rest.split("/", 1)[0]
                dirs[d] = dirs.get(d, 0) + 1
            elif rest:
                names.append(rest)
        counts = self.comment_counts(root)
        out = []
        for d in sorted(dirs, key=str.casefold):
            r = prefix + d
            out.append({"name": d, "rel": r, "path": os.path.join(root, r), "kind": "dir", "files": dirs[d],
                        "comments": counts.get(r, 0)})
        for n in sorted(names, key=str.casefold):
            r = prefix + n
            p = os.path.join(root, r)
            try:
                st = os.stat(p)
            except OSError:
                continue      # git 에는 있는데 지운 파일
            out.append({"name": n, "rel": r, "path": p, "kind": "file", "size": st.st_size, "mtime": st.st_mtime,
                        "comments": counts.get(r, 0)})
        if rel and not out and not os.path.isdir(os.path.join(root, rel)):
            raise ValueError("폴더가 없습니다: %s" % rel)
        return {"root": root, "dir": rel, "entries": out, "total": len(files), "git": bool(git_root(root))}

    def comment_counts(self, root):
        """프로젝트 안 파일·폴더(상대 경로)마다 열린(저장만·입력창) 문서 코멘트 수."""
        out = {}
        for c in self.lens.md_comments.read()["items"]:
            p = c.get("path") or ""
            if c.get("state") not in OPEN_STATES or not inside(root, p) or p == root:
                continue
            parts = os.path.relpath(p, root).replace(os.sep, "/").split("/")
            for i in range(1, len(parts) + 1):
                k = "/".join(parts[:i])
                out[k] = out.get(k, 0) + 1
        return out

    # --- 다듬기 ---------------------------------------------------------
    def project_root(self, path, root=None):
        if root and inside(root, path) and os.path.isdir(root):
            return os.path.abspath(root)
        return git_root(path) or os.path.dirname(path)

    def build_prompt(self, path, comments, instruction=""):
        lines = [REFINE_MARK,
                 "아래 문서를 코멘트에 맞게 직접 고쳐라.",
                 "- 문서: %s" % path,
                 "- 고칠 파일은 이 문서 하나다. 다른 파일은 바꾸지 마라.",
                 "- 코멘트마다 인용한 부분을 찾아 그 의견대로 고친다. 의견이 없는 코멘트는 그 부분을 더 낫게 다듬어 달라는 뜻이다.",
                 "- 코멘트가 없는 부분은 고친 곳과 이어지게 꼭 필요할 때만 손댄다. 문서의 말투·형식은 지킨다.",
                 "- 다 고친 뒤 코멘트마다 무엇을 어떻게 바꿨는지 한 줄씩 답하라."]
        if (instruction or "").strip():
            lines += ["", "추가 요청: " + instruction.strip()]
        for i, c in enumerate(comments, 1):
            where = " (%d번째 줄)" % (c["line"] + 1) if isinstance(c.get("line"), int) else ""
            lines += ["", "## 코멘트 %d%s" % (i, where)]
            lines += ["> " + q for q in (c.get("quote") or "").split("\n")]
            lines.append("의견: " + ((c.get("text") or "").strip() or "(없음 — 이 부분을 다듬어 줘)"))
        return "\n".join(lines)

    def start(self, body):
        path = os.path.abspath(os.path.expanduser(body.get("path") or ""))
        if not os.path.isfile(path):
            raise ValueError("문서가 없습니다: %s" % path)
        rid = body.get("client_id") or str(uuid.uuid4())
        for r in self.refines.read()["items"]:
            if r["id"] == rid:           # 같은 요청의 재전송
                return self.public(r)
        want = body.get("comment_ids")
        comments = [c for c in self.lens.list_md_comments(path)
                    if (c["id"] in want if want is not None else c.get("state") in OPEN_STATES)]
        instruction = (body.get("instruction") or "").strip()
        if not comments and not instruction:
            raise ValueError("다듬을 코멘트를 하나 이상 고르거나 요청을 적어 주세요")
        comments.sort(key=lambda c: (c.get("start") if isinstance(c.get("start"), int) else 1 << 30, c.get("created_at") or 0))
        root = self.project_root(path, body.get("root"))
        text = _read(path)
        prompt = self.build_prompt(path, comments, instruction)
        model, effort, tier = body.get("model") or None, body.get("effort") or None, body.get("tier") or None
        target = body.get("session_id") or None
        t0 = _now()
        if target:
            out = self.lens.send_turn(target, prompt, None, model, effort, "refine-" + rid, tier=tier)
        else:
            out = self.lens.create_session(root, prompt, None, model, effort, "refine-" + rid, agent=body.get("agent"), tier=tier)
        rec = {"id": rid, "path": path, "root": root, "thread_id": out["id"], "turn_id": out.get("turn_id"),
               "comment_ids": [c["id"] for c in comments], "instruction": instruction, "model": model, "effort": effort,
               "target": "session" if target else "new", "created_at": t0, "status": "running",
               "before": text if len(text) <= BEFORE_LIMIT else None, "before_mtime": _mtime(path)}
        ids = set(rec["comment_ids"])

        def mark(d):
            for c in d["items"]:
                if c["id"] in ids:
                    c.update(state="sent", sent_to=out["id"], refine_id=rid, updated_at=_now())
        self.lens.md_comments.update(mark)

        def add(d):
            d["items"].append(rec)
            same = [r for r in d["items"] if r["path"] == path]
            for old in same[:-KEEP_PER_DOC]:
                d["items"].remove(old)
        self.refines.update(add)
        applog.log("md_refine_start", refine=rid, thread=out["id"], turn=out.get("turn_id"), comments=len(comments),
                   target=rec["target"], model=model, effort=effort, ms=int((_now() - t0) * 1000))
        return self.public(rec)

    def _on_turn_done(self, tid, turn_id, status, error):
        hit = []

        def fn(d):
            for r in d["items"]:
                if r["thread_id"] == tid and r["status"] == "running" and (not r.get("turn_id") or r["turn_id"] == turn_id):
                    self._finish(r, status, error)
                    hit.append(r)
        self.refines.update(fn)
        for r in hit:
            applog.log("md_refine_end", level="info" if r["status"] == "completed" else "warn", refine=r["id"], thread=tid,
                       status=r["status"], changed=(r.get("diff") or {}).get("changed"),
                       ms=int((r["finished_at"] - r["created_at"]) * 1000))

    def _finish(self, r, status, error):
        r["status"] = status or "completed"
        r["error"] = error
        r["finished_at"] = _now()
        r["after_mtime"] = _mtime(r["path"])
        r["diff"] = None
        if r.get("before") is not None and r["after_mtime"] is not None:
            try:
                r["diff"] = line_diff(r["before"], _read(r["path"]))
            except OSError:
                pass

    def _settle(self, r):
        """앱이 다시 켜졌거나 알림을 놓친 다듬기의 끝 상태를 기록에서 읽어 맞춘다."""
        if r["status"] != "running" or _now() - r["created_at"] < 5:
            return
        with self.lens.lock:
            lt = self.lens.live.get(r["thread_id"])
            if lt and lt.status in ("starting", "inProgress"):
                return
        try:
            data = self.lens.read_session(r["thread_id"])
        except (RpcError, KeyError, ValueError):
            return
        t = next((t for t in data["turns"] if t["id"] == r.get("turn_id")), None)
        # 다듬기는 늘 이 앱의 app-server 에서 돈다 — 여기 살아 있는 턴이 없는데 기록이 끝나지 않았으면 앱이 꺼지며 끊긴 것이다
        if t and (t["status"] != "inProgress" or t.get("external")):
            st = "interrupted" if t["status"] in ("inProgress", "unfinished") else t["status"]
            self._on_turn_done(r["thread_id"], t["id"], st, t.get("error"))

    def _answer(self, r):
        if r["status"] == "running" or r.get("answer") is not None:
            return
        try:
            data = self.lens.read_session(r["thread_id"])
        except (RpcError, KeyError, ValueError):
            return
        t = next((t for t in data["turns"] if t["id"] == r.get("turn_id")), None)
        ans = ((t or {}).get("answer") or {}).get("text") or ""

        def fn(d):
            for x in d["items"]:
                if x["id"] == r["id"]:
                    x["answer"] = ans[:ANSWER_LIMIT]
                    r["answer"] = x["answer"]
        self.refines.update(fn)

    def list(self, path):
        path = os.path.abspath(path)
        items = [r for r in self.refines.read()["items"] if r["path"] == path]
        for r in items:
            self._settle(r)
            self._answer(r)
        items = [r for r in self.refines.read()["items"] if r["path"] == path]
        return [self.public(r) for r in sorted(items, key=lambda r: r["created_at"], reverse=True)]

    def public(self, r):
        out = {k: v for k, v in r.items() if k != "before"}
        out["can_revert"] = bool(r.get("before") is not None and r["status"] != "running" and not r.get("reverted")
                                 and r.get("after_mtime") is not None and _mtime(r["path"]) == r.get("after_mtime"))
        out["current"] = r.get("after_mtime") is not None and _mtime(r["path"]) == r.get("after_mtime")
        return out

    def revert(self, rid):
        """다듬기 전 글로 되돌린다 — 다듬은 뒤 문서가 또 바뀌었으면 거절한다."""
        def fn(d):
            for r in d["items"]:
                if r["id"] != rid:
                    continue
                if not self.public(r)["can_revert"]:
                    raise ValueError("되돌릴 수 없습니다 — 다듬은 뒤 문서가 바뀌었거나 고치기 전 글이 없습니다")
                with open(r["path"], "w", encoding="utf-8") as f:
                    f.write(r["before"])
                r["reverted"] = _now()
                return self.public(r)
            raise KeyError(rid)
        out = self.refines.update(fn)
        ids = set(out["comment_ids"])

        def reopen(d):
            for c in d["items"]:
                if c["id"] in ids and c.get("refine_id") == rid:
                    c["state"] = "none"
                    c["updated_at"] = _now()
        self.lens.md_comments.update(reopen)
        applog.log("md_refine_revert", refine=rid)
        return out


def line_diff(before, after):
    a, b = before.splitlines(), after.splitlines()
    sm = difflib.SequenceMatcher(None, a, b, autojunk=False)
    added = removed = 0
    lines = []
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            continue
        removed += i2 - i1
        added += j2 - j1
        lines.extend(range(j1, j2))
    return {"added": added, "removed": removed, "changed": added + removed > 0, "lines": lines[:3000]}
