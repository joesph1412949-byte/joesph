# -*- coding: utf-8 -*-
"""仪表盘前端契约测试: 静态读模板文件, 不启动 Flask。

为什么静态读而不是 Flask test_client: 页面是单文件原生 JS(无构建步骤),
"契约"就是文件里的字面量(区块 id / 本地 ECharts 路径 / confirm 强制 /
轮询周期)。起 Flask 只能额外证明 app.py 的 send_file 正常 —— 而
test_api.py::test_index_renders 已经在管这件事 —— 对"区块是否被删"
零增量。所以这里直接读文件: 最快、无副作用、失败信息最直白。
"""
from pathlib import Path

INDEX = Path(__file__).resolve().parents[1] / "templates" / "index.html"

# 五个验收区块 + 收益曲线容器的实际 id
BLOCK_IDS = ("gates", "account", "ladders", "battle", "rejections", "equity")


def _html():
    return INDEX.read_text(encoding="utf-8")


def test_five_blocks_and_equity_container_present():
    h = _html()
    for eid in BLOCK_IDS:
        assert f'id="{eid}"' in h, f"契约元素被删: id={eid}"


def test_echarts_local_and_no_external_url():
    h = _html()
    assert 'src="/static/echarts.min.js"' in h, "ECharts 不再走本地静态文件"
    low = h.lower()
    for bad in ("http://", "https://", "cdn", "unpkg", "jsdelivr"):
        assert bad not in low, f"页面出现外部依赖引用: {bad}"


def test_polling_interval_is_5000ms():
    assert "setInterval(load, 5000)" in _html()


def test_write_path_forces_confirm_and_buttons_use_it():
    h = _html()
    assert "async function post(" in h
    body = h.split("async function post(")[1].split("function togglePause")[0]
    # 强制确认必须在 post() 内, 且 body 展开在前(调用方无法覆盖 confirm)
    assert "{...body, confirm: true}" in body, "post() 不再强制 confirm: true"
    assert "Object.assign({confirm:true}" not in h, "退回可被覆盖的旧写法"
    # 两个写闸门按钮都必须走 post(); JS confirm() 二次确认仍在(双保险)
    assert "post('/api/pause'" in h and "post('/api/arm'" in h
    assert h.count("if(confirm(") == 2, "写闸门按钮的 JS 二次确认被改动"


def test_dry_run_qualified_when_from_config_not_daemon():
    """重算路径的 dry_run 只是 config 值, 不得当作实测事实呈现。"""
    h = _html()
    assert "_source === 'live_recompute'" in h
    assert "演练(不发单·按配置判定)" in h
    assert "实盘可发单(按配置判定)" in h
    # 行情源"未核实"必须是可见文本, 不能只藏在 title 悬浮里
    assert "行情源(自报·未核实)=" in h


def test_current_price_marker_between_rungs():
    """现价必须在 sell/buy 阶梯之间按价格插位, 而不是只写在卡头。"""
    h = _html()
    assert "▶ 现价" in h
    assert "rows.splice(" in h and "rows.findIndex(" in h
