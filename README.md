# fuelcore

依《規格_轉檔核心.md》2026-11-06 外部版實作。每個 TXT 寫入一張報單。只供測試資料庫使用。

## 在 Windows 試跑

在專案目錄開啟 PowerShell，準備 Python 3.8 以上。

```powershell
Copy-Item fuelcore.ini.example fuelcore.ini
python -m fuelcore schema --config fuelcore.ini
python -m fuelcore convert 'C:\路徑\D4122_TEST_0001.txt' --config fuelcore.ini
```

命令列也接受 `--db`。每檔輸出結果與說明，任一檔失敗時程式結束碼為 1。

```python
from fuelcore import Settings, convert_file, convert_files

settings = Settings.load("fuelcore.ini")
result = convert_file("sample.txt", settings)
```

## MySQL 測試庫

把範例設定改為 `driver = mysql`，填入測試主機、帳密和兩個測試資料庫名稱。名稱須以 `test_` 開頭，或以 `_test`／`_test_code` 結尾。請使用只有測試庫權限的帳號。建表工具讀取附帶的外部版 CSV，也接受 `--csv` 指定來源。MySQL 建表工具只輸出 SQL。請在測試伺服器套用 SQL 後再執行轉檔。

```powershell
python -m pip install ".[mysql]"
python -m fuelcore schema --config fuelcore.ini --output fuelcore_test.sql
```

MySQL 報單表與代碼表使用同一個連線和交易。船舶代碼維護若發生可回復錯誤，回滾該段並留下說明。SQLite 把有小數位的欄位存成定點文字，以保留尾端的零供測試核對。

## 驗證與產生測試檔

```powershell
$env:FUELCORE_SAMPLES = 'C:\路徑\轉檔核心規格包\資料\txt樣本'
python -m unittest discover -s tests -v
python -m fuelcore.generate $env:FUELCORE_SAMPLES .\generated --seed 55
```

## 待確認與資料差異

- Q1–Q5 的 TODO 集中於 `fuelcore/convert.py`。主檔稅則讀 I2，項次讀設定，付款方式固定 08，數量依第 4 節取整數，未列欄位交由資料庫給預設值，報單品名換行讀油品表。
- CSV 的 FOREX 與 B52_項次稅費 都是 5 位小數。依第 4 節先分別取 3 位與 4 位，再補零到欄位精度。
- CSV 只有九張報單表的欄位，缺少正式代碼表結構和欄位預設值。工具以第 5 節列出的欄位建立測試代碼表。正式庫欄位預設值需要內部確認。
- 第 0 節說正式代號及公司名由設定檔提供，第 4.1 節則列出賣方欄位為常數。此版使用第 4.1 節列出的值，正式賣方設定方式待確認。
- 第 5.3 節寫船舶代碼表出錯時轉檔仍成功，第 3 節要求任何一步失敗都回滾。此版把可回復的船舶代碼表寫入失敗單獨回滾，報單或交易本身失敗則全數回滾。
- 第 5.2 節的例子把 `CO.,LTD.` 拆成 `CO`、`LTD`。此版將符號視為詞界，數字移除。其餘有連字號或撇號的名稱，正式拆詞規則待確認。
- 產生器由樣板同油品的公噸與公升比例推算統計量。規格未給正式油品換算公式。
