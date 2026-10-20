"""检查当前文档链接、发布版本与启动器格式。"""

import json
from pathlib import Path
import re
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from server import __version__

ROOT = Path(__file__).resolve().parent.parent


class DocsTests(unittest.TestCase):
    """只检查当前发布文档与启动文件，防止引用已删除路径或出现版本不一致。"""
    def test_current_document_links(self):
        for name in ("README.md", "docs/开发指南.md", "docs/系统设计.md"):
            path = ROOT / name
            for target in re.findall(r'\]\(([^)]+)\)', path.read_text(encoding="utf-8")):
                if "://" not in target and not target.startswith("#"):
                    self.assertTrue((path.parent / target.split("#")[0]).exists(), (name, target))

    def test_versions(self):
        self.assertEqual(json.loads((ROOT / "web/package.json").read_text())["version"], __version__)
        self.assertIn(__version__, (ROOT / "CHANGELOG.md").read_text(encoding="utf-8"))

    def test_windows_launcher(self):
        raw = (ROOT / "启动数字生命.cmd").read_bytes()
        self.assertTrue(raw.isascii())
        self.assertIn(b"\r\n", raw)


if __name__ == "__main__":
    unittest.main()
