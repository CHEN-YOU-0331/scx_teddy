# scx_teddy Dashboard · Streamlit

這是一個包裝在 scx_teddy 與 `train.py` 之上的輕量 GUI。它本身不實作額外邏輯，而是透過 `scx_runner.py` 執行與使用者手動輸入時相同的指令。

因此，即使 GUI 關閉或崩潰，已經啟動的 scheduler 仍會持續運作，不受影響。

由於 scx_teddy 需要 root 權限來載入與執行 BPF 程式，相關執行指令都會透過 `sudo` 包裝。GUI 預設假設使用者具備執行 `sudo` 的權限。

## 環境安裝

一次性建好 repo 根目錄的 venv 並裝依賴(`requirements.txt` 含 `streamlit` +
`plotly` 等)：

```sh
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

`run.sh` 會自己鎖用這個 `venv/bin/python3`，所以日後啟動不必先手動 activate。

## 啟動

```sh
cd gui
sudo ./run.sh
```

鎖 repo `venv/bin/python3`、開深色主題、headless `streamlit run app.py`，預設
<http://localhost:8501>。

## 五個分頁

### 📥 Collect
跑 `sudo -E scx_teddy --mode collect`，把每個 task 的特徵寫成 CSV。
- 輸出預設 `/tmp/scx_teddy_gui`(tmpfs)，勾 Custom output dir 可自訂輸出位置；檔名一律時間戳自動命名。
- 串流 log / Stop 送 SIGINT 乾淨退出(flush CSV) / Clear `.csv` 一鍵清空。
- 下方列 saved CSVs，多選可複製到指定目錄。
- **Specialization target**：可選一個目標 ppid(見下方「Target 面板」)。collect
  模式下這只是標記 — CSV 的 `ancestor` 欄會收斂到該 ppid，分析時就能認出整個家族。

### 🧠 Train
跑 `train.py` 訓練 KMeans，輸出 `model_<ts>.json` + 旁邊的 `_result.json`
(每個 cluster 的成員清單)。
- Input CSV 下拉預設選最近一次 collect 的。K 留空 = elbow 自動選。

### 🗺 Static t-SNE
對任一 CSV 跑 t-SNE，plotly 出圖(可縮放、hover 顯示 tid/comm)。
- CSV 與 cluster-result JSON 都可下拉選 /tmp，或勾 Custom path 指任意路徑。
- 上色三選一：全部一色 / 按 KMeans cluster / highlight `tgid=1234,5678`(目標群染色)。

### 🎯 Classify
跑 `sudo -E scx_teddy --mode classify --model M --config C`，用訓練好的 model
即時分類任務並套排程策略。

- **Model + 排程 config 編輯器**(共用 `tabs/_config_editor.py`)：選一個 model，
  下方表格自動 sized 到它的 cluster 數(多的丟、少的補預設)，每列可編
  **prio**(0=最高 / 11=最低)、**slice_ns**(floor 100000)、**cpu_kind**
  (0=共用，否則 1-based，1=最快核；標籤按本機 topology 動態生成 P-core/E-core/
  tier-N)、**cpu_prefer**(no preference / prefer fast / prefer slow)。
  - Config 來源 radio：「Edit in GUI」從預設起編，「Existing file」載入磁碟上的
    config 當底稿(下拉選，或勾 Custom path 手填)。Start 一律序列化到 /tmp 新檔再
    `--config` 指它(原檔不動)；「Existing file」模式有 guarded「Save back to
    file」可寫回原檔。
  - **下拉掃描範圍**：model picker 與「Existing file」config 下拉除了掃 tmpfs 工作
    目錄，**也掃** repo 根目錄的 `model/` 與 `config/`。把整理好的 model 放進
    `model/`、config 放進 `config/`(任何 `*.json`)就會自動出現在這裡；目錄可有可無
    (不存在就忽略)。
- **Target family model + config**(可選，疊在 default 編輯器下方)：給特化目標一套
  **自己的** model + config(可與 default 不同 model — 雙 SchedSet 的重點)。
  - 排程器**還沒跑** → 不顯示按鈕；編輯器當下的內容會在按 Start 時自動帶入
    `--target-model/--target-config`(啟動前寫 control 檔會被 scx_teddy 初始化清掉，
    所以根本不需要先「保存」)。
  - 排程器**正在跑** → 才冒出「Apply target set」/「Clear target set」：Apply 寫
    `control_model`/`control_config`，scx_teddy 下個 poll 熱換(免重啟)；Clear 回 default。
  - ⚠️ 這套只有在你**也選了 target ppid** 時才會套到任務上。
- **Specialization target ppid**(Target 面板，見下)。
- Predict period `-c` 預設 1s(scx_teddy 內建 600s 太慢)。
- 串流 log / Stop 送 SIGINT 拆排程器。

### 📊 Overall
htop 風格實時 dashboard。整機 + per-task，1Hz 自刷(`@st.fragment(run_every="1s")`，
只刷這個 tab，不打斷別 tab 打字)。
- 頂部 metric：Total CPU% / RAM / 活躍 task 數 / core groups 組成。
- Per-CPU 條：每顆邏輯 CPU 一根 bar，按 cpufreq 自動分群染色(不寫死 P/E)。
- Task 表：全部 task(virtualised 卷軸)，按 CPU% 排序，支援 comm/tgid/ppid 過濾。
  整機那部分純 /proc，不動 scx_teddy。
- **分類欄(cluster / prio / cpu_kind / slice)接 classify snapshot**：classify 跑
  起來後 scx_teddy 每週期原子寫 `/tmp/scx_teddy/snapshot.json`(tid→分類狀態)，
  Overall 用 tid join 填這幾欄。沒在跑 classify 時這幾欄留空。

## Target 面板(`tabs/_target.py`，Collect 與 Classify 共用)

指定要特化哪個 ppid 家族。寫 `/tmp/scx_teddy/control_ppid`(root-owned，GUI 用
`sudo tee` 寫)，scx_teddy 每 `--control-interval` 秒讀一次。radio 兩模式：

- **Manual**：手動輸 ppid，Set / Clear(0)。
- **Scanner**：從 `target_finder_helper/` 下拉選一個 scanner 腳本(目前一個 Steam
  範例 `game_task_finder.py`；下拉是動態掃目錄，加新 scanner 不用改 code)，Start
  起 subprocess 持續掃並寫 control_ppid，Stop 送 SIGINT(scanner 收到會寫 0 清除)。

面板上方即時顯示目前 control_ppid。詳細協議見 `target_finder_helper/README.md`。
