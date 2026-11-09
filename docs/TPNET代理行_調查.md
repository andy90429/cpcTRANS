# TPNET 代理行查詢調查

調查日期 2026-11-09。使用匿名公開查詢。未登入，未使用帳密，未保存原始網頁。

## 1. 公開查詢入口與傳輸

- [TPNet 2.0 船舶動態](https://tpnet2.twport.com.tw/IFA_PUBLIC/ShipDynamic/Index) 和 [船席指泊](https://tpnet2.twport.com.tw/IFA_PUBLIC/ShipBerthDispatch/Index) 都能取得港代理全名。
- 先 GET 入口取得匿名 cookie 和頁面中的 `__RequestVerificationToken`。公開選港表單向 `/Account/SwitchPortAnonymous` POST `port=TXG`、`returnUrl`、防偽 token。切港後重新使用回應頁的 token。
- 兩個頁面各以 JSON POST 呼叫同頁模組的 `GridSearchData`，帶 `RequestVerificationToken` 標頭。回傳 `result` 和 `count`。這是公開網頁使用的端點，未發現穩定的公開 API 契約。

## 2. 港別與分頁

已驗證 `TXG` 是臺中港，選港後清單資料的 `port` 也是 `TXG`。網頁預設每頁 15 筆，`skip` 為偏移，`take` 為筆數，`requiresCounts=true` 要求回傳總數。船舶動態可能包含已離港事件，船席指泊補充預定靠泊資料。兩者依簽證號整合，剔除動態明確顯示已離港的簽證。這是兩個公開頁當下可查的清單，覆蓋率取決於網站資料。

## 3. 欄位

船舶動態可取船名、港代理全名、簽證號、船舶號數、碼頭、ETA、進出港事件。船名回傳中文和英文時以定位字元分隔。船席指泊有分開的中英文船名、全名、預定與實際碼頭、ETA 和 ETD。ETA 欄保留網站原值，可能是進港預報時間。公開明細 `GetDashboardComplex` 以簽證號和港別查呼號，明細中的代理欄是簡稱。兩個主清單和已查證的明細均未提供 IMO。

## 4. 單船查詢

TPNet 2.0 的船舶動態有呼號、船名和簽證號篩選，未見 IMO 篩選。模組以臺中港清單建立快取，在本機依 IMO、呼號、船名順序精確比對。IMO 用備援頁的船舶號數精確補對。缺資料或號碼互相矛盾時留空。不同代理行的候選一併呈現。

## 5. 備援頁

- [TPNet 船舶動態手機版](https://sdci.twport.com.tw/khbweb/TipcShip.aspx) 是 ASP.NET Web Forms，資料由 DevExpress Grid 顯示。GET 初始頁後 POST `R_港口=TXG`，帶同一 session 與頁面的 `__VIEWSTATE`、`__VIEWSTATEGENERATOR`、`__VIEWSTATEENCRYPTED`、`__EVENTVALIDATION`。
- 驗證可用完整 postback 取得 HTML。帶 `__EVENTTARGET=ASPxGridView1`、`ASPxGridView1` 的 JSON 狀態（`focusedRow`、`keys`、`callbackState`）和長度編碼的 `__EVENTARGUMENT`。下一頁的命令是 `PAGERONCLICK`、`PBN`。搜尋是 `APPLYSEARCHPANELFILTER` 與船舶號數，並帶 `ASPxGridView1$DXSE`。已驗證選港、依船舶號數搜尋與下一頁。每頁 20 筆。
- 原生非同步 callback 使用 `__CALLBACKID=ASPxGridView1` 與 `__CALLBACKPARAM`，回應含 `/*DX*/` 封裝。模組採已驗證的完整 postback，直接用 HTML 解析。
- 備援表是事件歷史，未篩選臺中港樣本顯示 500 頁。只在主站缺資料時，用主站船舶號數查該船的事件，不掃整港歷史。備援資料含船務代理、呼號、IMO 和碼頭，實際靠妥欄與預定靠泊時間含義不同。

## 6. 使用限制與選擇

兩個網站的 `/robots.txt` 本次皆回 HTTP 404。已查入口未見公開查詢的使用條款連結或明示限速，這只代表本次未找到。模組串行查詢，至少間隔一秒，單次逾時 20 秒，暫時性連線失敗重試一次，成功結果預設快取六小時。

選用 TPNet 2.0 為清單、代理全名和呼號的主要來源。SDCI 只補主站未提供的 IMO，或在同一簽證能精確確認時補其他缺值。若主站不可用，使用既有快取並顯示資料時間。兩站表單、回應欄位或分頁結構有變更時需更新解析。

本次模組實抓在臺北時間 19:52 取得 23 筆，其中 23 筆有呼號、19 筆有 IMO。遇到 HTTP 429 會停止該次抓取並回報限流。有舊快取時會沿用並標示資料時間。
