# 永久保存的水庫資料

由 GitHub Actions 每 3 小時自動更新，一律只新增、不刪除；同一天同一座水庫只保留一列。
水利署 API 只提供最近 24 小時與前一天的資料，因此永久紀錄從第一次執行當天開始累積，無法補回更早的資料。

## 檔案

| 路徑 | 內容 | 來源資料集 |
| --- | --- | --- |
| `realtime/YYYY-MM.csv` | 每座水庫每天一列：當天最後一筆水情觀測 | 水庫水情資料 |
| `daily/YYYY-MM.csv` | 每座水庫每天一列：水利署公布的前一日營運統計 | 水庫每日營運狀況 |

## realtime 欄位

| 欄位 | 說明 |
| --- | --- |
| date | 日期 (YYYY-MM-DD) |
| reservoir_id | 水庫代碼 |
| name | 水庫名稱 |
| observed_time | 該筆觀測時間 (HH:MM)，同一天以最晚的一筆為準 |
| water_level_m | 水位 (公尺) |
| effective_storage | 有效蓄水量 (萬立方公尺) |
| inflow_discharge | 進水量 |
| total_outflow | 總出水量 |

## daily 欄位

| 欄位 | 說明 |
| --- | --- |
| date | 日期 (YYYY-MM-DD) |
| reservoir_id | 水庫代碼 |
| name | 水庫名稱 |
| capacity | 有效容量 |
| full_water_level_m | 滿水位 (公尺) |
| dead_water_level_m | 死水位 (公尺) |
| inflow | 進水量 |
| outflow | 出水量 |
| outflow_total | 總出水量 |
| crossflow | 越域引水量 |
| basin_rainfall | 集水區降雨量 |

空白代表水利署該欄沒有數值。檔案依 date、reservoir_id 排序，編碼 UTF-8，換行 LF。
