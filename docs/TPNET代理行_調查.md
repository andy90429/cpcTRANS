# TPNET 單船代理行查詢調查

調查日期 2026-11-09。以匿名公開表單查詢臺中港。未保存網站原始回應。

## 船名篩選實測

| 查詢頁 | `ShipName` | 筆數 |
|---|---|---:|
| 船舶動態 | 空白 | 23 |
| 船舶動態 | `LIANYUNGANG` | 1 |
| 船舶動態 | `TS LIANYUNGANG` | 1 |
| 船舶動態 | `德翔連雲港` | 1 |
| 船舶動態 | 不存在的測試名稱 | 0 |
| 船席指泊 | 空白 | 3 |
| 船席指泊 | `SINGAPORE` | 1 |
| 船席指泊 | `KMTC SINGAPORE` | 1 |
| 船席指泊 | 不存在的測試名稱 | 0 |

兩個頁面的 `ShipName` 都支援包含比對。因此先由網站篩選，必要時以每頁 15 筆翻頁，再以去除船名前綴、空白和大小寫差異後的中英文名稱做完全相等比對。若回傳的船名顯示伺服器忽略了條件，才讀取該頁的未篩選臺中港清單，在本機比對。動態頁沒有符合且未離港的結果時，再用相同方式補查船席指泊。

- [TPNet 2.0 船舶動態](https://tpnet2.twport.com.tw/IFA_PUBLIC/ShipDynamic/Index)
- [TPNet 2.0 船席調派指泊情形](https://tpnet2.twport.com.tw/IFA_PUBLIC/ShipBerthDispatch/Index)

## 請求及欄位

1. GET 船舶動態頁以取得匿名 cookie 與 `__RequestVerificationToken`。若目前港別不是 `TXG`，POST 公開選港表單 `/Account/SwitchPortAnonymous`，再使用切港後頁面回傳的 token。
2. 以 JSON POST 查詢 `GridSearchData`。使用 `ShipName`、`skip`、`take` 和 `requiresCounts`，並驗證回傳的港別、總筆數和欄位。
3. 排除動態頁標示已離港的簽證，以及指泊清單明確標示已出港的資料。同一船舶號數有多筆時，優先比較申請時間，其次用 ETA 或實際進港時間。時間相同或缺少時間時，以簽證號排序。不同船舶號數會回傳候選清單。
4. 確定唯一船舶後，才 POST `GetDashboardComplex`，以選定的簽證號查呼號。代理行全名、碼頭、ETA 取自清單。ETA 保留網站原值，可能是進港預報時間。
5. 公開頁暫時性連線錯誤重試一次。每次請求逾時 20 秒，主動送出請求至少間隔一秒。HTTP 429 直接回報。網路或回應格式錯誤由呼叫端處理，可改請使用者手動輸入。

`lookup_ship` 只在呼叫時上網。沒有整港定期抓取、IMO 補查、備援站或船舶快取。單船測試選擇當天清單的 TS LIANYUNGANG，取得呼號 V7A7385、代理行顗泰企業有限公司。共 **5 次 HTTPS、5.12 秒**，含切港的同站重新導向。
