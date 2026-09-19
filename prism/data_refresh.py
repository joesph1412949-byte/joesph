# -*- coding: utf-8 -*-
"""盘后数据刷新入口 —— 依次跑 market_data 的各 --build-* 采集段, 逐段诚实汇报。

为什么存在(09-19 核实): 六段数据(板块K线/资金流/全球指数/美债+VIX/美元指数/
资金惯性)没有任何 scheduler 自动刷新, 最后成功日期各异。

本入口只做"调度 + 核实", 不复制任何采集逻辑:
  * 逐段起子进程跑 `python -m prism.market_data <段参数>`, 拿**真实退出码**;
  * 每段前后各读一次盘(只读), 报该段数据日期与条目数 —— 这是唯一能识破
    "退出码说成功但没刷新成功"的证据;
  * 空数据单独分类, 绝不与成功混为一谈。

子进程退出码语义(09-19 A 批次起, market_data 6 个 --build-* 统一; 见其
模块 docstring / --help):
  0 = 该段本次真的取到/推进了数据; 1 = 该段真失败; 3 = 该段本次未推进
  (空数据, 旧缓存原样保留)。**未知退出码一律按失败**(保守)。
改前: `--build-sectors/--build-global/--build-sector-map/--build-futures`
是"打印完裸 return"(恒 exit 0, 板块级失败只落在日志里), 只有
`--build-flow-rank/--build-benchmark` 有语义 ⇒ 本入口过去只能靠读盘兜底,
现在退出码本身可信, 但仍与"缓存是否推进"**交叉验证**: 两者矛盾时如实
**同时**报出(不只信一个)。
  矛盾: 退出码 1 但缓存推进了 / 退出码 0 但该段为空 / 退出码 3 但缓存推进了;
  注记: 退出码 0 但末日未推进(同日重采、全量替换、非交易日都会这样, 不判死)。

退出码约定(本入口自身, 与上面子进程的码**不同**, 是聚合口径):
  0 = 全段成功; 1 = 至少一段失败; 2 = 无失败但有段空数据(失败优先于空数据)。
  dry-run 恒 0(它什么都不做)。

用法: `python -m prism.data_refresh [--dry-run]` / `python prism/data_refresh.py`

ponytail: 不做"该推进而未推进"的自动判死 —— 那需要交易日历(周末/节假日跑
必然不推进), 本入口只如实报"前→后"日期 + 退出码自称, 由人或上层判读。
"""
import argparse
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if __package__ in (None, ""):          # 支持 `python prism/data_refresh.py`
    sys.path.insert(0, str(ROOT))

# 段表(纯数据)。顺序 = 执行顺序: 板块列表是 sectors/kline/flow 的底座, 先采;
# 其余段互不依赖。keys = 该段跑完后到缓存里核哪几段(用于报日期与判空)。
SEGMENTS = (
    {"name": "sectors", "args": ("--build-sectors",),
     "keys": ("sectors", "kline", "flow"),
     "chain": "东财 eastmoney(默认) / 封禁时 --source sw(申万, 无 flow 段)"},
    {"name": "sector-map", "args": ("--build-sector-map",),
     "keys": ("sector_map",),
     "chain": "akshare 申万成分股(不依赖东财)"},
    {"name": "global-eastmoney", "args": ("--build-global",),
     "keys": ("global",),
     "chain": "东财 →(封禁时)--source sina(新浪无美元指数 UDI)"},
    {"name": "global-fred", "args": ("--build-global", "--source", "fred"),
     "keys": ("global",),
     "chain": "FRED DGS10/VIXCLS(东财源无美债/VIX)"},
    {"name": "flow-rank", "args": ("--build-flow-rank",),
     "keys": ("flow_rank",),
     "chain": "仅东财, 无降级源(封禁即失败); 空快照不落盘且算空数据"},
    {"name": "benchmark", "args": ("--build-benchmark",),
     "keys": ("benchmark",),
     "chain": "东财 → 通达信 get_index_bars → QMT xtdata; 三路皆空 = fail-open "
              "保留旧缓存, CLI 已点名非零退出"},
    {"name": "futures", "args": ("--build-futures",),
     "keys": ("futures",),
     "chain": "akshare 新浪主力连续(单品种失败静默跳过)"},
)

FAIL, EMPTY, OK, DRY = "FAIL", "EMPTY", "OK", "DRY"

# 子进程(market_data --build-*)退出码 → 该段**自称**的状态(09-19 A 批次统一口径)。
# 未知码(含 None/字符串)→ FAIL: 看不懂的退出码绝不当成功。
_RC_CLAIM = {0: OK, 1: FAIL, 3: EMPTY}


def _claim(rc):
    return _RC_CLAIM.get(rc, FAIL)


def _load_cache():
    """只读整份缓存 —— 复用 market_data 自己的读盘口径(非 strict, 坏文件→{}), 不复制路径/pickle 知识。"""
    from prism.market_data import _load_cache as _lc
    return _lc()


def _run_cmd(cmd):
    """**真正执行命令的那一层**(测试整体替换它)。原样返回 CompletedProcess, 不吞退出码。"""
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    return subprocess.run(cmd, cwd=str(ROOT), capture_output=True, text=True,
                          encoding="utf-8", errors="replace", env=env)


def _last_date(rec):
    ds = (rec or {}).get("dates") or []
    return str(ds[-1]) if ds else None


def _scan(cache, keys):
    """缓存 → {段键: (条目数, [(子键, 末日期)])}。

    两种形态: {dates:[...]} 的段(flow_rank/benchmark)折算成单条 ('', 末日期);
    {code: {dates:[...]}} 的段(kline/global/futures...)按子键各取末日期。
    """
    out = {}
    for k in keys:
        seg = (cache or {}).get(k)
        if not isinstance(seg, dict) or not seg:
            out[k] = (0, [])
        elif isinstance(seg.get("dates"), list):
            ds = [str(d) for d in seg["dates"]]
            out[k] = (len(ds), [("", ds[-1] if ds else None)])
        else:
            out[k] = (len(seg), [(str(c), _last_date(r)) for c, r in seg.items()])
    return out


def _child_txt(label, before, after):
    lbl = label or "·"
    if not after:
        return "%s 无" % lbl
    if before and before != after:
        return "%s %s→%s" % (lbl, before, after)
    return "%s %s" % (lbl, after)


def _fmt_key(key, before, after):
    n, rows = after
    if n == 0:
        return "%s: 空" % key
    last = max((d for _l, d in rows if d), default=None)
    txt = "%s: %d项%s" % (key, n, " 末 %s" % last if last else "")
    bmap = dict(before[1])
    if 1 < len(rows) <= 8:        # 小段(global 6 个指数)逐子键报日期才对得上"哪段停更"
        return txt + " [" + " ".join(
            _child_txt(lbl, bmap.get(lbl), d) for lbl, d in rows) + "]"
    b_last = max((d for _l, d in before[1] if d), default=None)
    return txt + (" (%s→%s)" % (b_last, last) if b_last and last != b_last else "")


def _tail(text, n=500):
    text = (text or "").strip()
    return text[-n:].replace("\n", "\n      ")


def _advanced(before, after):
    """该段缓存本次是否真的推进了(条目数或末日期变大)。"""
    return any(after.get(k, (0, "")) > before.get(k, (0, "")) for k in after)


def run_segment(seg, dry):
    """跑一段(或 dry-run 只读当前状态) → 结果 dict。

    分类 = 子进程退出码的自称(**可信**) 与 缓存实测 交叉验证后取严:
      * 自称失败 → FAIL; 自称未推进/缓存实测为空 → EMPTY; 否则 OK。
    两者冲突时**不改判也不隐瞒**: 记进 `mismatch` 一并打印(见模块 docstring)。
    """
    cmd = [sys.executable, "-m", "prism.market_data"] + list(seg["args"])
    before = _scan(_load_cache(), seg["keys"])
    res = {"name": seg["name"], "cmd": cmd, "chain": seg["chain"],
           "before": before, "after": before, "rc": None, "secs": 0.0,
           "cat": DRY, "err": "", "out": "", "mismatch": "", "note": ""}
    if dry:
        return res
    t0 = time.monotonic()
    try:
        cp = _run_cmd(cmd)
        res["rc"], res["out"], res["err"] = cp.returncode, cp.stdout, cp.stderr
    except Exception as e:                       # 起不动进程 = 失败(不吞)
        res["cat"], res["err"] = FAIL, "%s: %r" % (type(e).__name__, e)
        res["secs"] = time.monotonic() - t0
        return res
    res["secs"] = time.monotonic() - t0
    res["after"] = _scan(_load_cache(), seg["keys"])
    claim = _claim(res["rc"])                    # 退出码自称
    empty = sum(n for n, _ in res["after"].values()) == 0
    observed = EMPTY if empty else OK            # 缓存实测
    res["cat"] = FAIL if claim == FAIL else (EMPTY if empty else OK)
    adv = _advanced(before, res["after"])
    if claim == FAIL and adv:
        res["mismatch"] = ("退出码 %s 自称失败, 但缓存本次推进了" % res["rc"])
    elif claim == EMPTY and adv:
        res["mismatch"] = ("退出码 %s 自称未推进, 但缓存本次推进了" % res["rc"])
    elif claim == OK and empty:
        res["mismatch"] = "退出码 0 自称成功, 但该段缓存为空"
    elif claim == OK and not adv and any(n for n, _ in before.values()):
        # 不是矛盾(同日重采/全量替换/非交易日都会不推进), 只如实并列两个信号
        res["note"] = "退出码 0 自称成功, 但缓存末日未推进(同日重采/非交易日可能如此)"
    return res


def _print_segment(i, r, dry):
    tag = {OK: "OK", FAIL: "失败", EMPTY: "空数据", DRY: "计划"}[r["cat"]]
    print("\n[%d/%d] %s  %s" % (i, len(SEGMENTS), r["name"], tag))
    print("  命令: %s" % " ".join(r["cmd"]))
    print("  数据源/降级链: %s" % r["chain"])
    if dry:
        print("  数据(当前, 未执行): %s"
              % " | ".join(_fmt_key(k, r["before"].get(k, (0, [])),
                                    r["before"].get(k, (0, [])))
                           for k in r["before"]))
        return
    print("  耗时 %.1fs  退出码 %s(= %s)"
          % (r["secs"], r["rc"], {OK: "成功", FAIL: "失败", EMPTY: "未推进/空数据"}
             .get(_claim(r["rc"]), "未知码⇒按失败")))
    print("  数据: %s" % (" | ".join(
        _fmt_key(k, r["before"].get(k, (0, [])), r["after"].get(k, (0, [])))
        for k in r["after"]) or "无"))
    if r.get("mismatch"):
        print("  ⚠ 退出码与缓存证据矛盾: %s" % r["mismatch"])
    if r.get("note"):
        print("  注: %s" % r["note"])
    if r["err"]:
        print("  子进程 stderr 尾部: %s" % _tail(r["err"]))
    if r["cat"] != OK and r["out"]:
        print("  子进程 stdout 尾部: %s" % _tail(r["out"], 300))


def _summary(results):
    fails = [r["name"] for r in results if r["cat"] == FAIL]
    empties = [r["name"] for r in results if r["cat"] == EMPTY]
    oks = [r["name"] for r in results if r["cat"] == OK]
    print("\n汇总: 成功 %d 段 / 空数据 %d 段 / 失败 %d 段 (共 %d 段)"
          % (len(oks), len(empties), len(fails), len(results)))
    print("  成功: %s" % (", ".join(oks) or "(无)"))
    print("  空数据: %s" % (", ".join(empties) or "(无)"))
    print("  失败: %s" % (", ".join(fails) or "(无)"))
    code = 1 if fails else (2 if empties else 0)
    print("退出码: %d = %s" % (code, {0: "全段成功", 1: "存在失败段",
                                      2: "存在空数据段(空数据≠成功)"}[code]))
    return code


def main(argv=None):
    try:
        sys.stdout.reconfigure(encoding="utf-8")     # 子进程输出与自身都按 utf-8
    except Exception:
        pass
    ap = argparse.ArgumentParser(
        description="盘后数据刷新: 逐段跑 prism.market_data 的 --build-* 并核实数据日期")
    ap.add_argument("--dry-run", action="store_true",
                    help="只打印将要执行什么(含各段当前数据日期), 不执行、不落盘")
    args = ap.parse_args(argv)
    print("== 盘后数据刷新 == dry-run=%s 段数=%d 解释器=%s"
          % (args.dry_run, len(SEGMENTS), sys.executable))
    results = [run_segment(s, args.dry_run) for s in SEGMENTS]
    for i, r in enumerate(results, 1):
        _print_segment(i, r, args.dry_run)
    if args.dry_run:
        print("\ndry-run: 以上 %d 段均未执行, 未落盘。" % len(SEGMENTS))
        return 0
    return _summary(results)


if __name__ == "__main__":
    raise SystemExit(main())
