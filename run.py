"""本机启动入口：初始化数据、选择端口并启动服务。开发热重载使用 --reload。"""

from __future__ import annotations

import argparse
import os
import socket
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))


def free_port(preferred: int) -> tuple[int, bool]:
    """优先用你指定的端口；被占了就换一个。返回 `(端口, 是不是换过)`。"""
    for candidate in [preferred] + list(range(preferred + 1, preferred + 20)):
        with socket.socket() as s:
            try:
                s.bind(("127.0.0.1", candidate))
            except OSError:
                continue
            return candidate, candidate != preferred
    with socket.socket() as s:  # 实在不行让系统随便给一个
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1]), True


def main() -> int:
    ap = argparse.ArgumentParser(description="启动数字生命")
    ap.add_argument("--port", type=int, default=8787)
    ap.add_argument("--data", default=str(Path(__file__).resolve().parent / "data"),
                    help="数据目录（默认项目内 data/）")
    ap.add_argument("--reload", action="store_true", help="改了后端代码自动重启（开发用）")
    args = ap.parse_args()

    try:
        import uvicorn
    except ImportError:
        print("缺依赖。装一下（只有这三个）：\n")
        print("    py -3 -m pip install fastapi uvicorn httpx\n")
        print("装完再跑一次 py -3 run.py")
        return 1

    from server import main as appmod
    from server import presets, store, templates

    data_dir = Path(args.data).resolve()
    data_dir.mkdir(parents=True, exist_ok=True)

    # 第一次跑：把内置人格模板播进去（幂等，随时可以再跑）
    added = templates.seed(data_dir)
    if added:
        print(f"播了 {added} 份内置人格模板")

    if not (data_dir / "settings.json").exists():
        store.write_json(data_dir / "settings.json", {
            "version": presets.DATA_VERSION,
            "providers": [],
            "advanced": dict(presets.DEFAULT_SETTINGS["advanced"]),
            "modelInfo": {},
        })

    port, moved = free_port(args.port)
    url = f"http://127.0.0.1:{port}"

    print()
    print("  数字生命在跑：" + url)
    if moved:
        print(f"（{args.port} 端口被占了，换成了 {port}）")
    print(f"  数据目录：{data_dir}")
    print("  只监听本机，外网访问不到。关掉这个窗口就是停服务。")
    if not (Path(__file__).resolve().parent / "web" / "dist" / "index.html").exists():
        # 没构建过前端就直接说清楚怎么构建——不然用户打开看到的是一张提示页，
        # 还得自己回头翻文档才知道差哪一步
        print()
        print("  前端还没构建，所以现在打开只有一张提示页。构建一次就好：")
        print("      cd web")
        print("      npm install")
        print("      npm run build")
        print("  （之后正常启动不用再构建）")
    print()

    if args.reload:
        os.environ["DL_DATA_DIR"] = str(data_dir)
        uvicorn.run("server.main:app", host="127.0.0.1", port=port, log_level="warning",
                    reload=True, app_dir=str(Path(__file__).resolve().parent),
                    reload_dirs=[str(Path(__file__).resolve().parent / "server")])
    else:
        uvicorn.run(appmod.create_app(data_dir), host="127.0.0.1", port=port, log_level="warning")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
