# scx_teddy Dashboard · Streamlit

> **三個分頁,全部接真後端**(Collect / Train / Static t-SNE)。仿真版實時儀表板(Overall / Per-task / Cluster)的 code 跟假資料一起放在 `_mock_stash/`,**不進 git**;之後一個一個改成真版時搬進 `tabs/`。

## 結構

```
gui/
  app.py              ← 入口:CSS、sidebar、三個 tab
  theme.py            ← 配色 / P-E core 編號 / Tier 名(仿真版也會用)
  scx_runner.py       ← subprocess + 檔案管理(起 scx_teddy / train.py、時間戳命名、列檔、複製)
  run.sh              ← 啟動腳本(鎖 venv、設深色主題)
  tabs/               ← 真後端三頁
    _common.py        ← 共用 helper(log buffer、rerun 觸發)
    collect.py
    train.py
    static_tsne.py
  _mock_stash/        ← gitignore 這整個目錄
    mock_data.py
    overall_page.py
    per_task_page.py
    cluster_page.py
```

**為什麼目錄叫 `tabs/` 而不是 `pages/`**:Streamlit 看到 entry script 旁邊有 `pages/` 子目錄會**自動**把它當 multipage app 處理,把每個 `.py` 列進左側 sidebar — 就跟我們的頂部 tabs 重複了。換名字避開這機制。

要讓 git 忽略 `_mock_stash/`,在 repo 根目錄 `.gitignore` 加一行:`gui/_mock_stash/`。

## 啟動

```sh
sudo ./run.sh
```

會自動鎖 repo `venv/bin/python3`、開深色主題、`streamlit run app.py`。
預設打開 <http://localhost:8501>(會跳預設瀏覽器)。

依賴:`streamlit` 與 `plotly`,已加進 `requirements.txt`。

## 三個分頁

### 📥 Collect · 真後端
- 跑 `sudo -E scx_teddy --mode collect`(BPF 需要 root)。
- 預設輸出 `/tmp/scx_teddy_gui`,勾「Custom output dir」可直接寫 SSD。檔名一律時間戳自動命名(絕不撞「已存在」)。
- 串流 log 顯示 / Stop 送 SIGINT 乾淨退出 / Clear `.csv` 一鍵清空。
- 下方列出 saved CSVs,多選後可複製到指定目錄。

### 🧠 Train · 真後端
- 跑 `train.py` 訓練 KMeans,輸出 `model_<ts>.json` + 旁邊的 `_result.json`。
- Input CSV 下拉預設選最近一次 collect 的(`runner.last_csv`)。
- Custom model out dir 同 Collect。K 留空 = elbow 自動選。

### 🗺 Static t-SNE · 真後端
- 對任一 CSV 跑 t-SNE,**plotly 出圖**(可縮放、hover 顯示 tid/comm)。
- 預設選最近 CSV 與最近 model 的 `_result.json`。
- Highlight 框輸 `tgid=1234,5678`,把目標群染紅、其他變灰。

## 仿真版實時儀表板(`_mock_stash/`,不進 git)

三頁:**Overall**(整機脈動 + per-CPU strip + Top-20 表)、**Per-task**(挑任務看歷史 + tier/slice/features)、**Cluster**(假 t-SNE + highlight)。配色與布局已定型,等 scx_teddy 端能匯出實時資料時搬進 `tabs/`。

本機要看效果:把 `app.py` 裡 `MOCK_TABS_ENABLED = False` 改成 `True`(別 commit)。

## 配色 & 視覺設計理念(`theme.py`)

- **深色主題 + plotly_dark**:長時間看不累、顏色更跳。
- **P-core 紅 / E-core 藍**:溫度直覺(perf=熱,efficiency=冷),一眼分得出 hybrid 兩半。
- **Tier 顏色**:CRITICAL 紅 → BATCH 灰,亮度遞減,眼睛自然先看到要緊的。
- **Cluster 用 matplotlib `tab10`** 同款 palette,跟 sklearn / matplotlib 預設一致。
- **Active tab 用珊瑚紅**邊框 + 加粗,跟整體配色呼應。

## 後續

- 把仿真版三頁一個一個改成真後端(資料源從 mock 換成 scx_teddy ringbuf / /proc / cgroup 等)。
- 屆時把對應 `*_page.py` 從 `_mock_stash/` 搬進 `tabs/`,設計樣式直接沿用。
