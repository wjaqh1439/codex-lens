"""최종 출력 규약 agent-output/1 을 이 앱이 직접 강제한다 — 화면이 답을 종류별 카드(정할 것·메인 반영·할 일)로 그리려고.

규약·스키마·지시문은 agent-hub `agent_output.py`(사양 docs/agent-output.md)의 사본이다 — 거기서 바뀌면 여기도 고친다.
이 앱은 다른 프로그램 없이(빈 VM 에 codex·claude CLI 만 있어도) 혼자 규약을 건다. 강제는 각 CLI 가 한다.

- Codex app-server: `turn/start` 의 `outputSchema` 로 최종 메시지(phase final_answer)를 제약 디코딩한다(턴마다 준다).
  중간 메시지는 평소 글이다. 지시문은 `developerInstructions`.
- Claude Code: `--json-schema` 로 띄우면 StructuredOutput 도구가 생기고 CLI 가 입력을 검증해 어긋나면 다시 부르게 한다.
  부르지 않고 끝내려 하면 CLI 가 ENFORCE_PREFIX 로 시작하는 사용자 줄을 넣는다(요청으로 보이지 않게 거른다).
  지시문은 `--append-system-prompt`. 포크도 같은 인자를 줘야 원본 캐시를 읽는다(도구 정의가 같아야 한다).

규약을 거는 세션은 이 앱이 만든 세션(`registry.json` mine 의 `output`)뿐이다 — 밖에서 만든 세션은 그 세션의 원래 조건을 지킨다.
답 읽기(본문·카드)는 `turns.parse_output` 이 맡는다.
"""
import json
import os

PROTOCOL = "agent-output/1"
CLAUDE_TOOL = "StructuredOutput"
ENFORCE_PREFIX = "[structured-output-enforce]"
EXHAUSTED = "error_max_structured_output_retries"

# 새 세션에 규약을 걸지 — 기본 켬. 0 이면 끈다(이미 건 세션은 그대로 건다).
ENABLED = os.environ.get("CODEX_LENS_OUTPUT_PROTOCOL", "1") != "0"

_NULLABLE_STR = {"type": ["string", "null"]}
SCHEMA = {
    "type": "object", "additionalProperties": False,
    "required": ["protocol", "body", "todos", "decisions", "merge"],
    "properties": {
        "protocol": {"type": "string", "enum": [PROTOCOL]},
        "body": {"type": "string", "description": "사용자에게 보일 최종 답 전체(마크다운)"},
        "todos": {"type": "array", "items": {
            "type": "object", "additionalProperties": False,
            "required": ["id", "title", "detail", "owner", "project"],
            "properties": {
                "id": {"type": "string"},
                "title": {"type": "string", "description": "한 줄 할 일"},
                "detail": {**_NULLABLE_STR, "description": "넘겨받는 쪽이 알아야 할 조건·파일·완료 기준"},
                "owner": {"type": "string", "enum": ["agent", "user"],
                          "description": "agent: 다른 세션에 넘길 수 있음, user: 사용자가 직접 할 일"},
                "project": {**_NULLABLE_STR, "description": "지금 프로젝트가 아니면 그 프로젝트 이름"}}}},
        "decisions": {"type": "array", "items": {
            "type": "object", "additionalProperties": False,
            "required": ["id", "header", "question", "options", "multiSelect", "allowOther"],
            "properties": {
                "id": {"type": "string"},
                "header": {**_NULLABLE_STR, "description": "12자 안의 짧은 이름"},
                "question": {"type": "string"},
                "options": {"type": "array", "items": {
                    "type": "object", "additionalProperties": False,
                    "required": ["label", "description", "recommended"],
                    "properties": {
                        "label": {"type": "string"},
                        "description": {**_NULLABLE_STR, "description": "고를 때 알아야 할 근거"},
                        "recommended": {"type": "boolean"}}}},
                "multiSelect": {"type": "boolean"},
                "allowOther": {"type": "boolean", "description": "선택지 밖의 직접 입력을 받을지"}}}},
        "merge": {"anyOf": [{"type": "null"}, {
            "type": "object", "additionalProperties": False,
            "required": ["summary", "bump", "version", "checks"],
            "properties": {
                "summary": {"type": "string", "description": "CHANGELOG 에 들어갈 한두 줄"},
                "bump": {"anyOf": [{"type": "string", "enum": ["major", "minor", "patch"]}, {"type": "null"}]},
                "version": _NULLABLE_STR,
                "checks": {"type": "array", "items": {"type": "string"},
                           "description": "이미 끝낸 확인만(테스트·회귀·데모). 남은 확인은 todos 로"}}}]}}}

_RULES = """# 응답 규약 agent-output/1
이 세션의 최종 답은 매 턴 agent-output/1 JSON 하나로 낸다({how}).
- body: 사용자에게 보일 최종 답 전체(마크다운). 한 일·결과·근거·변경 파일·검증을 여기에 쓴다. 최종 답을 이 밖의 글로 먼저 쓰지 않는다.{last}
- todos: 남은 할 일 항목마다 하나. 다른 세션이 이어받을 수 있으면 owner "agent", 사용자가 직접 해야 하면 "user". detail 에는 이어받는 쪽이 알아야 할 조건·파일·완료 기준을 쓴다.
- decisions: 사용자가 정해야 할 질문마다 하나. 추천하는 선택지에 recommended true, description 에 근거.
- merge: 메인 반영 승인을 받아야 할 때만 채운다. 반영 여부·방식은 decisions 로 묻지 않고 여기 하나로 묻는다. checks 에는 이미 끝낸 확인만.
- 해당하는 것이 없으면 빈 배열·null. todos·decisions·merge 에 넣은 항목은 body 에 다시 늘어놓지 않는다.
- 작업 중 진행 상황을 짧게 알리는 글은 평소처럼 써도 된다.
"""
RULES = {
    "claude": _RULES.format(how=CLAUDE_TOOL + " 도구 호출", last=" " + CLAUDE_TOOL + " 호출이 턴의 마지막 동작이다."),
    "codex": _RULES.format(how="최종 메시지", last=""),
}


def schema_json():
    """CLI 인자로 넘길 스키마 한 줄(Claude --json-schema)."""
    return json.dumps(SCHEMA, ensure_ascii=False, separators=(",", ":"))


def join_instructions(*parts):
    """개발자 지침 여러 개(포트 안내·규약)를 하나로 — 비면 None."""
    out = "\n\n".join(p.strip() for p in parts if p and p.strip())
    return out or None


_ESC = {'"': '"', "\\": "\\", "/": "/", "b": "\b", "f": "\f", "n": "\n", "r": "\r", "t": "\t"}


def partial_body(text):
    """받는 중인 규약 JSON 에서 지금까지 온 body 글. 규약 JSON 으로 시작하지 않으면 None.

    스트리밍 중에는 JSON 이 닫히지 않아 json.loads 가 실패한다 — "body" 문자열만 앞에서부터 풀어 본문이 흘러오게 보인다.
    body 키가 아직 오지 않았으면 빈 글이다."""
    t = (text or "").lstrip()
    if t.startswith("```"):
        t = t.split("\n", 1)[1].lstrip() if "\n" in t else ""
    if not t.startswith("{"):
        return None
    head = t[:200]
    if '"protocol"' not in head and '"body"' not in head:
        return None if len(t) > 40 else ""
    i = _body_start(t)
    if i is None:
        return ""
    out = []
    n = len(t)
    while i < n:
        c = t[i]
        if c == '"':
            break
        if c != "\\":
            out.append(c)
            i += 1
            continue
        if i + 1 >= n:
            break
        e = t[i + 1]
        if e == "u":
            hexs = t[i + 2:i + 6]
            if len(hexs) < 4:
                break
            try:
                cp = int(hexs, 16)
            except ValueError:
                break
            i += 6
            if 0xD800 <= cp < 0xDC00:   # 서로게이트 쌍 — 뒤쪽이 아직 안 왔으면 기다린다
                if t[i:i + 2] != "\\u" or len(t) < i + 6:
                    break
                try:
                    lo = int(t[i + 2:i + 6], 16)
                except ValueError:
                    break
                cp = 0x10000 + ((cp - 0xD800) << 10) + (lo - 0xDC00)
                i += 6
            out.append(chr(cp))
            continue
        out.append(_ESC.get(e, e))
        i += 2
    return "".join(out)


def _body_start(t):
    """맨 바깥 객체의 "body" 문자열 값이 시작하는 위치 — 없거나 아직 안 왔으면 None."""
    depth, i, n = 0, 0, len(t)
    while i < n:
        c = t[i]
        if c == '"':
            j = i + 1
            while j < n and t[j] != '"':
                j += 2 if t[j] == "\\" else 1
            if j >= n:
                return None
            key = t[i + 1:j]
            k = j + 1
            while k < n and t[k] in " \t\r\n":
                k += 1
            if depth == 1 and key == "body" and k < n and t[k] == ":":
                k += 1
                while k < n and t[k] in " \t\r\n":
                    k += 1
                if k < n and t[k] == '"':
                    return k + 1
                return None
            i = j + 1
            continue
        if c in "{[":
            depth += 1
        elif c in "}]":
            depth -= 1
        i += 1
    return None
