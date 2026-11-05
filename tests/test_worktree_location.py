"""같은 이름의 원본 저장소와 외부 linked worktree의 실제 위치를 구별한다."""
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import worktree


class LocationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo = Path(self.tmp.name) / 'original' / 'same-name'
        self.repo.mkdir(parents=True)
        self.git('init', '-q', '-b', 'main')
        (self.repo / 'src').mkdir()
        (self.repo / 'src' / 'a.py').write_text('x = 1\n')
        self.git('add', '.')
        self.git('-c', 'user.name=test', '-c', 'user.email=test@local', '-c', 'commit.gpgsign=false',
                 'commit', '-qm', 'fixture')

    def git(self, *args):
        return subprocess.check_output(['git', '-C', str(self.repo), *args], text=True)

    def test_original_and_external_worktree_with_same_name(self):
        linked = Path(self.tmp.name) / 'review-1234' / self.repo.name
        self.git('worktree', 'add', '-q', '-b', 'review', str(linked))
        original = worktree.location(str(self.repo / 'src'))
        external = worktree.location(str(linked / 'src'))
        self.assertEqual(original, {'kind': 'repository', 'root': str(self.repo.resolve())})
        self.assertEqual(external, {'kind': 'worktree', 'root': str(linked.resolve()),
                                    'repo': str(self.repo.resolve())})
        # detached HEAD도 브랜치 유무와 관계없이 워크트리다.
        subprocess.check_call(['git', '-C', str(linked), 'checkout', '-q', '--detach'])
        self.assertEqual(worktree.location(str(linked)), external)

    def test_symlink(self):
        alias = Path(self.tmp.name) / 'alias'
        alias.symlink_to(self.repo, target_is_directory=True)
        self.assertEqual(worktree.location(str(alias))['root'], str(self.repo.resolve()))

    def test_plain_and_missing_folder(self):
        self.assertEqual(worktree.location(self.tmp.name), {'kind': 'folder'})
        self.assertEqual(worktree.location(str(self.repo / 'missing')), {'kind': 'unknown'})
        self.assertEqual(worktree.location(None), {'kind': 'unknown'})

    def test_git_timeout_does_not_break_session_read(self):
        with patch.object(worktree, '_git', side_effect=subprocess.TimeoutExpired('git', 2)):
            self.assertEqual(worktree.location(str(self.repo)), {'kind': 'unknown'})


if __name__ == '__main__':
    unittest.main()
