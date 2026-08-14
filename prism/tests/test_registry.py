# -*- coding: utf-8 -*-
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

import pytest

import prism.registry as reg


def test_factor_decorator_registers():
    reg.reset()
    @reg.factor(id="T1", name="测试因子", category="通用", description="x")
    def compute(ctx):
        return {"score": 0, "note": ""}
    assert "T1" in reg.FACTORS
    assert reg.FACTORS["T1"]["name"] == "测试因子"
    assert reg.FACTORS["T1"]["category"] == "通用"
    assert reg.FACTORS["T1"]["func"] is compute


def test_get_factor_unknown_raises():
    reg.reset()
    with pytest.raises(reg.UnknownFactorError):
        reg.get_factor("NO_SUCH_FACTOR")


def test_list_factors_sorted():
    reg.reset()
    @reg.factor(id="T2", name="b", category="通用", description="")
    def c2(ctx):
        return {"score": 0, "note": ""}
    @reg.factor(id="T1", name="a", category="通用", description="")
    def c1(ctx):
        return {"score": 0, "note": ""}
    ids = [f["id"] for f in reg.list_factors()]
    assert ids == ["T1", "T2"]


def test_scan_factors_finds_module(tmp_path, monkeypatch):
    # 临时包 prism_test_factors 里放 factor_abc.py, 扫描应注册 "TABC"
    pkg_dir = tmp_path / "prism_test_factors"
    pkg_dir.mkdir()
    (pkg_dir / "__init__.py").write_text("", encoding="utf-8")
    (pkg_dir / "factor_abc.py").write_text(
        "from prism.registry import factor\n"
        "@factor(id='TABC', name='扫描因子', category='通用', description='')\n"
        "def compute(ctx):\n"
        "    return {'score': 0, 'note': ''}\n",
        encoding="utf-8")
    monkeypatch.syspath_prepend(str(tmp_path))
    reg.reset()
    reg.scan_factors(package="prism_test_factors")
    assert "TABC" in reg.FACTORS


def test_scan_factors_force_reimports(tmp_path, monkeypatch):
    # 第一次 scan 后 reset + 修改模块文件 + force=True 重扫 → 新注册生效
    pkg_dir = tmp_path / "prism_test_force"
    pkg_dir.mkdir()
    (pkg_dir / "__init__.py").write_text("", encoding="utf-8")
    (pkg_dir / "factor_one.py").write_text(
        "from prism.registry import factor\n"
        "@factor(id='FORCE1', name='一', category='通用', description='')\n"
        "def compute(ctx):\n    return {'score': 0, 'note': ''}\n",
        encoding="utf-8")
    monkeypatch.syspath_prepend(str(tmp_path))
    reg.reset()
    reg.scan_factors(package="prism_test_force")
    assert "FORCE1" in reg.FACTORS
    # 改模块内容后 force 重扫
    reg.reset()
    (pkg_dir / "factor_one.py").write_text(
        "from prism.registry import factor\n"
        "@factor(id='FORCE2', name='二', category='通用', description='')\n"
        "def compute(ctx):\n    return {'score': 0, 'note': ''}\n",
        encoding="utf-8")
    reg.scan_factors(package="prism_test_force", force=True)
    assert "FORCE2" in reg.FACTORS
