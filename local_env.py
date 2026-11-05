"""기기별 설정 파일 — <데이터 홈>/env 의 `KEY=VALUE` 줄을 환경변수로 채운다(이미 있는 값이 이긴다).

저장소에 넣지 않을 그 기기만의 값(의견 위젯 출처·바깥 주소 등)을 두는 곳이다. 빈 줄과 `#` 로 시작하는 줄은 건너뛰고,
값을 감싼 따옴표 한 쌍은 벗긴다. 파일이 없으면 아무것도 하지 않는다. 표준 라이브러리만 쓴다.
"""
import os


def load(home, environ=None):
    """home/env 를 읽어 environ 에 없는 키만 채우고, 채운 키 목록을 돌려준다."""
    environ = os.environ if environ is None else environ
    path = os.path.join(home, "env")
    try:
        with open(path, encoding="utf-8") as f:
            lines = f.read().splitlines()
    except OSError:
        return []
    added = []
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip()
        if key.startswith("export "):
            key = key[7:].strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        if key and key not in environ:
            environ[key] = value
            added.append(key)
    return added
