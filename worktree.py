"""새 세션을 원래 저장소가 아닌 git 워크트리에서 돌린다.

워크트리는 <데이터 홈>/worktrees/<짧은 ID>/<저장소 이름> 에 두고, 지금 체크아웃된 커밋(HEAD)에서
codex-lens/<날짜>-<짧은 ID> 브랜치를 새로 딴다. 폴더 끝 이름을 저장소 이름으로 두어 목록·머리 영역에
보이는 폴더 이름이 원래 저장소와 같게 한다.
"""

import os
import subprocess
import time
import uuid

GIT_TIMEOUT = 60


def _git(cwd, *args, timeout=GIT_TIMEOUT):
    return subprocess.run(["git", "-C", cwd] + list(args), capture_output=True, text=True, timeout=timeout)


def location(cwd):
    """읽는 세션의 실제 위치. 앱 밖에서 만든 linked worktree도 Git으로 판별한다.

    목록 전체를 읽을 때는 호출하지 않는다. status/파일 탐색 없이 저장소 메타데이터만 읽는다.
    """
    if not cwd or not os.path.isdir(cwd):
        return {"kind": "unknown"}
    try:
        result = _git(cwd, "rev-parse", "--path-format=absolute", "--show-toplevel",
                      "--absolute-git-dir", "--git-common-dir", timeout=2)
        if result.returncode:
            return {"kind": "folder"}
        root, git_dir, common = result.stdout.strip().splitlines()
        linked = os.path.realpath(git_dir) != os.path.realpath(common)
        out = {"kind": "worktree" if linked else "repository", "root": root}
        if linked:
            trees = _git(cwd, "worktree", "list", "--porcelain", "-z", timeout=2)
            first = trees.stdout.split("\0", 1)[0]
            if trees.returncode == 0 and first.startswith("worktree "):
                out["repo"] = first[len("worktree "):]
        return out
    except (OSError, ValueError, subprocess.SubprocessError):
        return {"kind": "unknown"}


def info(cwd):
    """cwd 가 git 작업 트리 안이면 {root, rel, branch, head, dirty}, 아니면 None."""
    cwd = os.path.abspath(os.path.expanduser(cwd or ""))
    if not os.path.isdir(cwd):
        return None
    try:
        top = _git(cwd, "rev-parse", "--show-toplevel", timeout=10)
        if top.returncode != 0 or not top.stdout.strip():
            return None
        root = top.stdout.strip()
        head = _git(cwd, "rev-parse", "--verify", "--quiet", "HEAD", timeout=10).stdout.strip()
        branch = _git(cwd, "symbolic-ref", "--quiet", "--short", "HEAD", timeout=10).stdout.strip()
        dirty = _git(cwd, "status", "--porcelain", "--untracked-files=normal", timeout=10).stdout.splitlines()
    except (OSError, subprocess.SubprocessError):
        return None
    rel = os.path.relpath(os.path.realpath(cwd), os.path.realpath(root))
    return {"root": root, "rel": "" if rel == "." else rel, "branch": branch or None, "head": head or None,
            "dirty": len(dirty)}


def create(cwd, base_dir):
    """cwd 의 저장소에서 새 워크트리를 만든다. 돌려주는 cwd 는 원래 cwd 에 해당하는 워크트리 안 폴더다."""
    gi = info(cwd)
    if not gi:
        raise ValueError("git 저장소가 아니라 워크트리를 만들 수 없습니다: %s" % cwd)
    if not gi["head"]:
        raise ValueError("커밋이 하나도 없는 저장소라 워크트리를 만들 수 없습니다: %s" % gi["root"])
    short = uuid.uuid4().hex[:8]
    branch = "codex-lens/%s-%s" % (time.strftime("%y%m%d"), short)
    path = os.path.join(base_dir, short, os.path.basename(gi["root"].rstrip(os.sep)) or "repo")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    try:
        out = _git(gi["root"], "worktree", "add", "-b", branch, path, gi["head"])
    except (OSError, subprocess.SubprocessError) as e:
        raise ValueError("워크트리를 만들지 못했습니다: %s" % e)
    if out.returncode != 0:
        raise ValueError("워크트리를 만들지 못했습니다: %s" % (out.stderr.strip() or out.stdout.strip())[:400])
    sub = os.path.join(path, gi["rel"]) if gi["rel"] else path
    return {"path": path, "cwd": sub if os.path.isdir(sub) else path, "branch": branch, "repo": gi["root"],
            "origin_cwd": os.path.abspath(os.path.expanduser(cwd)), "base_branch": gi["branch"], "base": gi["head"],
            "dirty": gi["dirty"]}


def remap(path, repo, wt_path):
    """원래 저장소 안의 절대 경로를 워크트리 안 경로로 바꾼다. 저장소 밖이면 그대로."""
    if not path:
        return path
    # git 은 심볼릭 링크를 푼 경로를 주므로(macOS 의 /var → /private/var) 양쪽 다 푼 뒤 견준다
    p = os.path.realpath(os.path.expanduser(path))
    r = os.path.realpath(repo)
    if p == r or p.startswith(r.rstrip(os.sep) + os.sep):
        return os.path.join(wt_path, os.path.relpath(p, r))
    return path


def remove(wt):
    """세션을 시작하지 못했을 때 방금 만든 워크트리와 브랜치를 걷어 낸다."""
    try:
        _git(wt["repo"], "worktree", "remove", "--force", wt["path"])
        _git(wt["repo"], "branch", "-D", wt["branch"])
    except (OSError, subprocess.SubprocessError):
        pass
