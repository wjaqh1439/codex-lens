# Codex Lens

Codex·Claude Code 코딩 에이전트 세션을 빠르게 만들고, 결과를 **턴(요청 · 과정 · 답변)** 으로 읽고, 답의 특정 부분에 **인라인 질문·코멘트**를 남겨 쉽게 이해하는 웹 앱.

## 빠른 시작 (macOS · Linux)

```sh
# 1) Codex CLI 와 Claude Code 중 쓰는 쪽을 설치·로그인
#    (예: npm i -g @openai/codex && codex login · curl -fsSL https://claude.ai/install.sh | bash && claude)
# 2) 실행 — 의존성 없음(Python 3.10+)
python3 app.py                       # http://127.0.0.1:8761
CODEX_LENS_HOST=0.0.0.0 python3 app.py   # 다른 기기에서 접속할 때
```

기존 Codex 기록(`~/.codex/sessions`)과 Claude Code 기록(`~/.claude/projects`)이 한 목록에 바로 보인다.

그 기기에서만 쓸 설정은 `~/.codex-lens/env` 에 `KEY=VALUE` 줄로 적어 두면 앱과 재기동 스크립트가 시작할 때 읽는다(명령줄에서 준 환경변수가 이긴다).

```sh
# ~/.codex-lens/env 예
CODEX_LENS_HOST=0.0.0.0
CODEX_LENS_CLAUDE_PERMISSION_MODE=acceptEdits   # root 로 돌리는 Linux 에서 Claude Code 를 쓸 때
```

### 재기동 · 업데이트 (진행 중인 세션 유지)

```sh
python3 scripts/restart.py            # 끄고 다시 띄우기(안 떠 있으면 띄우기만)
python3 scripts/restart.py status     # start · stop 도 있다
CODEX_LENS_HOST=0.0.0.0 CODEX_LENS_DEMO=1 python3 scripts/restart.py   # 설정은 앱과 같은 환경변수
```

Codex app-server 는 앱과 따로 도는 중계기(`codex_broker.py`)가 쥐고 있어, 앱을 다시 띄워도 진행 중인 세션이 이어진다.
코드를 새로 받은 뒤(`git pull`, 지우고 다시 clone 해도 된다) 이 스크립트로 다시 띄우면 된다.
**`pkill -f "codex app-server"`·`pkill -f codex` 처럼 이름으로 끄지 않는다** — 중계기와 Codex 가 함께 꺼져 세션이 끊긴다.
systemd 서비스로 띄우면 `KillMode=process` 를 넣는다(기본값은 재시작 때 중계기까지 끈다). 컨테이너는 재시작하지 말고 안에서 앱만 다시 띄운다.

## 기능

- **세션 실행** — 에이전트(Codex · Claude Code)·작업 폴더·모델·effort·속도(Fast, Codex)를 고르고 첫 메시지를 보내면 바로 시작. 진행 단계·경과·지연 표시, 멈춤.
- **모델 관리** — 에이전트마다 새 세션의 기본 모델·effort·속도를 정하고, 쓰지 않는 모델은 고르기 목록에서 숨긴다.
- **워크트리** — 작업 폴더가 git 저장소면 기본으로 새 브랜치 워크트리에서 세션을 돌려 원본을 건드리지 않는다.
- **세션 보관** — 목록 또는 세션 머리에 보이는 "보관"을 누르면 일반 목록·검색·멘션에서 숨긴다. 목록의 "보관됨"에서 검색·열람하고 "보관 해제"로 복원한다. 이 앱의 보관 상태만 저장하므로 기록과 실행 중인 작업은 유지된다.
- **응답 중에 보내기** — "바로 넣기"(진행 중인 턴에 끼워 넣기, `turn/steer`) 또는 "턴 끝나고"(대기열에 두었다가 턴이 끝나면 보내기).
- **턴 구조** — 요청 / 과정(명령·파일 변경·생각·도구) / 답변.
- **과정 요약** — 버튼을 누른 턴만, 같은 모델로 포크해 원본 캐시를 읽으며 정리.
- **인라인 질문** — 답변에서 글을 고르거나 줄을 눌러 묻고, 답은 그 자리 아래 카드로. 답 안에서 다시 골라 이어 묻기.
- **코멘트** — 고른 글에 의견을 남기고 입력창에 넣어 다음 요청으로 보낸다.
- **멘션** — 입력창에서 `@` 로 파일·다른 세션을 고른다.
- **프로젝트 탐색기** — 작업 폴더를 폴더·파일 트리로 둘러보고 찾는다(git 무시 규칙을 따른다). 열린 코멘트 수를 파일·폴더에 표시.
- **문서 코멘트 · AI 다듬기** — `.md` 파일을 열어 부분마다 코멘트하고, 고른 코멘트대로 Codex 가 문서를 직접 고치게 한다. 바뀐 줄 표시·답 보기·되돌리기.
- **다듬기** — 입력창의 ✨ 다듬기는 입력창 위에 판을 연다. 목적(에이전트 요청·좋은 글)·다듬기 모델·effort 를 고르고, 다듬기 프롬프트를 확인·수정하고, 함께 보내는 내용을 본 뒤 시작한다(세션 맥락을 참고하고 캐시를 읽는다). 결과는 입력창에 바로 들어가고, 에이전트 의견·궁금한 것의 답·더 알려 주면 좋은 것(선택지·직접 답하기 → 답변 반영해 다듬기)·편집 메모·원문 비교·되돌리기는 판에, 다듬기 전 글과 다듬은 글은 '버전' 탭에 남아 비교하고 되돌린다.

- **포트 관리** — 세션이 데모·개발 서버를 띄울 때 `lens-port run --name web -- <명령 {port}>` 으로 대역(기본 20000–20999)의 빈 포트를 받아 띄운다(Codex 에 지침으로 알려 준다). 목록 머리의 "포트"(세션 머리의 "포트 N") 화면에서 열기·로그·끄기. 별도 도구 없이 macOS·Linux 에서 돈다.

### 다른 기기에서 데모 열기

- 같은 네트워크(예: 리눅스 VM): `CODEX_LENS_HOST=0.0.0.0 python3 app.py` — 세션 서버도 0.0.0.0 에 묶이고, 포트 화면의 "열기"는 지금 보는 호스트의 그 포트를 연다.
- tailscale 이 로그인된 기기면 따로 설정하지 않아도 열린 서버마다 `tailscale serve` HTTPS 주소를 붙이고 끌 때 거둔다(끄려면 `CODEX_LENS_PORT_EXPOSE=`).
- 다른 HTTPS 앞단을 쓰면 명령·주소를 직접 준다(아래는 기본값과 같은 tailscale 예):

```sh
export CODEX_LENS_PORT_EXPOSE='tailscale serve --bg --https={public} http://127.0.0.1:{port}'
export CODEX_LENS_PORT_UNEXPOSE='tailscale serve --https={public} off'
export CODEX_LENS_PUBLIC_URL='https://<호스트>.ts.net:{public}/'
```

## 테스트

```sh
python3 -m unittest discover -s tests -v
```
