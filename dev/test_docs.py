"""文档一致性测试：**不让文档慢慢变成谎话**。

    py -3 dev/test_docs.py

为什么值得单独一套：接手的人看的是文档。文档一旦和代码对不上，比没有文档更坏——
他会照着一条不存在的命令敲半天，然后开始怀疑整个项目。
手工核一次只能管一次，所以把它变成断言：

  1. **接口表**：应用里有的接口，README 里必须有；README 里写的，应用里必须真的有。
     （最常见的漂移：加了个接口，忘了写文档；或者删了个接口，文档还留着）
  2. **链接**：文档之间互相引用的相对链接，目标必须存在。
  3. **提到的文件**：文档里点名的源码/脚本路径必须存在（防重命名之后忘了改文档）。
  4. **可调参数表**：开发指南里列的每个常量，必须在它说的那个文件里真的找得到。
  5. **版本号只有一个家**：`server/__init__.py` 的 `__version__` 要和 README 里写的一致。
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

ROOT = Path(__file__).resolve().parent.parent
DOCS = ["README.md", "开发指南.md", "CHANGELOG.md", "数字生命与记忆设计.md"]

passed = failed = 0


def check(cond, msg):
    global passed, failed
    if cond:
        passed += 1
        print(f"  ✅ {msg}")
    else:
        failed += 1
        print(f"  ❌ {msg}")


def read(name: str) -> str:
    return (ROOT / name).read_text(encoding="utf-8")


def main() -> int:
    # ── 1. 接口表 vs 应用里真实的接口 ──
    print("── 接口表和应用对得上吗 ──")
    from server import main as appmod

    real = {getattr(r, "path", "") for r in appmod.app.routes if getattr(r, "path", "").startswith("/api")}
    documented: set[str] = set()
    for name in DOCS:
        for line in read(name).splitlines():
            # 讲老项目的接口不算数（那些接口在新应用里本来就不存在）
            if "老项目" in line or "老接口" in line:
                continue
            for m in re.finditer(r"/api/[A-Za-z0-9_\-/{}]*", line):
                # 文档里的占位符写得短（{id}），代码里是 {provider_id} —— 归一化再比
                documented.add(re.sub(r"\{[^}]*\}", "{}", m.group(0).rstrip("/")))
    real_norm = {re.sub(r"\{[^}]*\}", "{}", p) for p in real}

    missing = sorted(real_norm - documented)
    extra = sorted(documented - real_norm)
    check(not missing, f"应用里 {len(real_norm)} 个接口，文档里都写了" +
          (f"，但缺：{missing}" if missing else ""))
    check(not extra, "文档里写的接口都真的存在" + (f"，但多出：{extra}" if extra else ""))

    # ── 2. 文档之间的相对链接 ──
    print("\n── 链接都能点开吗 ──")
    bad_links: list[str] = []
    for name in DOCS:
        for m in re.finditer(r"\[[^\]]*\]\(([^)]+)\)", read(name)):
            target = m.group(1).strip()
            if target.startswith(("http://", "https://", "#", "mailto:")):
                continue
            path = (ROOT / target.split("#")[0]).resolve()
            if not path.exists():
                bad_links.append(f"{name} → {target}")
    check(not bad_links, "文档里互相引用的链接都在" + (f"，坏的：{bad_links}" if bad_links else ""))

    # ── 3. 文档里点名的文件 ──
    print("\n── 文档点名的文件都还在吗 ──")
    pattern = re.compile(r"`((?:dev|server|web|docs)/[A-Za-z0-9_\-./]+|run\.py|启动圆桌\.cmd)`")
    bad_paths: list[str] = []
    for name in DOCS:
        for m in pattern.finditer(read(name)):
            p = m.group(1)
            if "*" in p or p.endswith("/"):
                continue
            if not (ROOT / p).exists():
                bad_paths.append(f"{name} → {p}")
    check(not bad_paths, "文档点名的文件都存在" + (f"，找不到：{bad_paths}" if bad_paths else ""))

    # ── 4. 可调参数表 vs 代码 ──
    print("\n── 可调参数表还是真的吗 ──")
    guide = read("开发指南.md")
    rows = re.findall(r"^\|\s*`([\w./]+)`\s*\|\s*`([A-Z_][A-Z0-9_]*)`", guide, re.M)
    check(len(rows) >= 12, f"开发指南里列了 {len(rows)} 条可调参数")
    wrong: list[str] = []
    for fname, const in rows:
        path = ROOT / "server" / fname
        if not path.exists():
            wrong.append(f"{fname}（文件不存在）")
            continue
        if const not in path.read_text(encoding="utf-8"):
            wrong.append(f"{fname} 里没有 {const}")
    check(not wrong, f"{len(rows)} 条参数都在它说的那个文件里" + (f"，对不上：{wrong}" if wrong else ""))

    # ── 5. 版本号只有一个家 ──
    print("\n── 版本号 ──")
    from server import __version__

    check(f"{__version__}" in read("README.md") or "0.5." not in read("README.md"),
          f"README 里没有和 __version__（{__version__}）打架的版本号")
    check(__version__ in read("CHANGELOG.md"),
          f"CHANGELOG 里有 {__version__} 这一版")
    main_src = read("server/main.py")
    check(not re.search(r'"version"\s*:\s*"0\.', main_src),
          "main.py 里没有写死的版本号（读的是 __version__）")
    check('from . import __version__' in main_src, "main.py 确实在 import __version__")
    import json

    pkg = json.loads(read("web/package.json"))
    check(pkg.get("version") == __version__,
          f"前端 package.json 的版本（{pkg.get('version')}）和后端一致")

    # ── 6. Windows 启动脚本 ──
    #
    # 这一节是被一次真实的双击失败逼出来的：启动器里写了中文，而 cmd.exe
    # 按 **OEM 代码页**（中文 Windows 上是 936）读文件、文件本身是 UTF-8，
    # 于是"echo"被解码成一个多字节字符的尾巴 + "o"，报出
    # "'o' 不是内部或外部命令" —— 离真正的原因十万八千里。
    print("\n── Windows 启动脚本 ──")
    for name in ("启动圆桌.cmd",):
        raw = (ROOT / name).read_bytes()
        high = [b for b in raw if b > 127]
        check(not high, f"{name} 是纯 ASCII（cmd.exe 按 GBK 读 UTF-8 会吃字符，"
                        f"中文必须交给 Python 打印）")
        check(b"\r\n" in raw or raw.count(b"\n") == 0, f"{name} 用的是 CRLF")
    attrs = read(".gitattributes")
    check("*.cmd text eol=crlf" in attrs, ".gitattributes 声明了 .cmd 用 CRLF 检出")
    check((ROOT / ".editorconfig").exists(), "有 .editorconfig（免得各人编辑器存出不同的换行/编码）")

    # ── 7. 不该有的编码痕迹 ──
    print("\n── 编码 ──")
    # 只看**源码和文档**：data/ 是运行时产物（老数据里本来就有 CRLF 的文件，
    # 而且水位线是按字节算的，**不能**回头去改它），dev/.tmp* 是测试草稿
    skip_exact = {"node_modules", ".git", "dist", "data"}

    def skipped(p) -> bool:
        return any(part in skip_exact or part.startswith(".tmp") for part in p.parts)

    boms = []
    crlf = []
    for p in ROOT.rglob("*"):
        if not p.is_file() or skipped(p):
            continue
        if p.suffix not in (".py", ".ts", ".vue", ".md", ".json", ".css", ".html", ".yml", ".yaml", ".cmd"):
            continue
        raw = p.read_bytes()
        if raw[:3] == b"\xef\xbb\xbf":
            boms.append(p.name)
        if p.suffix != ".cmd" and b"\r\n" in raw:
            crlf.append(str(p.relative_to(ROOT)))
    check(not boms, "没有源码/文档带 UTF-8 BOM" + (f"：{boms}" if boms else ""))
    check(not crlf, "除了 .cmd 之外没有源码/文档是 CRLF（.gitattributes 定了 LF）" +
          (f"：{crlf[:5]}" if crlf else ""))

    print(f"\n{'=' * 46}\n通过 {passed}，失败 {failed}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
