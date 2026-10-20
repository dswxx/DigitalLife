"""运行核心、协议、HTTP 和可选的浏览器测试。"""

import argparse
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parent.parent


def main():
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
