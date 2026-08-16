# -*- coding: utf-8 -*-
"""因子体检: 注册/签名/假数据跑通 自动验证。

用法: python -m prism.factor_check
"""
import inspect
import sys


def _fake_context():
    """给因子一个最小假上下文(全部 None), 验证不崩溃。"""
    from prism.context import FactorContext
    return FactorContext(code="600000.SH")


def run_checks(scan=True):
    """返回 [(factor_id, ok, message)]。scan=True 时先扫描因子库。"""
    from prism import registry as reg
    if scan:
        reg.reset()
        # force=True: 进程可能已 import 过 prism.factors, 无 force 时 importlib
        # 一次性语义使重扫为 no-op → FACTORS 空 → 静默假阴性
        reg.scan_factors("prism.factors", force=True)
    out = []
    for fid, meta in sorted(reg.FACTORS.items()):
        func = meta["func"]
        try:
            params = inspect.signature(func).parameters
            if len(params) < 1:
                out.append((fid, False, "签名错误: compute 缺 ctx 参数"))
                continue
            res = func(_fake_context())
            if not isinstance(res, dict) or "score" not in res:
                out.append((fid, False, "返回值缺少 score: %r" % (res,)))
                continue
            out.append((fid, True, "OK score=%r" % res["score"]))
        except Exception as e:
            out.append((fid, False, "运行异常: %r" % e))
    return out


def main():
    results = run_checks()
    bad = [r for r in results if not r[1]]
    for fid, ok, msg in results:
        print("[%s] %s: %s" % ("PASS" if ok else "FAIL", fid, msg))
    if bad:
        print("\n%d 个因子体检失败!" % len(bad))
        return 1
    print("\n全部 %d 个因子体检通过。" % len(results))
    return 0


if __name__ == "__main__":
    sys.exit(main())
