# 交易副駕・慢速層（copilot-daily）

借用「市場雷達」的骨架：**GitHub Actions 定時跑 → 結果存成 JSON → 靜態網頁顯示 → Claude 只負責寫總結**。
不用租主機、不用 pip install（全部只用 Python 內建模組），只讀行情、**不會下單**。

它不是要取代你 24 小時的即時雷達，而是在旁邊加一層「慢速層」：

| | 即時雷達（快速層） | 這套（慢速層） |
|---|---|---|
| 看什麼 | 盤中異常波動 | 日線方向＋4H 收盤進場 |
| 多久判斷一次 | 隨時 | 每 4 小時一次，只看已收盤 K 線 |
| 強項 | 抓得快 | 方向穩、每筆記帳、知道規則到底賺不賺 |

---

## 對應你實際用下來回報的問題

| 問題 | 這套怎麼處理 |
|---|---|
| 策略過 5 分鐘就改變方向 | 方向只看**日線收盤**，而且要**連續 2 天**判斷一樣才換。一天最多變一次，盤中怎麼跳都不影響。網頁上「醞釀轉多／轉空」只是提醒，還沒生效 |
| 會重複送單 | 同一個幣同時只有一筆；同一根 K 線不會發第二次；結束後冷卻 12 小時（3 根 4H）|
| 勝率很低、說不清楚 | 每筆訊號自動記帳，用之後真的 K 線結算（止盈／停損／到期），扣手續費，算出勝率、平均 R、最大回撤 |
| 不知道 AI 判斷對不對 | 數字全部程式算，Claude 只把算好的數字寫成文字，**不准自己生價格**。再拿「實盤成績」對「回測成績」，差太多就代表規則有問題 |
| 止盈掛太遠、停損被插針掃 | 止盈寫死在 1.5R（先平一半、停損移成本）和 2.5R；回踩單停損放在近 10 根低點再外推 0.5 倍 ATR |

---

## 已經驗證過的事

- 用測試資料把「每 4 小時排程」逐根重播 60 天，結果跟回測**逐筆完全一致**（31 筆，結果與 R 值零差異）→ 回測測到的就是實際在跑的規則，沒有兩套標準
- 回測每一根只用「當時已收盤」的資料，不偷看未來
- 網頁在 iPhone SE 寬度（375px）檢查過，沒有左右滑動

**還沒驗證的：用真實行情的成績。** 這要等你部署後跑回測才知道，規則可能根本不賺錢——這套東西的價值就是讓你**在押錢之前**知道。

---

## 安裝（約 15 分鐘）

### 1. 建 repo、上傳檔案
GitHub 右上角 **+ → New repository** → 名稱例如 `copilot-daily` → Private 或 Public 都可以（差別見第 4 步）→ Create。
進到 repo → **Add file → Upload files** → 把這個資料夾裡的東西全部拖進去（包含 `.github` 資料夾）→ Commit。

> `.github` 是隱藏資料夾，Windows 檔案總管要開「顯示隱藏的項目」才看得到。

### 2. 設定金鑰（全部選填）
repo → **Settings → Secrets and variables → Actions → New repository secret**：

| 名稱 | 用途 | 沒設會怎樣 |
|---|---|---|
| `ANTHROPIC_API_KEY` | Claude 寫總結 | 改用規則組句 |
| `TELEGRAM_BOT_TOKEN`、`TELEGRAM_CHAT_ID` | 新訊號推播到 Telegram（可以用你現有的 Bot） | 不推播 |

另外在同一頁的 **Variables** 分頁可以加 `SITE_URL`（網站網址），推播最後會附上成績單連結。

### 3. 先跑一次
repo → **Actions** → 左邊點「**回測**」→ **Run workflow**，跑完再點「**每 4 小時更新**」→ **Run workflow**。
之後每 4 小時會自動跑（台灣時間 0／4／8／12／16／20 點多一點）。

### 4. 開網站
- **Public repo**：Settings → Pages → Source 選 `Deploy from a branch`、`main`、`/ (root)` → Save。網址是 `https://你的帳號.github.io/copilot-daily/`
- **Private repo**（不想讓人看到訊號）：GitHub Pages 免費版不支援私人 repo，改用你已經在用的 **Netlify**：Add new site → Import from GitHub → 選這個 repo → Build command 留空、Publish directory 填 `.` → Deploy。之後每次資料更新會自動重新部署

iPhone 用 Safari 打開網站 → 分享 → **加入主畫面**，就能像 App 一樣開。

---

## 資料來源（重要）

GitHub 的伺服器在美國，**幣安合約 API 會擋美國 IP**，程式會自動改抓幣安現貨公開資料。大部分幣的現貨和合約價格幾乎一樣，但**只有合約、沒有現貨的幣會抓不到**，網頁最上面會顯示「抓不到資料」。

要用合約資料，就改在你 DigitalOcean 印度機房的主機上跑（`run_on_server.sh` 裡有安裝步驟，用 cron 每 4 小時跑一次，跑完自動推回 GitHub），然後把 GitHub 上的「每 4 小時更新」停用，避免兩邊同時跑。

---

## 怎麼看成績單

- **R**：1R ＝ 這筆停損會虧的錢。+2R ＝ 賺到停損金額的 2 倍。用 R 比較，不同幣、不同倉位才能放在一起看
- **平均每筆（期望值）**：最重要的數字。大於 0 才代表這套規則長期有機會賺；勝率高但平均 R 是負的，一樣是賠
- **少於 30 筆不要下結論**：網頁會提醒
- **實盤 vs 回測**：實盤平均每筆比回測少很多 → 規則可能只是剛好適合過去，先縮小倉位或暫停

---

## 改規則

所有參數都在 `config.json` 的 `rules`（白話說明在網頁最下面）。觀察的幣種在 `symbols`。

改完一定要重跑「回測」再看成績。**不要一直改參數直到回測很漂亮**——那只是把規則量身訂做給過去，實盤通常會打回原形。比較健康的做法：改一次、記下原因、讓實盤跑一個月再比。

---

## 檔案說明

```
config.json                 觀察幣種、規則參數、手續費、名目金額
scripts/data_source.py      抓幣安 K 線（合約 → 現貨自動切換），只留已收盤的
scripts/engine.py           指標、日線方向鎖定、4H 進場、停損止盈、結算、成績單（排程和回測共用）
scripts/update.py           每 4 小時主程式：結算舊單 → 找新單 → 寫 JSON → 推播
scripts/ai_summary.py       Claude 寫總結（沒金鑰就規則組句）
scripts/notify.py           Telegram 推播
research/backtest.py        回測（同一套規則丟回過去）
run_on_server.sh            改在自己主機跑時用
.github/workflows/          排程設定
index.html                  網站
data/                       程式自動產生，不要手動改
```

本機測試（不連網、用假資料）：
```
MOCK_DATA=1 python research/backtest.py
MOCK_DATA=1 python scripts/update.py
python scripts/ai_summary.py
python -m http.server 8000   # 打開 http://localhost:8000
```

---

## 限制

- 沒算滑價、資金費率、爆倉；實際成績通常會比回測差一點
- GitHub 排程偶爾會晚幾十分鐘，甚至跳過一次；跳過的那根 K 線不會補發訊號（避免用過時的價格進場）
- 公開 repo 太久沒有活動，GitHub 可能暫停排程並寄信通知；收到信就到 Actions 頁面按一下重新啟用
- 訊號是規則算出來的參考，不是投資建議
