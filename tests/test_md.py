"""static/md.js 렌더러 — node 로 돌린다(없으면 건너뛴다)."""
import json
import os
import shutil
import subprocess
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
NODE = shutil.which("node")


def render(src):
    js = ("global.window={};require(%s);process.stdout.write(window.CodexMd.render(%s))"
          % (json.dumps(os.path.join(ROOT, "static", "md.js")), json.dumps(src)))
    return subprocess.run([NODE, "-e", js], capture_output=True, text=True, check=True).stdout


@unittest.skipUnless(NODE, "node 없음")
class MdTest(unittest.TestCase):
    def test_details_summary_same_line(self):
        out = render("앞 글\n<details><summary>데모 정리 명령</summary>\n\n해당 `source` 폴더에서:\n\n```\nkill %1\n```\n\n</details>\n뒤 글")
        self.assertIn('<details class="md-details"><summary>데모 정리 명령</summary>', out)
        self.assertIn("</details>", out)
        self.assertNotIn("&lt;details", out)
        self.assertNotIn("&lt;/details", out)
        self.assertLess(out.index("<code>source</code>"), out.index("</details>"))
        self.assertLess(out.index("</details>"), out.index("뒤 글"))

    def test_summary_next_line_open_and_unclosed(self):
        out = render("<details open>\n<summary>**로그**</summary>\n- 하나\n- 둘")
        self.assertIn('<details class="md-details" open><summary><strong>로그</strong></summary>', out)
        self.assertNotIn("&lt;summary", out)
        self.assertTrue(out.rstrip().endswith("</details>"))   # 닫는 태그가 없어도 닫는다

    def test_no_summary_and_stray_close(self):
        out = render("<details>\n본문\n</details>\n</details>")
        self.assertIn("<summary>자세히</summary>", out)
        self.assertEqual(out.count("</details>"), 1)
        self.assertIn("&lt;/details&gt;", out)                 # 짝 없는 닫는 태그는 글자 그대로

    def test_br_and_other_tags_escaped(self):
        out = render("| a | b |\n|---|---|\n| 한 줄<br>두 줄 | `<br>` |\n\n<script>x</script>")
        self.assertIn("한 줄<br>두 줄", out)
        self.assertIn("<code>&lt;br&gt;</code>", out)          # 코드 안은 그대로
        self.assertIn("&lt;script&gt;", out)


if __name__ == "__main__":
    unittest.main()
