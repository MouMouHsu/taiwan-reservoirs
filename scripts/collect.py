"""由 GitHub Actions 每 3 小時執行：抓取水利署水庫水情資料，
每座水庫每天保留一筆（當天最後一筆觀測），累積成最近 31 天的 history.json。

前一版 history.json 從已發布的網站讀回；讀取失敗（非 404）時直接中止，
不發布新版本，避免把累積的歷史資料覆蓋掉。
"""
import datetime
import json
import os
import time
import urllib.error
import urllib.request

API = "https://opendata.wra.gov.tw/api/v2/"
RT = "2be9044c-6e44-4856-aad5-dd108c2e6679"      # 水庫水情資料（最近 24 小時逐時）
DAILY = "51023e88-4c76-4dbc-bbb9-470da690d539"   # 水庫每日營運狀況（名稱、有效容量）
SITE_URL = os.environ.get("SITE_URL", "").rstrip("/") + "/"
OUT = os.environ.get("OUT", "_site/history.json")
KEEP_DAYS = 31


def get_json(url, allow_404=False):
    req = urllib.request.Request(url, headers={"User-Agent": "taiwan-reservoirs-collector"})
    last = None
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            if e.code == 404 and allow_404:
                return None
            last = e
        except Exception as e:  # 網路逾時等
            last = e
        time.sleep(10 * (attempt + 1))
    raise RuntimeError(f"無法取得 {url}：{last}")


def fetch_all(dataset):
    rows = []
    for page in range(1, 11):
        data = get_json(f"{API}{dataset}?page={page}&size=1000")
        if not isinstance(data, list):
            data = data.get("responseData") or data.get("data") or []
        rows += data
        if len(data) < 1000:
            break
    return rows


def num(v):
    try:
        return None if v in (None, "") else round(float(v), 3)
    except ValueError:
        return None


def main():
    previous = get_json(SITE_URL + "history.json", allow_404=True) if SITE_URL != "/" else None
    hist = previous["r"] if previous and previous.get("v") == 2 else {}
    print(f"既有歷史：{len(hist)} 座水庫")

    rt, daily = fetch_all(RT), fetch_all(DAILY)
    if not rt:
        raise RuntimeError("水庫水情資料回傳空資料，本次不更新")
    print(f"即時資料 {len(rt)} 筆，每日營運 {len(daily)} 筆")

    for d in daily:
        o = hist.setdefault(d["reservoiridentifier"], {"s": []})
        o["n"] = d.get("reservoirname") or o.get("n")
        o["c"] = num(d.get("capacity")) or o.get("c")

    # 每天一筆：[日期, 水位(m), 有效蓄水量(萬m³), 觀測時刻]，同一天以較晚的觀測覆蓋
    for r in rt:
        t = (r.get("observationtime") or "")[:16]
        lv = num(r.get("waterlevel"))
        if len(t) < 16 or lv is None:
            continue
        day, hm = t[:10], t[11:16]
        o = hist.setdefault(r["reservoiridentifier"], {"s": []})
        series = {p[0]: p for p in o["s"]}
        old = series.get(day)
        if old is None or hm >= old[3]:
            st = num(r.get("effectivewaterstoragecapacity"))
            if st is None and old is not None and old[3] == hm:
                st = old[2]
            series[day] = [day, lv, st, hm]
        o["s"] = list(series.values())

    now_tw = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=8))).replace(tzinfo=None)
    cutoff = (now_tw - datetime.timedelta(days=KEEP_DAYS)).strftime("%Y-%m-%d")
    for o in hist.values():
        o["s"] = sorted((p for p in o["s"] if p[0] >= cutoff), key=lambda p: p[0])

    os.makedirs(os.path.dirname(OUT) or ".", exist_ok=True)
    out = {"v": 2, "updated": now_tw.strftime("%Y-%m-%dT%H:%M"), "r": hist}
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, separators=(",", ":"))
    points = sum(len(o["s"]) for o in hist.values())
    print(f"已寫入 {OUT}：{len(hist)} 座水庫、{points} 筆每日資料、{os.path.getsize(OUT):,} bytes")


if __name__ == "__main__":
    main()
