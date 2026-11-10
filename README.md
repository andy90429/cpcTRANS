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

`convert_file` 和 `convert_files` 也接受 `connection=`，可傳入與 `settings.driver` 相符、沒有進行中交易的資料庫連線。轉檔仍以每張報單為單位提交或回滾，外部連線由呼叫端關閉。舊版 MySQL 的登入及驗證可由呼叫端處理。

## 打包

在 Windows 安裝 **64 位元 Python 3.11 以上**，從專案根目錄依序執行：

```bat
build_exe.bat
build_check.bat
```

`build_exe.bat` 在專案內建立或沿用 `.venv_build`，安裝打包套件，產生 `dist\fuelcore.zip`。ZIP 包含 `fuelcore.exe`、`fuelcore_cli.exe` 和共用的 `_internal` 目錄。如果 Python 不在 PATH，可以先將 `FUELCORE_BUILD_PYTHON` 設為 Python 執行檔的完整路徑。建置用的 Python 須包含 Tkinter。

`build_check.bat` 將 ZIP 解到新的暫存資料夾，從範例建立設定，用 EXE 建立 SQLite 測試庫並轉換 `資料\txt樣本` 內的所有 TXT，最後列出成功及失敗數。樣本在專案外時，可以執行 `build_check.bat "C:\路徑\txt樣本"`。

## 視窗操作

完整解壓縮 ZIP 後，雙擊 `fuelcore.exe` 開啟視窗。測試機無須安裝 Python。第一次可在「設定」選擇 TXT 來源目錄與 CBSAPP 目錄，再按「儲存設定」。程式將設定寫入 EXE 同層的 `fuelcore.ini`。INI 的相對路徑以 INI 所在目錄為起點。

```ini
[paths]
txt_path = C:\NCBS\TxTrans中油\TXT
cbsapp_path = C:\NCBS\CBSAPP
```

儲存後會自動重新掃描。按「重新整理」會重新讀取 `fuelcore.ini` 的 `txt_path`。預設會掃描最近 60 個日曆日（含今天）的 `YYYYMMDD` 日期資料夾，勾選「顯示全部」可掃描更早的資料夾。`.txt` 與 `.TXT` 都會列出。清單預選未轉入的檔案，無法讀取的檔案會個別標記並將原因寫入作業紀錄。

雙擊「未轉入」的檔案可編輯 TXT。油品表可修改密度，主檔與項次可雙擊欄位修改。換算使用既有的 ASTM Table 13。若從 TXT 回推不出密度，修改噸數時會保留原 LTR 並顯示提醒，補填密度後可重算。檢核訊息供參考，仍可儲存。

有修改才會覆寫原 TXT，保留 UTF-8 BOM、換行及原欄位的小數位數。原檔先備份到 TXT 來源目錄的 `原檔備份`。未設定來源目錄時，使用該 TXT 所在的資料夾。備份資料夾會從轉檔清單排除。若記事本等其他程式已更改原檔，編輯視窗會拒絕覆寫。

「設定 → 船舶資料」可新增、修改或刪除船舶，變更會立即儲存到 INI 旁的 `fuelcore_ships.json`。TXT 編輯視窗可從清單選船以回填主檔，也可把目前的船舶資料加入清單。只有按「查詢代理行」才會上網。

轉檔遇到空白呼號時，依序查本機清單的 IMO、船名，再查測試代碼表的 IMO、船名。作業紀錄會列出填入依據。若清單無法讀取，轉檔會提醒並繼續查代碼表。

可用 Ctrl 或 Shift 調整選取，或按「加入其他檔案…」。轉檔完成後可在作業紀錄查看每筆結果，選取紀錄可閱讀完整文字。

未另存設定時使用附帶的 SQLite 測試設定，首次轉檔會建庫。CBSAPP 目錄用來確認 `CBSAPP.exe` 的位置。要在 CBSAPP 裡查到結果，兩邊需連到同一套 MySQL 測試庫。

## 封包命令列

在解壓後的 `fuelcore` 資料夾執行。`fuelcore.exe` 帶參數時也會轉交相同的命令。若要讓命令提示字元等待完成或讓腳本取得退出碼，請直接用 `fuelcore_cli.exe`。

```bat
copy fuelcore.ini.example fuelcore.ini
fuelcore_cli.exe schema --config fuelcore.ini
fuelcore_cli.exe convert "C:\路徑\D4122_TEST_0001.txt" --config fuelcore.ini
```

## MySQL 測試庫

把範例設定改為 `driver = mysql`，填入測試主機、帳密和兩個測試資料庫名稱。名稱須以 `test_` 開頭，或以 `_test`／`_test_code` 結尾。請使用只有測試庫權限的帳號。建表工具讀取附帶的外部版 CSV，也接受 `--csv` 指定來源。MySQL 建表工具只輸出 SQL。請在測試伺服器套用 SQL 後再執行轉檔。

```powershell
python -m pip install ".[mysql]"
python -m fuelcore schema --config fuelcore.ini --output fuelcore_test.sql
```

MySQL 報單表與代碼表使用同一個連線和交易。船舶代碼維護若發生可回復錯誤，回滾該段並留下說明。SQLite 把有小數位的欄位存成定點文字，以保留尾端的零供測試核對。

## 每旬報關匯率

```powershell
python -m fuelcore rate-simulate --config fuelcore.ini --output .\匯率報告
python -m fuelcore rate-import --config fuelcore.ini
python -m fuelcore rate-simulate --config fuelcore.ini --output .\匯率報告 --from-files .\current.json .\history.txt
```

執行匯率指令會向關務署 `portal.sw.nat.gov.tw` 取得本旬與歷史匯率。使用 `--from-files` 改讀本旬 JSON，可另附歷史 TXT，全程離線。先執行 `schema` 準備代碼測試庫的 `mydeclarerate`。

模擬只查詢資料庫。匯入會顯示變更，輸入 `y` 或指定 `--yes` 才寫入測試庫。歷史檔只用於核對最近九個已完成旬別。轉檔後，若測試庫已有同一期旬的買進匯率，程式會在報單匯率不符時加一行提醒。

## Windows PFX 憑證檢查

```powershell
python -m fuelcore cert-check .\signing.pfx
python -m fuelcore cert-check .\signing.pfx --recorded-expiry 2027-11-30
```

密碼由互動式終端機遮蔽輸入。檢查全程離線，PFX 在記憶體內開啟，密碼不會儲存。結果顯示檔名、選取的憑證主體、到期日與剩餘天數。已過期或無法開啟時退出碼為 1，正常與提醒為 0。

## CBS 邏輯檢查（F6）

將本機外掛路徑填入 `fuelcore.ini` 的 `[logic_check] plugin_dir`，或設定 `FUELCORE_LOGIC_PLUGIN` 覆蓋。外掛的進入點為 `run_check.py`。直接用 Python 執行 fuelcore 時可將 `python_exe` 留空。封裝版請填本機 Python 執行檔路徑。逾時預設為 120 秒。

```powershell
python -m fuelcore check TEST00A10E0777 --config fuelcore.ini
python -m fuelcore check TEST00A10E0777 --config fuelcore.ini --save
python -m fuelcore check TEST00A10E0777 --config fuelcore.ini --output 檢查結果.json
```

查詢使用測試庫。MySQL 的報單、代碼與 `[logic_check] rules_database` 請指定各自的測試庫，並使用權限限於這些測試庫的帳號。寫入需要 `test_only = yes`、通過測試庫名稱檢查和明確指定 `--save`。SQLite 測試把報單、代碼和規則表放在同一個檔案。

工作台預設在一批轉檔完成後，對這批成功的報單自動執行 F6 並由外掛寫回測試庫。「檢查結果」視窗逐張顯示船名、呼號、買方、項次、規則及錯誤訊息，也可重新檢查。檔案清單分開顯示轉入和檢查狀態。若只需要轉檔，在設定取消「轉完自動檢查（跟 CBS 按 F6 一樣）」並儲存，或設 `[workflow] check_after_convert = no`。批次完成時會顯示未檢查及各張呼號。

手動選取已轉入的報單按「邏輯檢查」時，預設為唯讀。勾選「寫回結果」才會寫測試庫。寫回前會先查 `CUSDEC.STATUS`，只允許 `Y`、`0` 或空白，已送出或已有回覆的報單不會交給外掛寫回。送信模擬仍依報單現有的 `STATUS=Y` 判斷。

測試分三層：

| 設定 | 會執行的測試 |
| --- | --- |
| 都不設 | 只測 fuelcore 的接口，使用 `tests/fixtures/check_stub` 的假外掛 |
| `FUELCORE_LOGIC_PLUGIN` | 另外用本機外掛檢查轉檔核心產生的 TEST 報單。規則是外掛資料夾 `test_rules` 裡自編的幾條，用來確認外掛能執行、能寫回，不代表 CBS 的實際規則。缺少這個資料夾時測試會跳過 |
| 再加 `FUELCORE_CBS_CONFIG` | 另外匯入該資料夾的 `cbssetcontent.*.csv` 與 `code4.*.csv`，用 CBS 的規則檢查。CBS 的規則不接受 `TEST` 開頭的報單號碼與虛構的箱號、許可證號，所以這個測試會用外掛資料夾 `test_rules` 裡的 SQL 從本機代碼表取值，組出格式合理的測試號碼（流水號以 `T` 開頭），並補上規則會查詢而轉檔核心不寫入的空表 |

要確認外掛能在封裝用的直譯器執行，可再將 `FUELCORE_LOGIC_PYTHON` 設為該 Python 執行檔（例如 Python 3.7 32 位元）。

SQLite 測試庫的小數與日期欄位宣告為 `TEXT DECIMAL(…)`、`TEXT DATETIME` 等型別，值仍以原樣文字儲存。舊版建立的 SQLite 測試庫沒有這些宣告，數值規則會無法檢查，請刪除後用 `schema` 命令重建。

外掛結構和需要的 CBS 規則資料見 [邏輯檢查調查](docs/邏輯檢查_調查.md)。

## 送信模擬

工作台的「送出前確認」可輸入多個報單號碼，以空白或逗號分隔。按「檢查」後，紅字表示不能送，橘字表示有提醒，選取報單即可查看原因。選取可模擬的報單，按「模擬送信」並逐張確認提醒。模擬只在選定資料夾輸出 XML 和結果報告，資料庫全程唯讀。

```powershell
python -m fuelcore send-check TEST00A10E0777 TEST00A10E0781 --config fuelcore.ini
```

CLI 檢查發現不能送的報單時回傳退出碼 `1`，只有提醒時回傳 `0`。沒有 EDI 測試表或船舶代碼表時，略過對應的歷史或船名比對。

在自己的 `fuelcore.ini` 新增 `[edi]`。MySQL 會透過同一組連線查詢報單、EDI 與 XML 設定測試庫。`company_code` 是檔名使用的 8 碼公司代碼，`user` 用於擬新增的送信紀錄。SQLite 測試可將測試資料表放在同一個檔案。

```ini
[edi]
edi_database = test_edi
xmlset_database = test_xmlset
company_code = 00000000
user = USER1
```

```powershell
python -m fuelcore simulate-send TEST00A10E0777 TEST00A10E0781 --config fuelcore.ini --output .\模擬輸出
```

命令在指定目錄下建立一個新的批次資料夾。`STATUS=Y` 且通過送前檢查的報單會產生 N5203 XML。`模擬結果.txt` 記錄預計使用的日序號、封包號碼、SendMsgRcds 欄位及 CUSDEC 變更。有任何報單被略過時退出碼為 1。資料庫操作限於 SELECT，輸出檔留在模擬資料夾。

如需用本機 CSV 驗證，可將 `FUELCORE_CBS_CONFIG` 指向 repo 外的 `只放本機_CBS設定` 資料夾。此設定會覆蓋 XML 設定庫來源，報單與序號仍由測試資料庫讀取。未設定時，依賴這批 CSV 的測試會跳過。

## 收信模擬與匯入

兩個命令都接受一個以上的回覆檔或資料夾，依檔名處理。預設讀取目前目錄的 `fuelcore.ini`。只處理 N5107 和 N5204，並逐檔列出結果。遇到失敗時退出碼為 1。

```powershell
python -m fuelcore simulate-receive .\回覆 --config fuelcore.ini --output .\模擬輸出
python -m fuelcore import-replies .\回覆 --config fuelcore.ini
```

先用 `simulate-receive` 查看 `模擬結果.txt` 裡的回覆欄位、日序號及 CUSDEC 變更。模擬只執行 SELECT。`import-replies` 每個檔案各用一個交易，成功後才更新 Input 日序號。依訊息類型及檔名略過已匯入的回覆，成功和失敗的檔案都留在原位。

MySQL 匯入需先準備測試庫的回覆表、`RcvMsgRcds` 及只有一筆資料的 `sequence` 表。參與寫入的表須為 InnoDB。N5204 需要唯一一筆對應的 CUSDEC 報單，才能一起提交放行狀態與回覆紀錄。

送信的兩份標準 XML 已在假報單 CSV 的 `SLNAME`、`SLADDR`、`SLCNAME`、`SLCADDR` 匿名化後重新產生。產生時用 `tests/test_edi.py` 的 `load_fixture_tables` 將假資料載入記憶體 SQLite，再用 `XmlSettings.load(Database(...))` 讀取本機 CBS 設定，最後將 `SendXmlBuilder.xml_bytes` 的結果直接寫入 `tests/fixtures/edi/expected/send/`。

## 海關回覆工作台

「海關回覆」以唯讀方式顯示 EDI 測試庫當天收到的回覆。按「重新整理」可更新清單，雙擊或在右鍵選單確認後可補印報單。補印需要 `[print] printer`、支援雙面的印表機及本機 CBS 報表資源。

在 `fuelcore.ini` 的 `[notify]` 設定 `enabled = yes` 後啟動工作台，即會每隔 `poll_seconds` 秒監看 N5107 與 N5204。初次執行會建立設定檔旁的 `fuelcore.notify-state.json`，記住既有回覆的位置。C3 和 C1A18 的通知可分開開關。C2 只寫監看紀錄。

```ini
[notify]
enabled = yes
c3 = yes
c1a18 = yes
poll_seconds = 60
c3_pdfs =
    附件\{SERSNO}-{BAID}-*.pdf
c1a18_pdfs =
print_command =
```

附件相對路徑以設定檔所在資料夾為準，也接受絕對路徑。可用 `{SERSNO}`、`{BANAME}`、`{BAID}`、`{BYNAME}` 和萬用字元 `*`。通知按下「列印」後才會送出報單及找到的 PDF。附件預設交給 Windows 的 PDF 列印關聯。若指定 `print_command`，請在參數中放入 `{file}`，例如 `"C:\example\PdfViewer.exe" /print "{file}"`。

N5109、NX5106、NX903、N5110 目前依規格欄名讀取，缺少的表或欄位會列入作業紀錄。

## 工作台設定分頁

「設定」分成資料庫與路徑、轉檔、其他申報事項、油品表、買方國家、列印、海關回覆和船舶資料。船舶資料的變更立即儲存，其餘分頁各自儲存。邏輯檢查外掛在「轉檔」，EDI 測試庫在「海關回覆」。歸檔開關會先儲存，檔案搬移由 P2-3 接續實作。

「其他申報事項」可選擇寫入 `OTHER`、`EDIOTHER`，停用時兩欄寫入空白。片語「帶入」會查詢測試代碼庫的 `mynoteabbr`，已有文字時會先確認取代。儲存會列出無法編成 cp950 的字元供確認。由工作台編輯的文字存於 `[other] text_json`，既有 `[conversion] other` 仍可讀取。

如需限制進入設定頁，可在本機 `fuelcore.ini` 的 `[app] admin_password` 設定密碼。留空直接開啟，輸入正確後本次執行不再詢問。此密碼存於本機 INI。

## 報單 Rpx 與 PDF 預覽

從專案根目錄安裝列印所需套件。MySQL 環境可一併安裝 `.[mysql,report]`。

```powershell
python -m pip install ".[report]"
$env:FUELCORE_CBS_REPORTS = 'C:\fuelcore_local\搬移包_列印\只放本機_CBS報表'
python -m fuelcore render-report TEST00A10E0777 TEST00A10E0781 --config fuelcore.ini --output .\列印輸出
```

報表目錄須提供 `N5203.Rpt`、`N5203CUS.Rpt` 和範本的 `PictureFile` 指定的 BMP。程式依既有 INI 以 SELECT 讀取測試庫。報單未填通關方式時，會在可用的 EDI 測試庫查找 N5107／N5204 的通關方式，不必等放行日期。輸出目錄每次建立一個批次資料夾，含每張報單一個 TPF0 正面的 Rpx、每張正面接背面查驗表的 PDF，以及逐頁 PNG。

600 dpi 一位元底圖預設快取於 `%LOCALAPPDATA%\fuelcore\report-backgrounds`，可用 `FUELCORE_REPORT_CACHE` 指定其他本機位置。背面延用舊版的 180 度方向。若缺少指定字型，程式使用可用字型並列出提醒。過長明細超出單張正面可用範圍時，程式會提示該報單。`參考輸出` 內如有同報單的 Rpx，批次資料夾會另外產生 `比對差異.txt`。

`tests/test_report.py` 會在測試期間產生假範本。設定 `FUELCORE_CBS_REPORTS` 後，也會驗證本機範本及用兩張假報單產生 PDF。

### Windows GDI 直接列印

先用 `list-printers` 查詢 Windows 上的名稱，再填入本機 `fuelcore.ini`。

```ini
[print]
printer = FX DocuCentre-V 3065 PCL 6
```

```powershell
python -m fuelcore list-printers
python -m fuelcore print-report TEST00A10E0777 TEST00A10E0781 --config fuelcore.ini
```

`print-report` 列出報單、頁數、印表機及雙面模式。輸入 `y` 才送出，`--yes` 可略過確認。多張報單走同一個 GDI 列印工作，依序正面接背面。支援雙面時設定長邊翻頁。驅動程式未回報雙面能力時顯示單面提醒。設定空白或名稱不符時列出可用印表機並停止。

`render-report` 仍可產生 PDF 預覽。兩個命令共用文字排版、條碼、虛線及一位元底圖。預覽輸出保留七天，清理只處理有 fuelcore 所有權標記、且內容只有預期輸出檔的批次。既有未標記資料夾保留。

### 工作台的報單列印

開啟「報單列印」，輸入報單號碼或查詢下拉清單後按「從資料庫產生」。可用空格或逗號一次輸入多張報單，也可以開啟既有 Rpx。用預覽下拉清單切換各張報單的正反面。

「試算」可修改各項數量，並選填密度改用 ASTM Table 13。單價與匯率沿用原報單。「試算」和「修改欄位」的「套用」會更新預覽，選「另存 Rpx」才會寫入副本。切換報單、重新載入或關閉程式前，會提示處理未存檔的修改。列印、PDF 和更正申請書使用目前畫面中的內容。Rpx、PDF、Excel 預設存到設定檔旁的 `列印輸出`，檔名含時間。資料庫查詢只用 SELECT。

第三階段的試算、Table 13 和更正申請書以 10-21 版 `rpx_editor.html` 為依據。更正申請書以 B 欄顯示原報單數字、C 欄顯示修改數字。有更正時隱藏未變動的數字列，完全沒有更正時顯示全部。Excel 公式使用前後各自的匯率與單價，版面為 A4 直式一頁、水平及垂直置中。執行時無須提供 HTML 或另外準備 Excel 範本。報單直接列印沿用本機設定的印表機，先顯示報單、頁數和單／雙面模式供確認。

## 驗證與產生測試檔

```powershell
$env:FUELCORE_SAMPLES = 'C:\路徑\轉檔核心規格包\資料\txt樣本'
python -m unittest discover -s tests -v
python -m fuelcore.generate $env:FUELCORE_SAMPLES .\generated --seed 55
```

## 臺中港船務代理查詢

```powershell
python -m fuelcore agent --name "TEST SHIP 1"
python -m fuelcore agent --name "TEST SHIP 1" --mapping .fuelcore-agent/mapping.csv --shorts .fuelcore-agent/shorts
```

指令用中英文船名查單艘船，顯示狀態、呼號、代理行全名、碼頭及 ETA。提供 `--mapping` 或 `--shorts` 才會顯示簡稱或候選。對照 CSV 可讀 UTF-8 或 CP950，寫回使用 UTF-8 BOM。`--shorts` 接受每行一個簡稱的文字檔或印章圖片資料夾。請將對照表和印章留在已排除的 `.fuelcore-agent/`，或由呼叫端指定 repo 外的路徑。

獨立模組 `fuelcore/agent_lookup.py` 僅使用 Python 3.7 標準函式庫。移植到 CPCtrans 時，可在 TXT 缺呼號時呼叫 `lookup_ship(ship_name)`。只有呼叫時會上網。網站調查與欄位限制見 `docs/TPNET代理行_調查.md`。

## 待確認與資料差異

- Q1–Q5 的 TODO 集中於 `fuelcore/convert.py`。主檔稅則讀 I2，項次讀設定，付款方式固定 08。`PKGTOTAL` 暫採四捨五入至整數，`EXPQUAN` 保留項次公噸加總的小數，`FOBPRICE` 保留 H26 的兩位小數。未列欄位交由資料庫給預設值，報單品名換行讀油品表。
- CSV 的 FOREX 與 B52_項次稅費 都是 5 位小數。依第 4 節先分別取 3 位與 4 位，再補零到欄位精度。
- CSV 只有九張報單表的欄位，缺少正式代碼表結構和欄位預設值。工具以第 5 節列出的欄位建立測試代碼表。正式庫欄位預設值需要內部確認。
- 第 0 節說正式代號及公司名由設定檔提供，第 4.1 節則列出賣方欄位為常數。此版使用第 4.1 節列出的值，正式賣方設定方式待確認。
- 第 5.3 節寫船舶代碼表出錯時轉檔仍成功，第 3 節要求任何一步失敗都回滾。此版把可回復的船舶代碼表寫入失敗單獨回滾，報單或交易本身失敗則全數回滾。
- 第 5.2 節的例子把 `CO.,LTD.` 拆成 `CO`、`LTD`。此版將符號視為詞界，數字移除。其餘有連字號或撇號的名稱，正式拆詞規則待確認。
- 產生器由樣板同油品的公噸與公升比例推算統計量。規格未給正式油品換算公式。
