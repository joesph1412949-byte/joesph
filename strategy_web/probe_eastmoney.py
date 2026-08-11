# -*- coding: utf-8 -*-
"""开发探针: 确认东财接口真实字段(运行前勿提交测试依赖)。用真实网络, 一次性。"""
import json
import requests

def dump(label, url, params=None):
    try:
        r = requests.get(url, params=params, timeout=6,
                         headers={"User-Agent": "Mozilla/5.0",
                                  "Referer": "http://quote.eastmoney.com/"})
        d = r.json()
        data = (d.get("result") or {}).get("data") if isinstance(d, dict) else None
        if isinstance(data, list) and data:
            print("[%s] keys: %s" % (label, sorted(data[0].keys())))
            print("[%s] first: %s" % (label, json.dumps(data[0], ensure_ascii=False)[:400]))
        else:
            print("[%s] no data: %s" % (label, str(d)[:200]))
    except Exception as e:
        print("[%s] ERROR: %r" % (label, e))

if __name__ == "__main__":
    # ① 涨停池条目: 确认概念/题材字段名(c/n/continuousBoardCount 之外)
    dump("ztpool", "https://push2ex.eastmoney.com/getTopicZTPool",
         {"ut": "7eea3edcaed734bea9cbfc24409ed989", "dpt": "wz.ztzt",
          "Pageindex": 0, "pagesize": 5, "sort": "fbt:asc"})
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
