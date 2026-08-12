# -*- coding: utf-8 -*-
"""开发探针: 确认东财接口真实字段(运行前勿提交测试依赖)。用真实网络, 一次性。

字段确认(已记录的结论见 .superpowers/sdd/probe-findings.md; 本脚本可直接复验):
  ① ztpool(getTopicZTPool, date 必须) → data.pool[]: c(代码)/n(名称)/hybk(题材)/hyb
  ② lhb(datacenter RPT_DAILYBILLBOARD_DETAILSNEW) → result.data[]:
     SECURITY_CODE / TRADE_DATE / BILLBOARD_NET_AMT
  ③ rzrq(datacenter RPT_RZRQ_LSHJ) → result.data[]: 融资数据(不可用, S5 fail-open)
  ④ gdhs(datacenter RPT_HOLDERNUMLATEST) → result.data[]: HOLDER_NUM_CHANGE
  ⑤ gg(np-anotice-stock ann) → data.list[]: title / notice_date
  ⑥ slist(slist/get) → data.diff[]: f14=板块名; data.total=板块总数
"""
import json
import requests


def _first_list(d):
    """在响应 dict 中定位数据列表(result.data / data.pool / data.list / data.diff)。"""
    if not isinstance(d, dict):
        return None
    result = d.get("result")
    if isinstance(result, dict) and isinstance(result.get("data"), list) and result["data"]:
        return result["data"]
    data = d.get("data")
    if isinstance(data, dict):
        for key in ("pool", "list", "diff", "data"):
            v = data.get(key)
            if isinstance(v, dict):          # 单元素接口可能返回 dict 而非 list
                return [v]
            if isinstance(v, list) and v:
                return v
    return None


def dump(label, url, params=None):
    try:
        r = requests.get(url, params=params, timeout=6,
                         headers={"User-Agent": "Mozilla/5.0",
                                  "Referer": "http://quote.eastmoney.com/"})
        d = r.json()
        items = _first_list(d)
        if items:
            print("[%s] top keys: %s" % (label, sorted(d.keys())))
            print("[%s] list keys: %s" % (label, sorted(items[0].keys())))
            print("[%s] first: %s" % (label, json.dumps(items[0], ensure_ascii=False)[:300]))
        else:
            # 找不到列表 → 打印顶层键集, 让人看到真实形状
            print("[%s] no data: %s" % (label, str(d)[:300]))
    except Exception as e:
        print("[%s] ERROR: %r" % (label, e))


if __name__ == "__main__":
    # ① 涨停池条目: 确认概念/题材字段名(c/n/hybk 之外)。date=YYYYMMDD 必须, 否则无数据。
    dump("ztpool", "https://push2ex.eastmoney.com/getTopicZTPool",
         {"ut": "7eea3edcaed734bea9cbfc24409ed989", "dpt": "wz.ztzt",
          "Pageindex": 0, "pagesize": 5, "sort": "fbt:asc", "date": "20260811"})
    # ② 龙虎榜
    dump("lhb", "https://datacenter-web.eastmoney.com/api/data/v1/get",
         {"reportName": "RPT_DAILYBILLBOARD_DETAILSNEW", "columns": "ALL",
          "pageSize": 5, "pageNumber": 1, "sortColumns": "TRADE_DATE",
          "sortTypes": "-1"})
    # ③ 融资融券历史(个股)
    dump("rzrq", "https://datacenter-web.eastmoney.com/api/data/v1/get",
         {"reportName": "RPT_RZRQ_LSHJ", "columns": "ALL",
          "filter": '(SCODE="000001")', "pageSize": 5, "pageNumber": 1})
    # ④ 股东户数(个股)
    dump("gdhs", "https://datacenter-web.eastmoney.com/api/data/v1/get",
         {"reportName": "RPT_HOLDERNUMLATEST", "columns": "ALL",
          "filter": '(SECURITY_CODE="000001")', "pageSize": 5, "pageNumber": 1})
    # ⑤ 个股公告
    dump("gg", "https://np-anotice-stock.eastmoney.com/api/security/ann",
         {"sr": "-1", "page_size": "5", "page_index": "1",
          "ann_type": "A", "client_source": "web", "page_number": "1",
          "f_node": "0", "s_node": "0", "stock_list": "000001"})
    # ⑥ 概念板块(slist): Y5 字段确认(f14=板块名, data.total=总数)
    dump("slist", "https://push2.eastmoney.com/api/qt/slist/get",
         {"spt": "3", "pi": 0, "po": 1, "np": 1, "fltt": 2, "invt": 2,
          "fid": "f3", "secid": "0.000001", "fields": "f12,f13,f14"})
