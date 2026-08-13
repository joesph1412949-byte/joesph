# -*- coding: utf-8 -*-
"""数据层：封装 xtdata 的所有数据获取。
所有方法都通过 DataSource 实例调用，便于测试时注入 mock。"""
import time

try:
    from xtquant import xtdata
except Exception:
    xtdata = None


class DataSourceError(Exception):
    pass


def _safe(v, default):
    return v if v is not None else default


class DataSource:
    def __init__(self):
        self._connected = False

    # ---------- 连接 ----------
    def connect(self):
        if xtdata is None:
            raise DataSourceError("xtquant 未安装或无法导入")
        try:
            # 该 xtquant 版本无 xtdata.connect(); miniQMT 通过 RPC 自动连接,
            # 用一次轻量行情探针确认链路可用
            xtdata.get_sector_list()
            self._connected = True
        except Exception as e:
            raise DataSourceError("连接 QMT miniQMT 失败: %r" % e)

    # ---------- 实时行情 ----------
    def get_full_market_ticks(self, codes=None):
        if not self._connected:
            raise DataSourceError("未连接，请先调用 connect()")
        if codes is None:
            codes = self.get_sector_stocks("沪深A股")
        try:
            return xtdata.get_full_tick(codes) or {}
        except Exception as e:
            raise DataSourceError("拉全市场行情失败: %r" % e)

    # ---------- 涨停股 ----------
    def get_limit_up_stocks(self, ticks=None):
        """从全市场盘口找出涨停股。用 instrument_detail 的 UpStopPrice 判断涨停。
        返回 [{code,name,last,last_close,up_stop_price,sealed,amount,volume}]"""
        if ticks is None:
            ticks = self.get_full_market_ticks()
        ups = [c for c in ticks if (ticks[c].get("lastPrice") or 0) > 0]
        if not ups:
            return []
        details = self.get_instruments_bulk(ups)
        result = []
        for code in ups:
            t = ticks[code]
            last = t.get("lastPrice") or 0
            last_close = t.get("lastClose") or 0
            det = details.get(code) or {}
            up_price = det.get("UpStopPrice") or 0
            if last <= 0:
                continue
            # 涨停判断：现价 >= 涨停价（容差 0.01）
            if up_price > 0 and last < up_price - 0.01:
                continue
            if up_price <= 0:
                # 兜底：用板块涨幅粗算（几乎用不到）
                if last_close > 0:
                    r = 0.30 if code.startswith(("8","4")) else (0.20 if code.startswith(("300","301","688")) else 0.10)
                    if last < round(last_close * (1 + r), 2) - 0.01:
                        continue
            sealed = bool((t.get("askPrice") or [0])[0] == 0)
            result.append({
                "code": code,
                "name": det.get("InstrumentName") or code,
                "last": last,
                "last_close": last_close,
                "up_stop_price": up_price,
                "sealed": sealed,
                "amount": t.get("amount") or 0,
                "volume": t.get("volume") or 0,
                "float_volume": det.get("FloatVolume") or 0,
                "open_date": det.get("OpenDate") or "",
            })
        return result

    # ---------- 历史K线 ----------
    def get_kline(self, code, days=120):
        """拉日线K线（自动下载+读取），返回含 open/high/low/close/volume/amount 的 DataFrame"""
        if not self._connected:
            raise DataSourceError("未连接，请先调用 connect()")
        period = "1d"
        try:
            xtdata.download_history_data(code, period, start_time="", end_time="",
                                         incrementally=True)
            time.sleep(0.05)
            k = xtdata.get_market_data_ex([], [code], period=period,
                                          start_time="", end_time="", count=days)
            df = (k or {}).get(code)
            if df is None or len(df) == 0:
                # 首次可能需全量下载
                xtdata.download_history_data(code, period, incrementally=True)
                time.sleep(0.05)
                k = xtdata.get_market_data_ex([], [code], period=period,
                                              start_time="", end_time="", count=days)
                df = (k or {}).get(code)
            if df is None or len(df) == 0:
                raise DataSourceError("无法获取 %s 的K线" % code)
            return df
        except DataSourceError:
            raise
        except Exception as e:
            raise DataSourceError("获取 %s K线失败: %r" % (code, e))

    def get_kline_bulk(self, codes, days=250, period="1d"):
        """批量拉多只股票日K线, 返回 {code: DataFrame}。失败个股跳过。"""
        if not self._connected:
            raise DataSourceError("未连接，请先调用 connect()")
        codes = list(codes)
        if not codes:
            return {}
        try:
            xtdata.download_history_data2(codes, period, start_time="", end_time="")
            time.sleep(0.05)
            k = xtdata.get_market_data_ex([], codes, period=period,
                                          start_time="", end_time="", count=days)
            out = {}
            for c in codes:
                df = (k or {}).get(c)
                if df is not None and len(df) > 0:
                    out[c] = df
            return out
        except Exception as e:
            raise DataSourceError("批量获取K线失败: %r" % e)

    # ---------- instrument 详情 ----------
    def get_instrument(self, code):
        try:
            return xtdata.get_instrument_detail(code) or {}
        except Exception:
            return {}

    def get_instruments_bulk(self, codes):
        """批量获取 instrument_detail，返回 {code: detail}"""
        if not codes:
            return {}
        try:
            if hasattr(xtdata, "get_instrument_detail_list"):
                try:
                    d = xtdata.get_instrument_detail_list(codes) or {}
                    out = {}
                    for code_key, detail in d.items():
                        if isinstance(detail, dict) and detail.get("InstrumentID"):
                            out[code_key] = detail
                    for c in codes:
                        if c not in out:
                            out[c] = self.get_instrument(c)
                    return out
                except Exception:
                    return {c: self.get_instrument(c) for c in codes}
            return {c: self.get_instrument(c) for c in codes}
        except Exception:
            return {c: self.get_instrument(c) for c in codes}

    # ---------- 指数K线 ----------
    def get_index_kline(self, index_code, days=60):
        if not self._connected:
            raise DataSourceError("未连接，请先调用 connect()")
        try:
            xtdata.download_history_data(index_code, "1d", start_time="", end_time="",
                                         incrementally=True)
            time.sleep(0.05)
            k = xtdata.get_market_data_ex([], [index_code], period="1d",
                                          start_time="", end_time="", count=days)
            df = (k or {}).get(index_code)
            if df is None or len(df) == 0:
                raise DataSourceError("无法获取指数 %s 的K线" % index_code)
            return df
        except DataSourceError:
            raise
        except Exception as e:
            raise DataSourceError("获取指数 %s K线失败: %r" % (index_code, e))

    # ---------- 板块 ----------
    def get_sector_stocks(self, sector_name):
        try:
            return xtdata.get_stock_list_in_sector(sector_name) or []
        except Exception:
            return []
