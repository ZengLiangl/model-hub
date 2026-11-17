# -*- coding: utf-8 -*-
"""hub/tools/normalize_catalog.py —— 把引擎目录缓存里的易变字段归一化（第 249 轮）。

为什么需要它：`hub/engines/<engine>/catalog/product-config-*.json` 是引擎把上游
`/v3/config` **原样落盘**的存档。上游每次回包都带一个新的 `requestId`，于是这份
被 git 跟踪的文件**每轮都会“有改动”** —— 打开 `git status` 永远脏着一条，
提交历史里堆满“只有 requestId 变了”的空提交（实测 2026-11-17 连推两次都是它）。

这个工具只做一件事：把这些**每次必变、且没人读**的字段换成固定占位符，
让“内容真的变了”和“又轮换了一次”在 diff 里区分开。

只改这些键（默认仅**顶层**，因为实测 requestId 只在顶层）：
  · requestId / request_id / reqId / traceId / trace_id

用法：
    python hub/tools/normalize_catalog.py            # 只报告：哪些文件需要归一化
    python hub/tools/normalize_catalog.py --check     # CI 用：需要归一化就退出码 1
    python hub/tools/normalize_catalog.py --write     # 真的写回（原子替换）

配套用法（本机）：
    git update-index --skip-worktree hub/engines/wb/catalog/product-config-cn.json
    # 需要提交真实目录更新时：--no-skip-worktree，跑本工具 --write，再提交

退出码：0 = 已归一化/无需改动；1 = --check 且有文件需要归一化；2 = 有文件读写失败。
"""
import argparse
import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))          # hub/tools
HUB = os.path.dirname(HERE)                                # hub/
REPO = os.path.dirname(HUB)                                # 融合仓根

#: 每轮必变、且网关自己不读的字段（归一化成固定占位符）
VOLATILE_KEYS = ("requestId", "request_id", "reqId", "traceId", "trace_id")
#: 占位符：一眼看出「这里被归一化过」，且与任何真实 id 都不会撞
PLACEHOLDER = "normalized-by-hub-tools"


def catalog_files(root=None):
    """默认扫 hub/engines/*/catalog/*.json（各引擎的落盘副本）。"""
    root = root or os.path.join(HUB, "engines")
    out = []
    if not os.path.isdir(root):
        return out
    for name in sorted(os.listdir(root)):
        cat = os.path.join(root, name, "catalog")
        if not os.path.isdir(cat):
            continue
        for fn in sorted(os.listdir(cat)):
            if fn.endswith(".json"):
                out.append(os.path.join(cat, fn))
    return out


def normalize_doc(doc):
    """把顶层的易变字段换成占位符；返回 (新文档, 被改的键名列表)。**不修改入参**。"""
    if not isinstance(doc, dict):
        return doc, []
    touched = []
    out = dict(doc)
    for key in VOLATILE_KEYS:
        if key in out and out[key] != PLACEHOLDER:
            out[key] = PLACEHOLDER
            touched.append(key)
    return out, touched


def _read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def _dump_like(original_text, doc):
    """按原文件的排版习惯写回：单行 + `", "` 分隔（与引擎 json.dumps 默认一致）。

    刻意不重新缩进：这份文件是**存档**，diff 越小越好；缩进一变整份都会“改”。
    """
    single_line = "\n" not in original_text.strip()
    text = json.dumps(doc, ensure_ascii=False,
                      indent=None if single_line else 2,
                      sort_keys=False)
    return text + ("\n" if original_text.endswith("\n") else "")


def _write_atomic(path, text):
    fd, tmp = tempfile.mkstemp(prefix=".normalize-", dir=os.path.dirname(path) or ".")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="归一化引擎目录缓存里的易变字段（requestId 之类）")
    ap.add_argument("files", nargs="*", help="指定文件；默认 hub/engines/*/catalog/*.json")
    ap.add_argument("--write", action="store_true", help="真的写回（默认只报告）")
    ap.add_argument("--check", action="store_true", help="有文件需要归一化时退出码 1（CI 用）")
    ap.add_argument("--json", action="store_true", help="机器可读输出")
    args = ap.parse_args(argv)

    targets = args.files or catalog_files()
    if not targets:
        print("没找到任何 catalog 缓存文件（hub/engines/*/catalog/*.json）")
        return 0

    changed, failed = [], []
    for path in targets:
        try:
            raw = _read(path)
            doc = json.loads(raw)
        except (OSError, ValueError) as exc:
            failed.append((path, str(exc)))
            continue
        new_doc, touched = normalize_doc(doc)
        if not touched:
            continue
        changed.append((path, touched))
        if args.write:
            try:
                _write_atomic(path, _dump_like(raw, new_doc))
            except OSError as exc:
                failed.append((path, str(exc)))

    if args.json:
        print(json.dumps({"changed": [{"path": p, "keys": k} for p, k in changed],
                          "failed": [{"path": p, "error": e} for p, e in failed],
                          "written": bool(args.write)}, ensure_ascii=False, indent=2))
    else:
        verb = "已归一化" if args.write else "需要归一化"
        for path, keys in changed:
            print("%s %s（%s）" % (verb, os.path.relpath(path, REPO), ", ".join(keys)))
        for path, err in failed:
            print("读/写失败 %s：%s" % (os.path.relpath(path, REPO), err), file=sys.stderr)
        if not changed and not failed:
            print("全部已归一化，没有需要改的文件")

    if failed:
        return 2
    if args.check and changed:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
