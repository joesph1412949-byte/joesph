# -*- coding: utf-8 -*-
"""策略声明键消费自检: JSON 里声明的键, 生产代码里到底有没有读取点?

用法:
    python -m prism.strategy_lint [--strategy <id|path>] [--all] [--json] [--root <dir>]

默认扫 prism/strategies/*.json(排除 .active.json 本地指针)。
退出码: 全部键都有读取点 → 0; 存在未消费键 → 1(可挂 CI/手工检查); 策略不存在 → 2。

判定依据(可解释的静态证据, 不猜):
1. `declared_keys` 递归策略 JSON, 取每个 dict 键及 JSON 路径(如 scoring_models[0].weight)。
2. `read_keys` 用 **ast** 解析生产 .py, 只收"按字符串键读"的位置:
   `x["k"]`(Load)、`x.get("k")` / `.pop` / `.setdefault`、`"k" in x`。
   - ast 里没有注释, docstring 是 Expr 常量节点、不落在上述位置 →
     散文里的同名词不会误伤(本仓血泪: 注释里出现同名词曾误判)。
   - 写入(`x["k"] = ...`)与 dict 字面量(`{"k": ...}`)不算消费。
   - 裸字符串(`DOC = "k"`)、关键字实参、`**cfg` 展开不算消费。
3. 键名**精确相等**比较(不做子串/前缀): `weight` 与 `weights` 天然分开。
4. 判定粒度 = 叶子键名, 不是全路径: 通用键名(如 name)会被别处无关命中掩盖 →
   本工具报出的未消费键是**下界**(宁少勿滥); 未报出不等于所有声明取值都生效
   (如 composite.cap 在 mode="average" 下曾被忽略 —— 这类"读了但没作用于该分支"
   靠静态搜键名看不出来, 需人工复核)。
5. 只看 .py: prism_web/static/*.js 与模板不在扫描内(已知盲区)。
"""
import argparse
import ast
import json
import sys
from pathlib import Path

STRATEGIES_DIR = Path(__file__).resolve().parent / "strategies"
REPO_ROOT = Path(__file__).resolve().parent.parent
# 生产代码扫描根(六个生产包 + qmt: qmt/tools/live_check.py:271-282 真读
# execution/sell_rules —— 漏掉它会把 pick_slot/open_window 误报成未消费)。
SCAN_ROOTS = ("prism", "shared", "datasource", "backtest", "prism_web",
              "tt_solo", "qmt")

_READ_ATTRS = ("get", "pop", "setdefault")

# 手工白名单: 键名 → "理由; 证据: <file:line>"。只收静态搜不到的**动态**消费者
# (如按结构遍历 payload)。加条目必须在理由里给出证据位置, 不许凭印象。
DYNAMIC_CONSUMERS = {
    "pick_slot": (
        "理由: qmt/tools/live_check.py 用变量遍历键名元组后 ex.get(k), 字面量键"
        "不在读取位置 → 静态搜不到; 但它只校验键存在, 取值未生效"
        "(守护时点见 prism/schedule.py:10 硬编码); 证据: qmt/tools/live_check.py:272"),
}


# ---------------------------------------------------------------- 声明侧
def _is_leaf(v):
    """容器键(值为 dict, 或含 dict 的列表)不单独判定: 它的消费由子键体现。"""
    if isinstance(v, dict):
        return False
    return not (isinstance(v, list) and any(isinstance(x, dict) for x in v))


def declared_keys(obj, path=""):
    """JSON → [(路径, 键名)]; 列表用 [i] 标位; 容器键跳过(只报叶子声明)。"""
    out = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            p = "%s.%s" % (path, k) if path else str(k)
            if _is_leaf(v):
                out.append((p, str(k)))
            out.extend(declared_keys(v, p))
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            out.extend(declared_keys(v, "%s[%d]" % (path, i)))
    return out


# ---------------------------------------------------------------- 消费侧
def _key_reads(tree):
    """AST → (行号, 键名, 读法节点)。只认字符串常量键的读取位置。"""
    for n in ast.walk(tree):
        if isinstance(n, ast.Subscript) and isinstance(n.ctx, ast.Load) \
                and isinstance(n.slice, ast.Constant) \
                and isinstance(n.slice.value, str):
            yield n.lineno, n.slice.value, n
        elif isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) \
                and n.func.attr in _READ_ATTRS and n.args \
                and isinstance(n.args[0], ast.Constant) \
                and isinstance(n.args[0].value, str):
            yield n.lineno, n.args[0].value, n
        elif isinstance(n, ast.Compare) \
                and any(isinstance(o, ast.In) for o in n.ops):
            for c in [n.left] + list(n.comparators):
                if isinstance(c, ast.Constant) and isinstance(c.value, str):
                    yield n.lineno, c.value, n


def _is_test(rel):
    parts = rel.split("/")
    return "tests" in parts[:-1] or parts[-1].startswith("test_") \
        or parts[-1] == "conftest.py"


def read_keys(root, roots=None):
    """扫生产 .py → ({键名: [证据...]}, 解析成功的文件数)。

    roots=None → 扫 root 下全部 .py(测试用); CLI 传 SCAN_ROOTS。
    """
    base = Path(root)
    hits, n_files = {}, 0
    for r in (roots if roots is not None else ("",)):
        top = base / r if r else base
        if not top.is_dir():
            continue
        for p in sorted(top.rglob("*.py")):
            rel = p.relative_to(base).as_posix()
            if _is_test(rel) or "__pycache__" in rel:
                continue
            try:
                src = p.read_text(encoding="utf-8")
                tree = ast.parse(src)
            except (OSError, UnicodeDecodeError, SyntaxError):
                continue
            n_files += 1
            for lineno, key, node in _key_reads(tree):
                hits.setdefault(key, []).append(
                    "%s:%d %s" % (rel, lineno, ast.unparse(node)[:120]))
    return hits, n_files


# ---------------------------------------------------------------- 判定
def lint_file(path, root=REPO_ROOT, roots=SCAN_ROOTS):
    """单份策略 → {strategy, file, scanned_files, unconsumed, consumed, hints}。"""
    path = Path(path)
    data = json.loads(path.read_text(encoding="utf-8"))
    hits, n_files = read_keys(root, roots)
    declared = {}
    for jp, k in declared_keys(data):
        declared.setdefault(k, []).append(jp)
    unc, con = [], []
    for k in sorted(declared):
        ev = hits.get(k) or []
        ent = {"key": k, "paths": declared[k], "hits": ev}
        if ev:
            con.append(ent)
        elif k in DYNAMIC_CONSUMERS:
            ent["whitelist"] = DYNAMIC_CONSUMERS[k]
            con.append(ent)
        else:
            unc.append(ent)
    sid = (data.get("id") or path.stem) if isinstance(data, dict) else path.stem
    return {"strategy": sid,
            "file": path.as_posix(), "scanned_files": n_files,
            "hints": [h for h in [_cap_hint(data)] if h],
            "unconsumed": unc, "consumed": con}


# ---------------------------------------------------------------- 二阶观察
def _weight_bound(data):
    """weighted_sum 的理论上限 Σ(层权重 × 对齐后因子权重和)。

    与引擎校验器同式(prism/engine.py:435), 因子条目权重的对齐规则同
    engine._compute_scores(字符串形态按位读 m["weights"], 否则读 item["weight"])。
    只有这一种模式的上限能**只从 JSON** 算出, 故其余模式返回 None(不硬猜)。
    """
    comp = data.get("composite") or {}
    if comp.get("mode") != "weighted_sum" or not data.get("scoring_models"):
        return None
    total = 0.0
    for m in data["scoring_models"]:
        ws = [float(x) for x in (m.get("weights") or [])]
        s = 0.0
        for idx, item in enumerate(m.get("factors") or []):
            if isinstance(item, str):
                s += ws[idx] if idx < len(ws) else 1.0
            else:
                s += float((item or {}).get("weight", 1.0))
        total += float(m.get("weight", 1.0)) * s
    return round(total, 4)


def _cap_hint(data):
    """cap 被读取 ≠ cap 生效: 声明值 ≥ 理论上限时 min(total, cap) 永不触发。"""
    try:
        bound = _weight_bound(data)
    except (TypeError, ValueError):
        return None
    cap = ((data.get("composite") or {}).get("cap")
           if isinstance(data, dict) else None)
    if bound is None or not isinstance(cap, (int, float)):
        return None
    return ("composite.cap=%s %s weighted_sum 理论上限 %s → %s"
            % (cap, ">=" if cap >= bound else "<", bound,
               "恒不生效(惰性配置: min(total, cap) 永不触发)" if cap >= bound
               else "会触发"))


def lint_all(root=REPO_ROOT, strategies_dir=None, roots=SCAN_ROOTS):
    d = Path(strategies_dir) if strategies_dir else Path(root) / "prism" / "strategies"
    return [lint_file(p, root=root, roots=roots)
            for p in sorted(d.glob("*.json")) if not p.name.startswith(".")]


# ---------------------------------------------------------------- CLI
def _resolve(spec):
    for cand in (Path(spec), STRATEGIES_DIR / ("%s.json" % spec),
                 STRATEGIES_DIR / spec):
        if cand.is_file():
            return cand
    return None


def _fmt(rep):
    lines = ["== %s  (%s)" % (rep["strategy"], rep["file"]),
             "   扫描 %d 个 .py(已排除 tests*/test_*.py)" % rep["scanned_files"]]
    unc, con = rep["unconsumed"], rep["consumed"]
    lines.append("   未消费 %d / 声明 %d" % (len(unc), len(unc) + len(con)))
    for h in rep["hints"]:
        lines.append("   提示(不计入退出码): %s" % h)
    for e in unc:
        lines.append("     [X] %s" % e["key"])
        lines.append("         声明于: %s" % _join(e["paths"]))
        lines.append("         依据: 键名精确搜 %r → 生产代码 0 个读取点" % e["key"])
    if con:
        lines.append("   已消费 %d:" % len(con))
        for e in con:
            wl = "  [白名单: %s]" % e["whitelist"] if "whitelist" in e else ""
            lines.append("     [v] %s%s" % (e["key"], wl))
            lines.append("         声明于: %s" % _join(e["paths"]))
            for h in e["hits"][:2]:
                lines.append("         读取点: %s" % h)
            if len(e["hits"]) > 2:
                lines.append("         读取点: ...(共 %d 处, 见 --json)"
                             % len(e["hits"]))
    return "\n".join(lines)


def _join(paths):
    return ", ".join(paths[:4]) + (" ...(共 %d 处)" % len(paths)
                                   if len(paths) > 4 else "")


_CAVEAT = """注: 判定粒度=叶子键名精确相等(weight ≠ weights); 报出 = 全仓生产 .py 零读取点。
    "有读取点"不等于声明值一定生效(如 composite.cap 在 mode=average 下曾被忽略);
    键名被别处无关命中掩盖时不报(下界, 宁少勿滥); 只看 .py, 网页 JS/模板不在扫描内。"""


def main(argv=None):
    ap = argparse.ArgumentParser(
        prog="python -m prism.strategy_lint",
        description="策略 JSON 声明键消费自检(只报告, 不改任何文件)")
    ap.add_argument("--strategy", help="策略 id 或 JSON 路径(默认扫全部)")
    ap.add_argument("--all", action="store_true", help="扫 prism/strategies/ 全部(默认)")
    ap.add_argument("--root", default=str(REPO_ROOT),
                    help="生产代码扫描根(默认仓库根; 换 root 可复核历史提交状态)")
    ap.add_argument("--json", action="store_true", dest="as_json")
    a = ap.parse_args(argv)
    root = Path(a.root)
    if a.strategy and not a.all:
        p = _resolve(a.strategy)
        if p is None:
            print("找不到策略: %s" % a.strategy, file=sys.stderr)
            return 2
        reports = [lint_file(p, root=root)]
    else:
        reports = lint_all(root=root)
    if a.as_json:
        print(json.dumps(reports, ensure_ascii=False, indent=1))
    else:
        print("\n".join(_fmt(r) for r in reports))
        print("\n" + _CAVEAT)
        n = sum(len(r["unconsumed"]) for r in reports)
        print("\n合计 %d 个未消费键(退出码 %d)。" % (n, 1 if n else 0))
    return 1 if any(r["unconsumed"] for r in reports) else 0


if __name__ == "__main__":
    sys.exit(main())
