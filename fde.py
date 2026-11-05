"""FDE 케이스 — 작업 폴더의 문제 하나를 이해 → 계획 → 구현 → 검증 → 사용 사이클로 다룬다.

단일 출처는 작업 폴더의 `fde/case.json`, `fde/CASE.md` 는 저장할 때마다 다시 쓰는 읽기용 보고서다.

- AI 단계는 셋뿐이다: understand(이해 카드), plan(접근 · 메인 알고리즘 · 해결책 비교 · 검증 명령), ask(질문).
  계획을 다시 만들면 그것이 메인 알고리즘의 다음 버전이다 — 피드백과 최근 검증 결과를 넣어 개선한다.
- AI 가 사람에게 묻는 것은 선택지(choices)로만 온다. 고르면 결정 기록(D 번호)이 된다.
- 검증 통과는 명령의 종료 코드 0 으로만 정한다. 출력의 `METRIC 이름=값` 줄은 그때의 계획 버전 지표가 된다.

AI 작업은 forks.JobManager 의 'fde' 작업(작업 폴더의 일회용 읽기 전용 세션)으로 돈다. 결과는 케이스를 읽을 때
끝난 작업을 찾아 반영한다 — 화면을 떠났다 와도 결과를 잃지 않는다.
"""

import json
import os
import re
import subprocess
import threading
import time

import applog
import refine

DIR = "fde"
CTX_LIMIT = 60 * 1024
CTX_TOTAL = 240 * 1024
RUN_TIMEOUT_SEC = int(os.environ.get("CODEX_LENS_FDE_RUN_TIMEOUT_SEC") or 300)
METRIC_RE = re.compile(r"^\s*METRIC\s+([A-Za-z0-9_.-]+)\s*=\s*(-?[0-9][0-9.eE+-]*)\s*$", re.M)
STEPS = {"understand": "이해 카드", "plan": "계획", "ask": "질문"}
TARGETS = ("understanding", "plan")

_locks, _guard = {}, threading.Lock()


def _now():
    return time.time()


def _s(v, n=2000):
    return v.strip()[:n] if isinstance(v, str) else ""


def _list(v, n=10, each=500):
    return [x for x in (_s(i, each) for i in (v if isinstance(v, list) else [])) if x][:n]


def _lock(path):
    with _guard:
        return _locks.setdefault(path, threading.RLock())


def _next(case, prefix):
    seq = case.setdefault("seq", {})
    seq[prefix] = seq.get(prefix, 0) + 1
    return "%s%d" % (prefix, seq[prefix])


def last(items):
    return items[-1] if items else None


def resolve_cwd(cwd):
    cwd = os.path.abspath(os.path.expanduser((cwd or "").strip()))
    if not cwd or not os.path.isdir(cwd):
        raise ValueError("작업 폴더가 없습니다: %s" % (cwd or "(비어 있음)"))
    return cwd


def empty_case():
    return {"problem": "", "created_at": _now(), "updated_at": _now(), "context": [], "understanding": [], "plan": [],
            "checks": [], "runs": [], "decisions": [], "feedback": [], "qa": [], "usage": [], "pending": [], "errors": [], "seq": {}}


# --- 저장 ------------------------------------------------------------------
def load(cwd):
    try:
        with open(os.path.join(cwd, DIR, "case.json"), encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return None
    case = empty_case()
    case.update(data if isinstance(data, dict) else {})
    return case


def save(cwd, case):
    d = os.path.join(cwd, DIR)
    os.makedirs(d, exist_ok=True)
    case["updated_at"] = _now()
    for name, text in (("case.json", json.dumps(case, ensure_ascii=False, indent=1) + "\n"), ("CASE.md", report(case))):
        tmp = os.path.join(d, name + ".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, os.path.join(d, name))


# --- 컨텍스트 --------------------------------------------------------------
def context_text(cwd, item):
    if item.get("kind") == "text":
        return item.get("text") or ""
    p = os.path.abspath(os.path.join(cwd, os.path.expanduser(item.get("path") or "")))
    try:
        if os.path.isdir(p):
            names = []
            for root, dirs, files in os.walk(p):
                dirs[:] = sorted(x for x in dirs if not x.startswith(".") and x not in ("node_modules", "__pycache__"))
                names += [os.path.relpath(os.path.join(root, f), p) for f in sorted(files)]
                if len(names) > 300:
                    break
            return "[폴더 목록]\n" + "\n".join(names[:300])
        with open(p, "rb") as f:
            raw = f.read(CTX_LIMIT)
        if b"\x00" in raw[:4096]:
            return "[글자 파일이 아님]"
        return raw.decode("utf-8", errors="replace")
    except OSError as e:
        return "[읽지 못함: %s]" % e


def add_context(cwd, case, body):
    if body.get("kind") == "text":
        text = _s(body.get("text"), CTX_LIMIT)
        if not text:
            raise ValueError("붙여 넣은 글이 비었습니다")
        item = {"kind": "text", "text": text, "title": text.split("\n")[0][:60]}
    else:
        path = _s(body.get("path"), 1000)
        full = os.path.abspath(os.path.join(cwd, os.path.expanduser(path))) if path else ""
        if not path or not os.path.exists(full):
            raise ValueError("파일이 없습니다: %s" % (path or "(비어 있음)"))
        rel = os.path.relpath(full, cwd)
        item = {"kind": "file", "path": full if rel.startswith("..") else rel, "title": os.path.basename(full) or rel}
    item.update(id=_next(case, "C"), at=_now())
    case["context"].append(item)
    return item


def context_blocks(cwd, case):
    out, total = [], 0
    for c in case["context"]:
        text = context_text(cwd, c)[:max(0, CTX_TOTAL - total)]
        total += len(text)
        out.append("[%s] %s\n<<<\n%s\n>>>" % (c["id"], c.get("path") or c.get("title"), text))
    return "\n\n".join(out) or "(지정한 컨텍스트 없음)"


# --- 프롬프트 --------------------------------------------------------------
FORMAT = (
    "출력: JSON 객체 하나만 반환한다(코드 블록 · 설명 없이).\n"
    '공통 키: "headline"(이것만 읽어도 핵심을 아는 한 문장, 80자 안), "summary"(핵심 3개 이하), '
    '"detail"(펼쳐 볼 세부, 마크다운, 1500자 안, 요지를 되풀이하지 않음), "sources"(근거로 쓴 컨텍스트 ID 배열, 예 ["C1"]), '
    '"choices"(사용자가 정해야 할 가치판단 최대 3개: [{"question":"한 문장","why":"이 답이 무엇을 바꾸는지",'
    '"options":[{"label":"짧은 선택지","effect":"고르면 달라지는 것 한 줄","recommended":true}]}] — '
    "options 는 서로 겹치지 않는 2~4개, 추천은 많아야 하나. 데이터로 확인할 수 있는 사실은 묻지 않는다. "
    "아래 결정 기록에 있는 것은 다시 묻지 않는다.)\n")

PROMPTS = {
    "understand": (
        "사용자의 문제를 사용자가 쉽게 이해할 수 있는 형태로 바꾼다. 컨텍스트에 없는 사실을 지어내지 않는다.\n"
        "detail 에는 '### 문제', '### 입력', '### 만들 결과', '### 지킬 규칙', '### 성공의 모습', '### 아직 모르는 것' 절을 짧은 목록으로 쓴다."),
    "plan": (
        "이 문제 해결에 특화된 숙련 엔지니어처럼 큰 그림을 세운다. 이해 카드와 컨텍스트를 근거로 한다.\n"
        "추가 키:\n"
        '"modules": [{"name":"모듈","role":"하는 일 한 줄","main":true}] — 핵심 모듈 3~6개, main 은 메인 알고리즘이 사는 모듈 하나.\n'
        '"algorithm": {"name":"메인 알고리즘 이름","idea":"핵심 아이디어 두세 문장","steps":["단계"],"weaknesses":["알려진 약점"]}.\n'
        '"criteria": [{"name":"비교 기준","metric":"재는 지표(가능하면 METRIC 이름, 영문 소문자)","why":"왜 중요한지"}] — 3~5개.\n'
        '"options": [{"name":"해결 방법","summary":"한 줄","eval":{"비교 기준 name":"짧은 평가"}}] — 실제로 다른 방법 2~3개, 첫째가 위 메인 알고리즘.\n'
        '"checks": [{"name":"검증 이름","command":"작업 폴더에서 도는 셸 명령"}] — 통과는 종료 코드 0, 지표는 '
        "'METRIC 이름=값' 줄로 찍는다. 만든 코드와 검사 코드는 분리한다.\n"
        '"change": 이전 계획이 있으면 이번에 바꾼 점과 겨냥한 지표 한 문장(없으면 빈 값).\n'
        "어느 해결 방법으로 갈지는 choices 의 첫 질문으로 둔다(추천 표시). 최근 검증 결과 · 피드백 · 사용 기록이 있으면 "
        "메인 알고리즘을 개선하는 데 쓴다."),
    "ask": "사용자가 아래 대상에 대해 질문했다. 컨텍스트와 대상을 근거로 짧고 구체적으로 답한다(detail 에 답, headline 에 요지).",
}
GUARD = "\n\n---\n분석 · 제안만 한다. 파일을 고치거나 명령을 실행하지 말고 도구 없이 위 자료만 쓴다. JSON 만 반환한다."


def _dump(v):
    return json.dumps(v, ensure_ascii=False, indent=1)


def build_prompt(cwd, case, step, extra=None):
    extra = extra or {}
    parts = ["[FDE 케이스 — codex-lens · step=%s]" % step, PROMPTS[step], FORMAT,
             "[문제]\n" + (case["problem"] or "(비어 있음)"), "[컨텍스트]\n" + context_blocks(cwd, case),
             "[결정 기록]\n" + ("\n".join("- %s → %s" % (d["question"], d["chosen"]) for d in case["decisions"][-30:]) or "(없음)")]
    und, plan = last(case["understanding"]), last(case["plan"])
    if und:
        parts.append("[이해 카드 v%d]\n%s\n%s" % (und["v"], und["data"]["headline"], und["data"]["detail"]))
    if plan and step != "understand":
        d = plan["data"]
        parts.append("[계획 v%d]\n%s" % (plan["v"], _dump({k: d.get(k) for k in ("modules", "algorithm", "criteria", "options")})))
    if step == "plan" and case["runs"]:
        parts.append("[최근 검증]\n" + "\n".join("- %s %s %s" % (r["name"], "통과" if r["ok"] else "실패", r.get("metrics") or "")
                                              for r in case["runs"][-10:]))
    if step in ("understand", "plan") and case["usage"]:
        parts.append("[사용 기록]\n" + "\n".join("- " + u["text"] for u in case["usage"][-10:]))
    if step == "ask":
        parts.append("[질문 대상: %s]\n[질문]\n%s" % ({"understanding": "이해 카드", "plan": "계획"}[extra["target"]], extra["question"]))
    else:
        target = "understanding" if step == "understand" else "plan"
        fb = [f for f in case["feedback"] if f["status"] == "open" and f["target"] == target]
        if fb:
            parts.append("[사용자 피드백 — 반드시 반영]\n" + "\n".join("- %s" % f["text"] for f in fb))
    return "\n\n".join(parts) + GUARD


# --- 결과 ------------------------------------------------------------------
def parse(step, text):
    raw = refine.extract_json(text, key="headline")
    out = {"headline": _s(raw.get("headline"), 200), "summary": _list(raw.get("summary"), 3, 300),
           "detail": _s(raw.get("detail"), 5000), "sources": _list(raw.get("sources"), 10, 10), "choices": []}
    if not out["headline"]:
        raise ValueError("요지(headline)가 비었습니다")
    for c in raw.get("choices") if isinstance(raw.get("choices"), list) else []:
        if not isinstance(c, dict) or not _s(c.get("question")):
            continue
        opts, rec = [], False
        for o in c.get("options") if isinstance(c.get("options"), list) else []:
            o = {"label": o} if isinstance(o, str) else o
            if isinstance(o, dict) and _s(o.get("label"), 160):
                r = bool(o.get("recommended")) and not rec
                rec = rec or r
                opts.append({"label": _s(o["label"], 160), "effect": _s(o.get("effect"), 300), "recommended": r})
        if len(opts) >= 2:
            out["choices"].append({"question": _s(c["question"], 400), "why": _s(c.get("why"), 300), "options": opts[:4]})
    out["choices"] = out["choices"][:3]
    if step == "plan":
        a = raw.get("algorithm") if isinstance(raw.get("algorithm"), dict) else {}
        out.update(
            modules=[{"name": _s(m.get("name"), 120), "role": _s(m.get("role"), 300), "main": bool(m.get("main"))}
                     for m in raw.get("modules") or [] if isinstance(m, dict) and _s(m.get("name"))][:8],
            algorithm={"name": _s(a.get("name"), 120), "idea": _s(a.get("idea"), 800), "steps": _list(a.get("steps"), 10),
                       "weaknesses": _list(a.get("weaknesses"), 6)},
            criteria=[{"name": _s(c.get("name"), 80), "metric": _s(c.get("metric"), 40), "why": _s(c.get("why"), 300)}
                      for c in raw.get("criteria") or [] if isinstance(c, dict) and _s(c.get("name"))][:6],
            options=[{"name": _s(o.get("name"), 120), "summary": _s(o.get("summary"), 300),
                      "eval": {_s(k, 80): _s(v, 200) for k, v in (o.get("eval") or {}).items()} if isinstance(o.get("eval"), dict) else {}}
                     for o in raw.get("options") or [] if isinstance(o, dict) and _s(o.get("name"))][:4],
            checks=[{"name": _s(c.get("name"), 120), "command": _s(c.get("command"), 2000)}
                    for c in raw.get("checks") or [] if isinstance(c, dict) and _s(c.get("command")) and _s(c.get("name"))][:6],
            change=_s(raw.get("change"), 500))
        if not out["modules"] or not out["algorithm"]["idea"]:
            raise ValueError("모듈이나 메인 알고리즘이 비었습니다")
    return out


def apply_result(cwd, case, pend, job):
    step = pend["step"]
    data = parse(step, job.get("answer") or "")
    for c in data["choices"]:
        c["id"] = _next(case, "P")
    meta = {"at": _now(), "model": job.get("model"), "effort": job.get("effort")}
    if step == "ask":
        case["qa"].append(dict(meta, id=_next(case, "Q"), target=pend["target"], question=pend["question"], data=data))
        return
    key = "understanding" if step == "understand" else "plan"
    v = len(case[key]) + 1
    case[key].append(dict(meta, v=v, data=data))
    for f in case["feedback"]:
        if f["status"] == "open" and f["target"] == key:
            f.update(status="applied", applied_in=v)
    if step == "plan":   # AI 가 낸 검증 명령은 같은 명령이 없을 때만 더한다 — 사람이 지울 수 있다
        have = {c["command"] for c in case["checks"]}
        for c in data["checks"]:
            if c["command"] not in have:
                case["checks"].append(dict(c, id=_next(case, "V"), by="ai", at=_now()))
    applog.log("fde_apply", step=step, v=v, choices=len(data["choices"]))


def decide(case, body):
    pid = _s(body.get("id"), 20)
    for item in case["understanding"] + case["plan"] + case["qa"]:
        for c in item["data"]["choices"]:
            if c["id"] != pid:
                continue
            if c.get("decision"):
                raise ValueError("이미 정했습니다(%s)" % c["decision"])
            chosen = _s(body.get("chosen"), 600)
            if not chosen:
                raise ValueError("고른 답이 없습니다")
            d = {"id": _next(case, "D"), "at": _now(), "question": c["question"], "chosen": chosen, "reason": _s(body.get("reason"), 600),
                 "options": [o["label"] for o in c["options"]], "recommended": next((o["label"] for o in c["options"] if o["recommended"]), "")}
            case["decisions"].append(d)
            c.update(decision=d["id"], answer=chosen)
            applog.log("fde_decision", id=d["id"])
            return d
    raise ValueError("선택지를 찾지 못했습니다: %s" % pid)


# --- 검증 ------------------------------------------------------------------
def parse_metrics(text):
    out = {}
    for name, val in METRIC_RE.findall(text or ""):
        try:
            num = float(val)
        except ValueError:
            continue
        out[name] = int(num) if num.is_integer() else num
    return out


def run_check(cwd, command, timeout=None):
    t0 = _now()
    try:
        p = subprocess.run(["/bin/sh", "-c", command], cwd=cwd, capture_output=True, timeout=timeout or RUN_TIMEOUT_SEC,
                           stdin=subprocess.DEVNULL)
        out = p.stdout.decode("utf-8", "replace") + (("\n[stderr]\n" + p.stderr.decode("utf-8", "replace")) if p.stderr else "")
        code = p.returncode
    except subprocess.TimeoutExpired:
        out, code = "[시간 초과]", None
    return {"exit": code, "ok": code == 0, "ms": int((_now() - t0) * 1000), "tail": out[-4000:], "metrics": parse_metrics(out)}


def metric_table(case):
    """계획(메인 알고리즘) 버전별 최근 지표."""
    t = {}
    for r in case["runs"]:
        if r.get("plan_v") and r.get("metrics"):
            t.setdefault(str(r["plan_v"]), {}).update(r["metrics"])
    return t


# --- 보고서 ----------------------------------------------------------------
def report(case):
    t = lambda x: time.strftime("%m-%d %H:%M", time.localtime(x)) if x else ""  # noqa: E731
    und, plan = last(case["understanding"]), last(case["plan"])
    L = ["# FDE 케이스", "", "> 자동 생성 — 원본은 같은 폴더의 `case.json`.", "", "## 문제", "", case["problem"] or "(비어 있음)", ""]
    if case["context"]:
        L += ["## 컨텍스트", ""] + ["- %s %s" % (c["id"], c.get("path") or c.get("title")) for c in case["context"]] + [""]
    if und:
        L += ["## 이해 (v%d)" % und["v"], "", "**%s**" % und["data"]["headline"], ""] + ["- " + s for s in und["data"]["summary"]]
        L += ["", und["data"]["detail"], ""]
    if plan:
        d = plan["data"]
        L += ["## 계획 (v%d)" % plan["v"], "", "**%s**" % d["headline"], ""]
        L += ["- %s%s — %s" % (m["name"], " ★" if m["main"] else "", m["role"]) for m in d["modules"]]
        a = d["algorithm"]
        L += ["", "### 메인 알고리즘 — %s" % a["name"], "", a["idea"], ""] + ["%d. %s" % (i + 1, s) for i, s in enumerate(a["steps"])]
        if d["criteria"]:
            L += ["", "### 비교", "", "| 기준 | " + " | ".join(o["name"] for o in d["options"]) + " |", "|---" * (len(d["options"]) + 1) + "|"]
            L += ["| %s | %s |" % (c["name"], " | ".join(o["eval"].get(c["name"], "") for o in d["options"])) for c in d["criteria"]]
        L.append("")
    mt = metric_table(case)
    if case["plan"]:
        names = sorted({k for v in mt.values() for k in v})
        L += ["## 메인 알고리즘 버전 × 지표", "", "| 계획 | 알고리즘 | 바꾼 점 | " + " | ".join(names) + " |", "|---|---|---|" + "---|" * len(names)]
        L += ["| v%d | %s | %s | %s |" % (p["v"], p["data"]["algorithm"]["name"], p["data"].get("change", ""),
                                       " | ".join(str(mt.get(str(p["v"]), {}).get(n, "")) for n in names)) for p in case["plan"]]
        L.append("")
    if case["checks"]:
        lastrun = {r["check"]: r for r in case["runs"]}
        L += ["## 검증", ""] + ["- %s %s `%s` — %s" % (c["id"], c["name"], c["command"],
                                                      ("통과" if lastrun[c["id"]]["ok"] else "실패") if c["id"] in lastrun else "미실행")
                                for c in case["checks"]] + [""]
    if case["decisions"]:
        L += ["## 결정 기록", ""] + ["- **%s** %s %s → **%s**%s" % (d["id"], t(d["at"]), d["question"], d["chosen"],
                                                                 (" — " + d["reason"]) if d.get("reason") else "") for d in case["decisions"]] + [""]
    if case["usage"]:
        L += ["## 사용 기록", ""] + ["- %s %s" % (t(u["at"]), u["text"]) for u in case["usage"]] + [""]
    return "\n".join(L)


# --- 관리자 ----------------------------------------------------------------
class Fde:
    def __init__(self, lens):
        self.lens = lens

    def _sync(self, cwd, case):
        changed = False
        for pend in list(case["pending"]):
            try:
                job = self.lens.jobs.get(pend["job"])
            except KeyError:
                job = {"status": "failed", "error": "작업 기록이 사라졌습니다"}
            if job.get("status") == "running":
                continue
            case["pending"].remove(pend)
            changed = True
            err = job.get("error") or ("취소했습니다" if job.get("status") == "cancelled" else "실패했습니다")
            if job.get("status") == "done":
                try:
                    apply_result(cwd, case, pend, job)
                    continue
                except ValueError as e:
                    err = "결과를 읽지 못했습니다: %s" % e
            case["errors"] = case["errors"][-4:] + [{"step": pend["step"], "error": err[:400], "at": _now()}]
            applog.log("fde_step_failed", level="warn", step=pend["step"], error=err[:200])
        return changed

    def _edit(self, cwd, fn, create=False):
        cwd = resolve_cwd(cwd)
        with _lock(cwd):
            case = load(cwd)
            if case is None:
                if not create:
                    raise KeyError("케이스가 없습니다")
                case = empty_case()
            self._sync(cwd, case)
            out = fn(cwd, case)
            save(cwd, case)
        return out

    def view(self, cwd):
        cwd = resolve_cwd(cwd)
        with _lock(cwd):
            case = load(cwd)
            if case is None:
                return {"cwd": cwd, "case": None}
            if self._sync(cwd, case):
                save(cwd, case)
        jobs = []
        for p in case["pending"]:
            try:
                jobs.append(dict(self.lens.jobs.public(self.lens.jobs.get(p["job"])), step=p["step"]))
            except KeyError:
                pass
        return {"cwd": cwd, "case": case, "jobs": jobs, "metrics": metric_table(case),
                "report": os.path.join(cwd, DIR, "CASE.md")}

    def create(self, cwd, body):
        def fn(cwd, case):
            case["problem"] = _s(body.get("problem"), 20000)
        self._edit(cwd, fn, create=True)
        return self.view(cwd)

    def act(self, cwd, action, body):
        def fn(cwd, case):
            if action == "context":
                return add_context(cwd, case, body)
            if action == "context_remove":
                case["context"] = [c for c in case["context"] if c["id"] != body.get("id")]
                return True
            if action == "decide":
                return decide(case, body)
            if action == "feedback":
                if not _s(body.get("text")):
                    raise ValueError("피드백이 비었습니다")
                f = {"id": _next(case, "F"), "target": body.get("target") if body.get("target") in TARGETS else "understanding",
                     "text": _s(body.get("text"), 3000), "status": "open", "at": _now()}
                case["feedback"].append(f)
                return f
            if action == "check_add":
                if not _s(body.get("command")):
                    raise ValueError("명령이 비었습니다")
                c = {"id": _next(case, "V"), "name": _s(body.get("name"), 120) or _s(body.get("command"), 40),
                     "command": _s(body.get("command"), 2000), "by": "user", "at": _now()}
                case["checks"].append(c)
                return c
            if action == "check_remove":
                case["checks"] = [c for c in case["checks"] if c["id"] != body.get("id")]
                return True
            if action == "usage":
                if not _s(body.get("text")):
                    raise ValueError("사용 기록이 비었습니다")
                u = {"id": _next(case, "U"), "text": _s(body.get("text"), 2000), "at": _now(),
                     "plan_v": len(case["plan"]) or None}
                case["usage"].append(u)
                return u
            if action == "problem":
                case["problem"] = _s(body.get("problem"), 20000)
                return True
            if action == "dismiss_errors":
                case["errors"] = []
                return True
            raise ValueError("모르는 동작: %s" % action)
        out = self._edit(cwd, fn)
        return dict(self.view(cwd), result=out)

    def step(self, cwd, body):
        step = body.get("step")
        if step not in STEPS:
            raise ValueError("모르는 단계: %s" % step)

        def fn(cwd, case):
            if any(p["step"] == step for p in case["pending"]):
                raise ValueError("%s 작업이 이미 돌고 있습니다" % STEPS[step])
            if step != "understand" and not case["understanding"]:
                raise ValueError("이해 카드부터 만든다")
            pend, extra = {"step": step, "at": _now()}, {}
            if step == "ask":
                q = _s(body.get("question"), 3000)
                if not q:
                    raise ValueError("질문이 비었습니다")
                extra = {"target": body.get("target") if body.get("target") in TARGETS else "understanding", "question": q}
                pend.update(extra)
            prompt = build_prompt(cwd, case, step, extra)
            job = self.lens.jobs.start_fde(cwd, prompt, step, body.get("agent"), body.get("model"), body.get("effort"),
                                           body.get("client_id"))
            pend["job"] = job["id"]
            case["pending"].append(pend)
            applog.log("fde_step_start", step=step, job=job["id"], prompt_chars=len(prompt))
            return job
        job = self._edit(cwd, fn)
        return dict(self.view(cwd), job=job)

    def run(self, cwd, ids):
        cwd = resolve_cwd(cwd)
        with _lock(cwd):
            case = load(cwd)
            if case is None:
                raise KeyError("케이스가 없습니다")
            todo = [c for c in case["checks"] if not ids or c["id"] in ids]
            plan_v = len(case["plan"]) or None
        if not todo:
            raise ValueError("돌릴 검증 명령이 없습니다")
        results = []
        for c in todo:   # 잠금 밖에서 돈다 — 오래 걸려도 화면이 케이스를 읽는다
            r = run_check(cwd, c["command"])
            r.update(check=c["id"], name=c["name"], at=_now(), plan_v=plan_v)
            results.append(r)
            applog.log("fde_check", check=c["id"], ok=r["ok"], ms=r["ms"], level="info" if r["ok"] else "warn")

        def fn(cwd, case):
            case["runs"] = (case["runs"] + results)[-300:]
            return results
        out = self._edit(cwd, fn)
        return dict(self.view(cwd), runs=out)
