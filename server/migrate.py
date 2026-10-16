"""老数据 → 新结构。

这是整个重构里最不可逆的一步，所以它被写得**只读老目录、只写新目录**，
而且**可以随时重跑**（id 全是确定性的，已经搬过的会跳过）。

搬什么：

    data/humans.json（一个数组，15 位）
        ↓  按「名字 + 人格正文」分组
    templates/tpl_*.json        人格蓝图（同名同文的人共用一份）
        ↓  每一位旧数字人 = 一个生命
    humans/dh_*/identity.json   身份：来自哪个模板、绑哪家 API、什么时候出生

    threads/*.json（桌子的视角）
        ↓  按「他在这场对话里亲历了什么」重新组织
    humans/dh_*/sessions/<会话>.jsonl   对话记录（带**原始时间戳**）

为什么一定要补历史（不能省）：不补的话，15 位生命全是"今天出生"的，
用户打开会觉得他们集体失忆了。而他们的过去完全能从老数据里重建——
每一轮都记了谁说的、什么时候说的。

**这一版不生成长期记忆条目**（memory/*.md）——记忆内容怎么编排还没定，
而后面的提取是"从对话记录里提炼"，随时可以重跑。先把原始记录搬对。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from . import presets, store, templates
from .life import Life


@dataclass
class Report:
    humans_total: int = 0  # 老数据里一共几位数字人
    templates_created: int = 0  # 新建了几份蓝图（文件数，不是数字人数）
    templates_reused: int = 0  # 有几位数字人用了别人已经建好的蓝图
    lives_created: int = 0
    lives_skipped: int = 0
    sessions_written: int = 0
    entries_backfilled: int = 0
    entries_skipped: int = 0  # 调用失败或压根没答的座位：没有内容就不记
    providers: int = 0
    # 模板 → 用它的生命们（用来展示"同一份蓝图，两个个体"）
    sharing: dict[str, list[str]] = field(default_factory=dict)
    # 模板名 → 不同的模板 id（同名但正文不同）
    name_clashes: dict[str, list[str]] = field(default_factory=dict)

    def lines(self) -> list[str]:
        shared = {k: v for k, v in self.sharing.items() if len(v) > 1}
        n_shared = sum(len(v) for v in shared.values())
        out = [
            f"数字人：{self.humans_total} 位（这次搬了 {self.lives_created} 位，"
            f"跳过已经搬过的 {self.lives_skipped} 位）",
            f"蓝图：新建 {self.templates_created} 份"
            f"（{n_shared} 位共用同一份，{self.humans_total - n_shared} 位是独一份）",
            f"对话记录：写入 {self.sessions_written} 个会话文件，"
            f"补进 {self.entries_backfilled} 条历史"
            f"（跳过 {self.entries_skipped} 条：调用失败的、和压根没答的座位）",
            f"平台：{self.providers} 个（密钥原样带过来）",
        ]
        if shared:
            out.append("共用同一份蓝图的生命（同一份人格，两个个体）：")
            for tpl_id, names in shared.items():
                out.append(f"  · {tpl_id} ← {', '.join(names)}")
        clashed = {k: v for k, v in self.name_clashes.items() if len(v) > 1}
        if clashed:
            out.append("同名但正文不同的蓝图（老数据里就是这样，不是迁移造成的）：")
            for name, ids in clashed.items():
                out.append(f"  · {name}：{len(ids)} 份 → {', '.join(ids)}")
        return out


def _read_old_json(path: Path, fallback):
    """读老目录里的 JSON——**解析失败也不碰磁盘**。

    不能用 `store.read_json`：它解析失败时会把原文件改名归档（那是对源目录的写操作）。
    老目录在这次迁移里必须一个字节都不变，所以这里只读、失败就退回默认值并出声。
    """
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return fallback
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        print(f"[migrate] 读不出 {Path(path).name}（{e}），这一项按默认值处理，文件没有被动过")
        return fallback


def _read_old_humans(src: Path) -> list[dict]:
    """老项目的 humans.json 是**顶层数组**（不是 {humans: [...]}）。"""
    data = _read_old_json(src / "humans.json", [])
    if isinstance(data, dict):  # 兼容将来可能的包装
        data = data.get("humans", [])
    return list(data)


def _read_old_threads(src: Path) -> list[dict]:
    d = src / "threads"
    if not d.exists():
        return []
    out = []
    for p in sorted(d.glob("*.json")):
        got = _read_old_json(p, None)
        if got is None:
            continue
        out.append(got)
    return out


def migrate(src, dst, *, dry_run: bool = False) -> Report:
    """把老数据搬成新结构。`src`/`dst` 都是数据目录。

    - **只读 src**，一个字节都不改
    - 已经搬过的生命会跳过（靠确定性 id），所以可以放心重跑
    """
    src, dst = Path(src), Path(dst)
    rep = Report()

    # ── 平台与密钥：原样带过来（只动版本号）──
    old_settings = _read_old_json(src / "settings.json", {"providers": [], "advanced": {}})
    providers = old_settings.get("providers", [])
    rep.providers = len(providers)
    if not dry_run:
        store.write_json(
            dst / "settings.json",
            {
                "version": presets.DATA_VERSION,
                "providers": providers,
                "advanced": {**presets.DEFAULT_SETTINGS["advanced"], **old_settings.get("advanced", {})},
            },
        )

    # ── 人格 → 模板（同名同文共用一份）──
    humans = _read_old_humans(src)
    rep.humans_total = len(humans)
    tpl_of: dict[str, str] = {}  # 旧 human id → 模板 id
    seen: set[str] = set()  # 这一趟里已经算过的模板（试跑时磁盘上什么都没写，只能靠它）
    for h in humans:
        tpl = templates.make(
            h.get("name", "未命名"),
            h.get("system", "") or "",
            emoji=h.get("emoji", "🙂"),
            tagline=h.get("tagline", ""),
            tags=list(h.get("tags") or []),
            bare=bool(h.get("bare")),
            builtin=bool(h.get("builtin")),
            origin="imported",
        )
        tpl_of[h["id"]] = tpl.id
        rep.name_clashes.setdefault(tpl.name, [])
        if tpl.id not in rep.name_clashes[tpl.name]:
            rep.name_clashes[tpl.name].append(tpl.id)
        exists = tpl.id in seen or templates.template_path(dst, tpl.id).exists()
        if exists:
            rep.templates_reused += 1
        else:
            rep.templates_created += 1
            seen.add(tpl.id)
            if not dry_run:
                templates.save(dst, tpl)
        rep.sharing.setdefault(tpl.id, []).append(h.get("name", "?"))

    # ── 每一位旧数字人 → 一个生命 ──
    threads = _read_old_threads(src)
    for h in humans:
        old_id = h["id"]
        life_id = templates.stable_id("dh", old_id)
        root = dst / "humans" / life_id
        if (root / "identity.json").exists():
            rep.lives_skipped += 1
            continue  # 已经搬过（重跑时走这里）

        rep.lives_created += 1
        identity = {
            "id": life_id,
            "templateId": tpl_of[old_id],
            "providerId": h.get("providerId") or "",
            "model": h.get("model") or "",
            "name": h.get("name", "未命名"),
            "emoji": h.get("emoji", "🙂"),
            "bornAt": int(h.get("createdAt") or 0),
        }
        # 老数据里"通用"那两位自带 temperature——**不能静默丢掉用户的设置**。
        # 新界面的温度是座位级的，先原样留在身份里，等接上请求那层再决定怎么用。
        if h.get("temperature") is not None:
            identity["temperature"] = h["temperature"]

        if dry_run:
            # 试跑也要**算出**会写什么（用户会先跑试跑看结果），只是不碰磁盘
            life = Life(id=life_id, root=root, identity=identity)
        else:
            life = Life.create(
                dst / "humans",
                template_id=identity["templateId"],
                provider_id=identity["providerId"],
                model=identity["model"],
                name=identity["name"],
                emoji=identity["emoji"],
                life_id=life_id,
                born_at=identity["bornAt"],
            )
            if "temperature" in identity:
                life.identity["temperature"] = identity["temperature"]
                store.write_json(root / "identity.json", life.identity)

        # ── 补历史：他在每场对话里亲历了什么 ──
        for th in threads:
            seats = th.get("seats") or []
            mine = [s for s in seats if s.get("humanId") == old_id]
            if not mine:
                continue
            session_id = th.get("id") or "th_unknown"
            council = th.get("mode") == "council"
            entries: list[dict] = []

            for rnd in sorted(th.get("rounds") or [], key=lambda r: r.get("at") or 0):
                at = int(rnd.get("at") or 0)
                q = (rnd.get("question") or "").strip()
                if q:
                    entries.append({"t": at, "kind": "you", "text": q, "imported": True})

                answers = rnd.get("answers") or []
                for seat in mine:
                    # 会议模式下，他听得见排在他前面的人的发言（并排模式听不见）
                    if council:
                        for a in answers:
                            if a.get("seatId") == seat.get("id"):
                                break  # 到他自己了，后面的人还没说
                            if (a.get("content") or "").strip() and not a.get("error"):
                                entries.append({
                                    "t": at,
                                    "kind": "heard",
                                    "who": a.get("name") or "某位",
                                    "text": a["content"],
                                    "imported": True,
                                })
                    ans = next((a for a in answers if a.get("seatId") == seat.get("id")), None)
                    if ans and (ans.get("content") or "").strip() and not ans.get("error"):
                        entries.append({
                            "t": at + int(ans.get("ms") or 1000),
                            "kind": "self",
                            "text": ans["content"],
                            "imported": True,
                        })
                    else:
                        rep.entries_skipped += 1

            if not entries:
                continue
            path = life.session_path(session_id)
            # 已经有这个会话文件就不重复追加（重跑安全）
            if path.exists():
                rep.entries_skipped += len(entries)
                continue
            if not dry_run:
                for e in entries:
                    store.append_jsonl(path, e)
            rep.sessions_written += 1
            rep.entries_backfilled += len(entries)

    return rep


def main(argv: list[str] | None = None) -> int:
    """命令行入口：py -3 -m server.migrate <老数据目录> <新数据目录> [--dry-run]"""
    import sys

    args = list(argv if argv is not None else sys.argv[1:])
    dry = "--dry-run" in args
    args = [a for a in args if a != "--dry-run"]
    src = args[0] if args else "D:/AAsanboxForDsh/data"
    dst = args[1] if len(args) > 1 else "data"
    rep = migrate(src, dst, dry_run=dry)
    print(f"{'（试跑，没有写任何东西）' if dry else '迁移完成'}")
    for line in rep.lines():
        print(f"  {line}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
