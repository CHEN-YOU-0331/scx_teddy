# scx_teddy

一個基於 eBPF 的實驗性排程器，能夠在線收集任務執行行為，使用 ML 模型（K-means）對任務進行分群，並根據分群結果動態調整排程優先權與時間片。

## 系統需求

- 支援 sched_ext 的 Linux 核心
- Root 權限（eBPF 操作所需）
- Rust 工具鏈
- libbpf
- Python 3 及 `numpy`、`pandas`、`scikit-learn`（訓練用）

## 建置

```bash
cargo build --release
```

安裝 Python 依賴：

```bash
pip install -r requirements.txt
```

## 使用方式

scx_teddy 有兩種模式：**collect**（收集任務資料）以及 **classify**（套用訓練好的模型進行排程）。

### 步驟一：收集任務資料

以 collect 模式執行排程器，將任務行為記錄到 CSV 檔案：

```bash
sudo ./target/release/scx_teddy -m collect -c 60 -o event.csv
```

**選項：**
- `-m, --mode <模式>` - 運作模式：`collect` 或 `classify`（預設：`collect`）
- `-c, --collect-duration <秒數>` - 資料收集間隔，單位為秒（預設：600）
- `-o, --output <路徑>` - 輸出 CSV 檔案路徑（預設：`event.csv`）
- `--min-events <N>` - 最低事件數門檻，低於此數的任務會被忽略（預設：3）
- `--csv-checkpoint` - 每個收集週期都寫入 CSV。預設情況下 CSV 會保留在記憶體中，僅在關閉時寫入一次；啟用此選項可在每個週期 checkpoint，避免當機或 `kill -9` 導致本次收集的資料遺失。
- `-v, --verbose` - 啟用詳細輸出

### 步驟二：訓練 K-means 模型

使用訓練腳本，根據任務的執行特徵進行分群：

```bash
python3 train.py event.csv -o model.json
```

預設會使用 CSV 中的全部任務。若要限定特定工作負載，可傳入 `train_config.config`，每行填一個 comm prefix：

```bash
python3 train.py event.csv -o model.json --train-config train_config.config
```

專案提供的 `train_config.config` 範例已預設包含 `bench_mark.sh` 的所有工作負載：

```
# stress-ng workloads
stress-ng-cpu
stress-ng-hdd
stress-ng-switc
stress-ng-timer

# custom workloads
slow-timer
random-timer
fixed-mutex
```

每行以 prefix 方式比對任務的 `comm`，`#` 開頭與空白行會被忽略。

這會：
- 使用手肘法自動選擇分群數量（或以 `-k` 手動指定）
- 將模型（中心點 + 標準化參數）匯出為 JSON 檔案
- 印出每個 cluster 的特徵統計與任務成員（tid、tgid、ppid、command）
- 將分類結果儲存至 `model_result.json`

**選項：**
- `-o, --output <路徑>` - 模型 JSON 輸出路徑（預設：`model.json`）
- `-k, --clusters <N>` - 分群數量（未指定則自動偵測）
- `--train-config <路徑>` - comm prefix 篩選清單（預設：使用全部任務）
- `--filter-tid <TID...>` - 依 tid 篩選
- `--filter-tgid <TGID...>` - 依 tgid 篩選
- `--filter-cmd <CMD...>` - 依精確命令名稱篩選

### 步驟三：設定排程策略

建立 `config.json`，將每個 cluster 對應到優先權與時間片策略：

```json
{
  "clusters": {
    "0": { "prio": 2, "slice_mode": "adaptive", "slice_sigma": 1.0 },
    "1": { "prio": 3, "slice_mode": "fixed", "slice_ns": 100000 },
    "4": { "prio": 0, "slice_mode": "adaptive", "slice_sigma": 2.0 }
  },
  "default": { "prio": 3, "slice_mode": "fixed", "slice_ns": 100000 }
}
```

- **prio**：排程優先權層級（0 = critical、1 = interactive、2 = normal、3 = batch）
- **slice_mode**：
  - `adaptive`：時間片 = 平均執行時間 + sigma * 標準差（依每個任務計算）
  - `fixed`：時間片 = 固定值，單位為奈秒

### 步驟四：以分類模式執行

套用訓練好的模型，動態分類任務並更新排程參數：

```bash
sudo ./target/release/scx_teddy -m classify -c 60 --model model.json --config config.json
```

**分類模式額外選項：**
- `--model <路徑>` - 訓練好的模型 JSON 路徑（必填）
- `--config <路徑>` - 排程設定 JSON 路徑（必填）
- `-c, --collect-duration <秒數>` - 刷新間隔，單位為秒（預設：600）

---

[English Documentation](README.md)
