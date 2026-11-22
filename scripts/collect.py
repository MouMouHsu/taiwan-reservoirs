"""由 GitHub Actions 每 3 小時執行：抓取水利署水庫水情資料。

1. 每座水庫每天保留一筆（當天最後一筆觀測），累積成最近 31 天的 history.json（網頁畫圖用）。
2. 同時把每天的資料永久存成 data/ 下的月份 CSV（只附加、依「日期 + 水庫代碼」去重）。

前一版 history.json 從已發布的網站讀回；讀取失敗（非 404）時直接中止，
不發布新版本，避免把累積的歷史資料覆蓋掉。CSV 若無法解析也直接中止，不覆寫既有資料。
"""
import csv
import datetime
import json
import os
import re
import time
import urllib.error
import urllib.request

API = os.environ.get("API_BASE", "https://opendata.wra.gov.tw/api/v2/")
RT = "2be9044c-6e44-4856-aad5-dd108c2e6679"      # 水庫水情資料（最近 24 小時逐時）
DAILY = "51023e88-4c76-4dbc-bbb9-470da690d539"   # 水庫每日營運狀況（名稱、有效容量）
SITE_URL = os.environ.get("SITE_URL", "").rstrip("/") + "/"
OUT = os.environ.get("OUT", "_site/history.json")
KEEP_DAYS = 31
DATA_DIR = os.environ.get("DATA_DIR", "data")
RETRY_WAIT = int(os.environ.get("RETRY_WAIT", "10"))
# 歷史查詢用的彙總檔（每次由 data/ 的 CSV 重新產生，只放在發布的網站內，不存回 repository）
Q_DIR = os.environ.get("Q_DIR") or os.path.join(os.path.dirname(OUT) or ".", "q")

# 永久資料表：欄位固定，只附加不改舊列
RT_COLS = ["date", "reservoir_id", "name", "observed_time", "water_level_m",
           "effective_storage", "inflow_discharge", "total_outflow"]
DAILY_COLS = ["date", "reservoir_id", "name", "capacity", "full_water_level_m", "dead_water_level_m",
              "inflow", "outflow", "outflow_total", "crossflow", "basin_rainfall"]
DAY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


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
        time.sleep(RETRY_WAIT * (attempt + 1))
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


def fmt(v):
    """數字轉成 CSV 文字；沒有資料就留空白。"""
    return "" if v is None else ("%.3f" % v).rstrip("0").rstrip(".")


def build_rt_rows(rt, names):
    """水情資料 → 每座水庫每天一列（當天最後一筆觀測）。"""
    best = {}
    for r in rt:
        t = (r.get("observationtime") or "")[:16]
        lv = num(r.get("waterlevel"))
        rid = r.get("reservoiridentifier")
        if len(t) < 16 or lv is None or not rid:
            continue
        key = (t[:10], rid)
        if key in best and best[key]["observed_time"] > t[11:16]:
            continue
        best[key] = {
            "date": t[:10], "reservoir_id": rid, "name": names.get(rid, ""), "observed_time": t[11:16],
            "water_level_m": fmt(lv), "effective_storage": fmt(num(r.get("effectivewaterstoragecapacity"))),
            "inflow_discharge": fmt(num(r.get("inflowdischarge"))), "total_outflow": fmt(num(r.get("totaloutflow"))),
        }
    return list(best.values())


def build_daily_rows(daily):
    """每日營運狀況 → 每座水庫每天一列（官方記錄前一日的定案值）。"""
    rows, skipped = {}, 0
    for d in daily:
        day = (d.get("datetime") or "")[:10]
        rid = d.get("reservoiridentifier")
        if not DAY_RE.match(day) or not rid:
            skipped += 1
            continue
        rows[(day, rid)] = {
            "date": day, "reservoir_id": rid, "name": d.get("reservoirname") or "",
            "capacity": fmt(num(d.get("capacity"))), "full_water_level_m": fmt(num(d.get("nwlmax"))),
            "dead_water_level_m": fmt(num(d.get("dwl"))), "inflow": fmt(num(d.get("inflow"))),
            "outflow": fmt(num(d.get("outflow"))), "outflow_total": fmt(num(d.get("outflowtotal"))),
            "crossflow": fmt(num(d.get("crossflow"))), "basin_rainfall": fmt(num(d.get("basinrainfall"))),
        }
    if skipped:
        print(f"警告：每日營運資料有 {skipped} 筆缺少日期或水庫代碼，已略過")
    return list(rows.values())


def known_names():
    """從既有 CSV 讀取 reservoir_id → name（較新的月份優先）。"""
    out = {}
    for kind in ("daily", "realtime"):
        d = os.path.join(DATA_DIR, kind)
        if not os.path.isdir(d):
            continue
        for fn in sorted(os.listdir(d)):
            if not fn.endswith(".csv"):
                continue
            try:
                with open(os.path.join(d, fn), encoding="utf-8", newline="") as f:
                    for row in csv.DictReader(f):
                        if row.get("name"):
                            out[row["reservoir_id"]] = row["name"]
            except OSError:
                pass
    return out


def save_table(kind, cols, new_rows, newer_wins=None):
    """把新資料併入 data/<kind>/YYYY-MM.csv：以（日期, 水庫代碼）去重、依日期與代碼排序。
    既有檔案欄位不符或無法解析時丟出錯誤，不覆寫。newer_wins 可自訂「新舊列誰取代誰」。"""
    by_month = {}
    for r in new_rows:
        by_month.setdefault(r["date"][:7], []).append(r)
    changed = 0
    for month, rows in sorted(by_month.items()):
        path = os.path.join(DATA_DIR, kind, month + ".csv")
        existing = {}
        if os.path.exists(path):
            with open(path, encoding="utf-8", newline="") as f:
                reader = csv.DictReader(f)
                if reader.fieldnames != cols:
                    raise RuntimeError(f"{path} 的欄位與預期不符，為避免覆寫既有資料而中止")
                for row in reader:
                    existing[(row["date"], row["reservoir_id"])] = row
        before = dict(existing)
        for r in rows:
            key = (r["date"], r["reservoir_id"])
            old = existing.get(key)
            if old is None or newer_wins is None or newer_wins(r, old):
                existing[key] = r
        if existing == before:
            continue
        changed += sum(1 for k, v in existing.items() if before.get(k) != v)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8", newline="") as f:
            w = csv.DictWriter(f, fieldnames=cols, lineterminator="\n")
            w.writeheader()
            for key in sorted(existing):
                w.writerow(existing[key])
        os.replace(tmp, path)
    return changed


RT_KEEP = ("reservoiridentifier", "observationtime", "waterlevel", "effectivewaterstoragecapacity",
           "inflowdischarge", "totaloutflow")


def write_snapshot(rt, daily, now_tw):
    """最新水情快照 latest.json：網頁直接連水利署失敗時的備援。
    每座水庫保留最新一筆觀測與最新一筆有蓄水量的觀測；每日營運取得失敗時沿用上一份快照。"""
    keep = {}
    for r in rt:
        rid, t = r.get("reservoiridentifier"), r.get("observationtime") or ""
        if not rid or not t:
            continue
        slim = {k: r.get(k) for k in RT_KEEP}
        o = keep.setdefault(rid, {})
        if t >= o.get("last", {}).get("observationtime", ""):
            o["last"] = slim
        if r.get("effectivewaterstoragecapacity") not in (None, "") and t >= o.get("st", {}).get("observationtime", ""):
            o["st"] = slim
    rows = {json.dumps(v, sort_keys=True): v for o in keep.values() for v in o.values()}
    if not daily and SITE_URL != "/":
        try:
            prev = get_json(SITE_URL + "latest.json", allow_404=True)
            daily = (prev or {}).get("daily") or []
        except RuntimeError:
            daily = []
    out = {"t": now_tw.strftime("%Y-%m-%dT%H:%M"), "rt": list(rows.values()), "daily": daily}
    path = os.path.join(os.path.dirname(OUT) or ".", "latest.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, separators=(",", ":"))
    return len(out["rt"]), len(daily)


def agg(vals):
    """[平均, 最低, 最高, 天數]；沒有數值時平均與極值為 None。"""
    v = [x for x in vals if x is not None]
    if not v:
        return [None, None, None, 0]
    return [round(sum(v) / len(v), 3), min(v), max(v), len(v)]


def build_query_files():
    """讀取全部永久 CSV，產生歷史查詢用的彙總檔：
    q/index.json            水庫清單、名稱、有效容量、資料起訖日
    q/<代碼>/<YYYY>.json     該年每天一筆 [日期, 水位, 有效蓄水量]
    q/<代碼>/m.json、y.json   每月、每年的 [期間, 水位 平均/最低/最高/天數, 蓄水量 平均/最低/最高/天數]"""
    days, names, caps = {}, {}, {}
    for kind in ("realtime", "daily"):
        d = os.path.join(DATA_DIR, kind)
        if not os.path.isdir(d):
            continue
        for fn in sorted(os.listdir(d)):
            if not fn.endswith(".csv"):
                continue
            with open(os.path.join(d, fn), encoding="utf-8", newline="") as f:
                for row in csv.DictReader(f):
                    rid = row["reservoir_id"]
                    if row.get("name"):
                        names[rid] = row["name"]
                    if kind == "realtime":
                        days.setdefault(rid, {})[row["date"]] = (num(row["water_level_m"]), num(row["effective_storage"]))
                    elif num(row.get("capacity")) is not None:
                        caps[rid] = num(row["capacity"])
    index = []
    for rid, dd in sorted(days.items()):
        dates = sorted(dd)
        base = os.path.join(Q_DIR, rid)
        os.makedirs(base, exist_ok=True)
        groups = {"y": {}, "m": {}}
        years = {}
        for day in dates:
            lv, st = dd[day]
            years.setdefault(day[:4], []).append([day, lv, st])
            for g, k in (("y", day[:4]), ("m", day[:7])):
                groups[g].setdefault(k, []).append((lv, st))
        for y, rows in years.items():
            with open(os.path.join(base, y + ".json"), "w", encoding="utf-8") as f:
                json.dump(rows, f, separators=(",", ":"))
        for g, gd in groups.items():
            out = [[k] + agg([a for a, _ in v]) + agg([b for _, b in v]) for k, v in sorted(gd.items())]
            with open(os.path.join(base, g + ".json"), "w", encoding="utf-8") as f:
                json.dump(out, f, separators=(",", ":"))
        index.append({"id": rid, "n": names.get(rid, ""), "c": caps.get(rid), "from": dates[0], "to": dates[-1]})
    with open(os.path.join(Q_DIR, "index.json"), "w", encoding="utf-8") as f:
        json.dump({"r": index}, f, ensure_ascii=False, separators=(",", ":"))
    return len(index)


def main():
    previous = get_json(SITE_URL + "history.json", allow_404=True) if SITE_URL != "/" else None
    hist = previous["r"] if previous and previous.get("v") == 2 else {}
    print(f"既有歷史：{len(hist)} 座水庫")

    rt = fetch_all(RT)
    if not rt:
        raise RuntimeError("水庫水情資料回傳空資料，本次不更新")
    # 每日營運只提供名稱與有效容量，既有歷史已有這些資料；暫時連不上時沿用舊值繼續
    try:
        daily = fetch_all(DAILY)
    except RuntimeError as e:
        print(f"警告：每日營運資料暫時無法取得，沿用既有的名稱與容量（{e}）")
        daily = []
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

    # 永久保存：先寫 CSV；若 CSV 有問題就在這裡中止，不發布新版本
    names = {rid: o.get("n") or "" for rid, o in hist.items()}
    for rid, nm in known_names().items():      # 補上 CSV 內已存的名稱（每日營運 API 失敗時的備援）
        if not names.get(rid):
            names[rid] = nm
    n_rt = save_table("realtime", RT_COLS, build_rt_rows(rt, names),
                      newer_wins=lambda new, old: new["observed_time"] >= old["observed_time"])
    n_daily = save_table("daily", DAILY_COLS, build_daily_rows(daily)) if daily else 0
    print(f"永久資料：水情新增或更新 {n_rt} 列，每日營運新增或更新 {n_daily} 列")
    print(f"歷史查詢彙總檔：{build_query_files()} 座水庫")

    now_tw = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=8))).replace(tzinfo=None)
    cutoff = (now_tw - datetime.timedelta(days=KEEP_DAYS)).strftime("%Y-%m-%d")
    for o in hist.values():
        o["s"] = sorted((p for p in o["s"] if p[0] >= cutoff), key=lambda p: p[0])

    os.makedirs(os.path.dirname(OUT) or ".", exist_ok=True)
    out = {"v": 2, "updated": now_tw.strftime("%Y-%m-%dT%H:%M"), "r": hist}
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, separators=(",", ":"))
    points = sum(len(o["s"]) for o in hist.values())
    print("最新水情快照：%d 筆觀測、%d 筆每日營運" % write_snapshot(rt, daily, now_tw))
    print(f"已寫入 {OUT}：{len(hist)} 座水庫、{points} 筆每日資料、{os.path.getsize(OUT):,} bytes")


if __name__ == "__main__":
    main()
