# OSAT MES — 半導體封裝測試廠製造執行系統

以 **Python (FastAPI) + MongoDB** 實作，針對 **OSAT（Outsourced Semiconductor Assembly and Test，封裝測試代工廠）** 的作業特性設計的製造執行系統。

涵蓋從**晶圓進料 → 切割 → 黏晶 → 打線 → 封膠 → 印字 → 植球 → 切單 → 外觀檢 → 最終測試 → 編帶包裝 → 出貨**的完整生產管理，含 WIP 追蹤、生產派工、良率分析、SPC 統計製程管制、設備 OEE、治具壽命、Q-Time 管制與正逆向追溯。

---

## 一分鐘看到成果

不需要安裝 MongoDB，直接跑展示模式（記憶體資料庫 + 自動產生 3 天份的模擬產線資料）：

```bash
pip install -r requirements.txt
python -m scripts.demo --days 3
```

開啟 <http://127.0.0.1:8000>，用 `admin / admin1234` 登入。
介面預設淺色，右上角可切換深色（夜班用），選擇會記在瀏覽器。

API 文件在 <http://127.0.0.1:8000/docs>。

---

## 為什麼 OSAT 的 MES 不一樣

一般離散製造的 MES 直接套到封測廠會出事，這套系統針對以下特性設計：

| OSAT 特性 | 系統的處理方式 |
| --- | --- |
| **同一批貨在不同站的計量單位不同**（片 → 顆 → 條 → 卷） | 站別設定 `unit_transform`，Track-Out 時自動換算「應產出量」，良率以換算後數量為分母 |
| **切割後數量暴增**（1 片晶圓 → 上千顆晶粒） | `WAFER_TO_DIE` 換算取自產品主檔的 `gross_die_per_wafer` |
| **Q-Time 管制**（打線後未在時限內封膠會影響可靠度） | 站別可設 `max_queue_minutes`，進站時檢查等待時間，逾時自動扣留待品保判定 |
| **拆批／併批頻繁**（依機台載盤容量調整） | 拆併批保留完整族譜，晶圓歸屬正確分配給子批 |
| **測試以 Bin 分類**，不是單純良／不良 | 測試站以 `bin_map` 出站，依 `pass_bins` 判定良品，另有 Bin 分佈統計 |
| **客訴要追到晶圓與機台** | 正逆向追溯：批號 ↔ 晶圓 ↔ 設備 ↔ 作業員 ↔ 材料批 ↔ 出貨單 |
| **設備稼動是成本核心** | SEMI E10 六大狀態機 + OEE（稼動率 × 效能 × 良率） |

---

## 功能模組

### 主檔管理
客戶、封裝型式、產品料號、站別、製程流程（含版本控管）、設備、不良代碼、材料、晶圓來料。
主檔一律**軟刪除**，避免破壞既有生產紀錄的追溯性。

### 工單與批號
- 工單狀態機：`DRAFT → RELEASED → IN_PROGRESS → CLOSED`，尚有在製批號時不可取消
- 開批時檢查工單剩餘可投料量、晶圓是否已被其他批號使用、料號是否相符
- 批號狀態：`WAITING / RUNNING / HOLD / COMPLETED / SCRAPPED / SHIPPED / MERGED / SPLIT`

### 生產執行（Track-In / Track-Out）
進站時檢查：批號狀態、是否扣留中、設備能力與狀態、設備是否被其他批佔用、作業員站別資格、Q-Time。
出站時檢查：數量結平（良品 + 不良 = 應產出量）、不良明細與不良數相符、不良代碼適用該站、材料庫存充足。

### 生產派工
待進站批號的建議加工順序，依 **Q-Time 剩餘 → 急單優先序 → 交期 → 等待時間** 排序，
並附上排序理由與可用機台。Q-Time 預警讓逾時在「發生之前」就被看見，而不是進站時才發現。

### 品質管理
扣留／放行（含 Q-Time 逾時自動扣留與品保一次性特採放行）、不良記錄與判定、不良柏拉圖、測試 Bin 分佈、部分報廢、重工退站。

### SPC 統計製程管制
良率告訴你「已經做壞了多少」，SPC 告訴你「製程正在往壞的方向漂」。

* 量測項目主檔：規格上下限、目標值、子群大小、是否自動扣留
* 量測資料收集：即時判定超規與管制圖異常
* X-bar / R 管制圖：以**基準期建立並凍結**管制界限
* 判異規則（Nelson rules 子集）：單點超出 3σ、連續 9 點同側、連續 6 點趨勢、三點中兩點超 2σ
* 製程能力：Cp / Cpk / Ca 與業界分級

### 治具壽命管理
毛細管、劈刀、切割刀這類以「累計加工顆數」計壽命的耗材：上下機、出站自動累計、
接近壽命預警、到期自動把設備轉為計畫停機待換刀、換刀後自動復機。

### 稽核與交接班
所有異動類 API 呼叫與主檔欄位異動都留下稽核軌跡（敏感欄位遮蔽）。
交接班報表把本班產出、新增扣留、設備停機、SPC 異常、治具待換與下一班待辦整理成一頁。

### 設備管理
SEMI E10 狀態機與狀態履歷、稼動率／OEE 計算、PM 保養工單（完成後自動排下一次）。

### 報表與戰情看板
各站 WIP、在製停留時間分佈、各站良率與累計良率、各料號良率、班別產出、週期時間與瓶頸站、Q-Time 逾時清單。

### 追溯
```
晶圓 → 批號（含拆批／併批族譜）→ 加工履歷（設備／人員／時間）→ 材料批 → 出貨單 → 客戶
```
- **逆向追溯**：客訴進來時，查這批貨用了哪些晶圓、上了哪些機台、誰做的、用了哪批材料
- **正向追溯**：晶圓廠通知某片 wafer 異常時，圈出所有受影響的批號與已出貨客戶
- **材料流向**：供應商材料批異常時的圈選範圍

---

## 系統架構

```
app/
├── main.py                  FastAPI 應用、例外處理、靜態頁面掛載
├── config.py                設定（MES_ 開頭環境變數）
├── database.py              MongoDB 連線、Collection 常數、索引定義
├── security.py              PBKDF2 密碼雜湊 + HS256 JWT + RBAC（純標準函式庫）
├── errors.py                商業邏輯例外 → HTTP 狀態碼
├── models/                  Pydantic 模型與 OSAT 領域列舉
├── services/                商業邏輯（進出站、拆併批、OEE、SPC、派工、治具、追溯、報表…）
├── routers/                 HTTP API
└── static/                  戰情看板前端（原生 JS，無需建置）

scripts/
├── seed.py                  建立示範主檔（客戶／料號／流程／設備／不良碼／量測項目／治具／晶圓）
├── simulate.py              離散時間模擬產線跑批，含 SPC 量測、製程漂移與換刀
└── demo.py                  一鍵展示：記憶體資料庫 + 主檔 + 模擬 + 網頁

tests/                       148 個測試，涵蓋規則、狀態機、SPC 數學、報表與 API
```

### 分層原則
- **routers** 只做參數驗證與權限檢查，不含商業邏輯
- **services** 是唯一的商業規則所在，可直接被腳本與測試呼叫，不依賴 HTTP
- **models** 定義輸入驗證；**服務層仍會重新驗證關鍵不變條件**（例如投入晶圓數與批量相符），因為服務層也會被腳本直接呼叫

---

## 安裝與執行

### 方式一：展示模式（免安裝 MongoDB）

```bash
pip install -r requirements.txt
python -m scripts.demo --days 3
```

資料存在記憶體，程式結束即消失，適合快速看功能。

### 方式二：搭配真實 MongoDB

```bash
cp .env.example .env          # 依環境調整，務必更換 MES_JWT_SECRET
pip install -r requirements.txt

python -m scripts.seed        # 建立主檔
python -m scripts.simulate --days 3   # （選用）產生模擬生產資料
uvicorn app.main:app --reload
```

### 方式三：Docker Compose

```bash
docker compose up -d --build
docker compose exec api python -m scripts.seed
```

---

## 設定項目

全部可用 `MES_` 開頭的環境變數覆寫（見 `.env.example`）：

| 變數 | 預設 | 說明 |
| --- | --- | --- |
| `MES_DB_BACKEND` | `mongo` | `mongo`（正式）或 `memory`（記憶體展示） |
| `MES_MONGODB_URL` | `mongodb://localhost:27017` | MongoDB 連線字串 |
| `MES_MONGODB_DB` | `osat_mes` | 資料庫名稱 |
| `MES_JWT_SECRET` | 開發用預設值 | **正式環境務必更換** |
| `MES_JWT_EXPIRE_MINUTES` | `480` | Token 有效期（一個班別） |
| `MES_TZ_OFFSET_HOURS` | `8` | 廠區時區，影響班別與日報切分 |
| `MES_SHIFT_START_HOURS` | `[8,20]` | 班別起始時間 |
| `MES_AUTO_HOLD_ON_QTIME_VIOLATION` | `true` | Q-Time 逾時是否自動扣留 |

---

## 角色權限

| 角色 | 可做的事 |
| --- | --- |
| `admin` | 全部（角色檢查一律放行）、使用者管理 |
| `planner` 生管 | 工單、開批、拆批、併批、出貨 |
| `operator` 作業員 | 進站、出站、完成 PM、治具上下機、SPC 量測 |
| `qc` 品保 | 扣留、放行、報廢、重工、不良判定、SPC 量測、稽核調閱 |
| `engineer` 工程師 | 主檔維護、設備狀態、PM 排程、SPC 項目、治具管理；視為全站別合格 |
| `viewer` | 唯讀報表 |

作業員另需具備該站別的**資格認證**（`certifications`）才能在要求認證的站別進站；`admin` 與 `engineer` 視為全站合格。

示範帳號密碼為「帳號 + 1234」（例如 `op001 / op0011234`），管理者為 `admin / admin1234`。

---

## 主要 API

| 方法 | 路徑 | 說明 |
| --- | --- | --- |
| `POST` | `/api/auth/login` | 登入取得 JWT |
| `POST` | `/api/work-orders` · `/{wo}/release` | 建立與下達工單 |
| `POST` | `/api/lots` | 自工單開批 |
| `POST` | `/api/lots/track-in` · `/track-out` | 進站／出站 |
| `POST` | `/api/lots/hold` · `/release` | 扣留／放行 |
| `POST` | `/api/lots/split` · `/merge` | 拆批／併批 |
| `POST` | `/api/lots/scrap` · `/rework` · `/ship` | 報廢／重工／出貨 |
| `POST` | `/api/equipments/{eq}/state` | 變更設備 E10 狀態 |
| `GET` | `/api/equipments/oee` | 全廠 OEE |
| `GET` | `/api/reports/dashboard` | 戰情看板（單一 API 餵滿整面牆） |
| `GET` | `/api/reports/yield/by-operation` | 各站良率 |
| `GET` | `/api/reports/yield/by-route` | 各流程累計良率 |
| `GET` | `/api/quality/defects/pareto` | 不良柏拉圖 |
| `GET` | `/api/trace/lots/{lot}/backward` | 逆向追溯 |
| `GET` | `/api/trace/wafers/{wafer}/forward` | 正向追溯 |
| `GET` | `/api/dispatch` | 派工清單（下一批做哪個） |
| `GET` | `/api/dispatch/qtime-watch` | Q-Time 預警 |
| `POST` | `/api/spc/measurements` | 記錄量測（即時判異） |
| `GET` | `/api/spc/items/{item}/chart` | X-bar / R 管制圖 |
| `GET` | `/api/spc/items/{item}/capability` | 製程能力 Cp / Cpk |
| `POST` | `/api/tools/mount` · `/replace` | 治具上機／換刀 |
| `GET` | `/api/tools/attention` | 待更換治具 |
| `GET` | `/api/reports/shift-handover` | 交接班報表 |
| `GET` | `/api/audit` | 稽核軌跡（管理者／品保） |

完整規格見 `/docs`（Swagger UI）。

---

## 幾個值得一提的設計決策

**單位換算放在站別而非硬編碼。**
`WFR_SAW` 設 `unit_transform=WAFER_TO_DIE`，換算比例取自產品主檔。換料號、換封裝都不必改程式。

**良率的分母是「應產出量」而不是「進站量」。**
切割站進站 5 片、應產出 5,000 顆，良率分母是 5,000。用進站量當分母在切割站會算出 100,000% 的荒謬數字。

**Q-Time 逾時放行後給予一次性特採。**
逾時自動扣留後，品保放行時等待時間仍然超標——若不處理會立刻再次扣留，形成死循環。因此放行時對**當前站別**記錄一次性特採（`qtime_waived_seq`），用掉即失效。

**累計良率只在單一流程下計算。**
導線架流程與基板流程的站別代碼會重疊（兩者都有 `FT`），把混流資料串成一條累計良率在數學上沒有意義。因此混流時 `cumulative_yield` 回傳 `null`，改由 `/api/reports/yield/by-route` 分流程呈現。

**拆批時晶圓歸屬正確分配。**
在晶圓階段拆批會把晶圓實際分給各子批；已切割成顆而無法分割時才整份複製。追溯時以批號自身的紀錄為準，避免客訴圈選範圍被不當放大。

**管制界限以基準期凍結，不隨新資料浮動。**
若用「最近 N 點」即時算界限再拿最新點去比，界限會跟著製程漂移一起移動，
永遠測不到漂移。系統改採正規做法：累積足夠子群後建立一次界限並固定，
之後所有點都跟這組固定界限比對；工程師也可指定期間重新建立基準。

**扣留擋的是下一次進站，不是這一次出站。**
SPC 在加工途中判異會扣留批號，但料還在機台上——若連出站都擋住，機台會被卡死。
因此加工中被扣留的批號仍可 Track-Out，扣留狀態帶到下一站，放行後回到待進站。

**治具到期直接把機台停下來。**
超壽命的毛細管會讓不良率悄悄爬升卻查不出原因。出站時自動累計加工顆數，
到期即把設備轉為計畫停機，換刀後才能復機——靠制度而不是靠作業員記得。

**認證不依賴原生擴充套件。**
密碼用 `hashlib.pbkdf2_hmac`、JWT 用 `hmac` + `base64` 自行實作，不需要 `bcrypt` / `cryptography` 等需要編譯的相依，部署到受限環境不會卡住。

**時間來源可注入。**
`app.models.base.set_clock()` 讓模擬器與測試可以推進虛擬時間，才能測出 Q-Time、週期時間與 OEE 這類與時間強相關的邏輯。

---

## 測試

```bash
python -m pytest
```

148 個測試，涵蓋：
- 密碼／JWT／RBAC 與越權存取
- 主檔參照完整性與流程版本管理
- 單位換算、數量結平、不良明細一致性
- 設備能力／狀態／佔用與人員資格管制
- Q-Time 逾時扣留與特採放行（含防死循環）
- 拆批、併批、族譜與正逆向追溯
- SEMI E10 時間分類與 OEE 數學
- WIP、良率、柏拉圖、Bin 分佈、週期時間、看板彙總
- SPC 判異規則、管制界限數學、Cp/Cpk（含單邊規格）
- 治具壽命累計、到期停機、換刀復機
- 派工排序（Q-Time 插隊優先於急單）與 Q-Time 預警
- 稽核遮蔽、班別區間換算與交接班報表
- HTTP 層完整動線與錯誤回應格式

測試使用記憶體資料庫（mongomock），不需要啟動 MongoDB。

---

## 已知限制

- MongoDB 單機部署時本系統未使用多文件交易（standalone 不支援）。進出站的多筆寫入之間若發生程序中斷，可能留下不一致狀態。正式環境建議部署 **Replica Set** 並在 `lot_service` 的關鍵區段加上 session transaction。
- 記憶體後端（`memory`）僅供展示與測試，不保留資料，也不支援 `$sortByCount` 等少數聚合運算子。
- `scrap_qty` 累計的是「不良發生當下的單位數量」；批號跨越單位換算站時，此欄位是混合單位的累計值，精確的分站不良數請看 `lot_history` 或不良報表。
- SPC 管制界限目前在累積足夠子群後自動建立一次；正式使用時應由製程工程師確認基準期是否穩定，必要時以 `establish_limits` 重新建立。
- 尚未實作：晶圓 Map（Die 級座標追溯）、與 ERP／設備 SECS/GEM 的整合介面、e-SOP 作業指導書。
