"""运行核心、协议、HTTP 和可选的浏览器测试。"""

import argparse
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parent.parent


def main():
    """每套测试独立运行并汇总退出码；--unit 不起服务，--ui 额外驱动浏览器。"""
    parser = argparse.ArgumentParser(description="运行数字生命测试")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--unit", action="store_true", help="只运行无需网络的测试")
    mode.add_argument("--ui", action="store_true", help="包含浏览器测试")
    parser.add_argument("--cdp-port", type=int, help="连接已有浏览器调试端口")
    args = parser.parse_args()
    if args.cdp_port and not args.ui:
        parser.error("--cdp-port 需要同时指定 --ui")
    failed = []
    for path in sorted((ROOT / "tests").glob("test_*.py")):
        if path.stem == "test_ui" and not args.ui:
            continue
        if path.stem == "test_http" and args.unit:
            continue
        # 复用调用本命令的 Python 和 UTF-8 模式，避免 Windows 默认编码干扰中文断言。
        command = [sys.executable, "-X", "utf8", str(path)]
        if path.stem == "test_ui" and args.cdp_port:
            command += ["--cdp-port", str(args.cdp_port)]
        print(f"运行 {path.name}", flush=True)
        if subprocess.run(command, cwd=ROOT).returncode:
            failed.append(path.name)
    print(f"失败套件：{', '.join(failed) or '无'}")
    return int(bool(failed))


if __name__ == "__main__":
    raise SystemExit(main())
