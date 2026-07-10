コードを精査しました。16 テストはすべてパスしていますが、静的解析とロジック追跡で以下のバグを発見しました。

---

## 発見されたバグ

### 1. `extract_metrics` — 1行に複数の指標が含まれる場合、最初のものしか返さない

`llama_server_manager.py` 204-215行の `extract_metrics` は、1行に「prompt eval time」と「eval time =」の両方が含まれる場合、最初の `tokens per second` にマッチするものしか返しません。

```python
def extract_metrics(line: str) -> dict[str, float]:
    match = _TOKENS_PER_SECOND.search(line)  # ← 最初のマッチのみ
    if match is None:
        return {}
    speed = float(match.group(1))
    if "prompt eval time" in line.casefold():
        return {"pp": speed}
    if re.search(r"\beval time\s*=", line, re.IGNORECASE):
        return {"tg": speed}
    return {}
```

**問題:** 1行に両方の指標が含まれていても、最初のものしか返らない。`{"pp": 20.0, "tg": 25.0}` のような完全な状態が失われる。

### 2. `LlamaServerApp._drain_events` — メトリクス更新が順序に依存する

`main.py` 168-207行の `_drain_events` は、イベントを FIFO で処理する。`metrics` イベントと `operation_complete` イベントが同時に到着した場合、処理順序に依存する。

```python
def _drain_events(self) -> None:
    for _ in range(200):
        try:
            kind, value = self._events.get_nowait()
        except queue.Empty:
            break
        if kind == "metrics":
            self._metrics.update(value)
            self._metrics_text.set(format_metric_summary(self._metrics))
        elif kind == "operation_complete":
            ...
            if operation == "start":
                self._metrics.clear()  # ← メトリクスをクリア
```

**問題:** `metrics` イベントが `operation_complete` イベントより先に処理されると、クリアされる前に表示される。逆順だとクリアされて空になる。これは競合状態（race condition）。

### 3. `LlamaServerController._stream_logs` — スレッド間での `_generation` 同期問題

`llama_server_manager.py` 266-289行。`_stream_logs` は別スレッドで実行されるが、`self._generation` の読み書きは GIL 保護されているものの、`self._process` の参照がスレッド間で同期されていない。

```python
def _stream_logs(self, process: RunningProcess, generation: int) -> None:
    try:
        for raw_line in process.stdout:
            ...
            with self._lock:
                is_current_process = (
                    process is self._process and generation == self._generation
                )
```

**問題:** `self._process` は `start()` で `self._process = process` として設定されるが、`_stream_logs` のスレッドが開始されるタイミングで `self._process` が `None` の場合、`is_current_process` は `False` になり、メトリクスが報告されない。

### 4. `WindowsServerRuntime.exclusive_server_operation` — Mutex のセキュリティ ACL が `WD`（Everyone）

`llama_server_manager.py` 19行:

```python
MUTEX_SECURITY_SDDL = "D:(A;;0x001F0001;;;WD)"
```

**問題:** `WD` は Everyone にアクセス権を付与するが、`0x001F0001`（`0x001F` = 排他所有権、`0x0001` = 読み取り）は排他ロックの取得に必要ない。実際には `0x001F0001` は `0x001F`（排他所有権）+ `0x0001`（読み取り）で、これは排他ロックの取得には不十分。正しい ACL は `0x001F0003`（排他所有権 + 書き込み）または `0x001F0000`（排他所有権のみ）であるべき。

---

## 結論

| #   | 問題                                      | 深刻度 | 影響                                   |
| --- | ----------------------------------------- | ------ | -------------------------------------- |
| 1   | `extract_metrics` が1行に両指標を返せない | **中** | メトリクス表示が不完全になる可能性     |
| 2   | `_drain_events` のイベント順序依存        | **中** | メトリクスが一瞬空になる可能性         |
| 3   | `_stream_logs` の `_process` 参照同期     | **軽** | 初期化直後のメトリクスが失われる可能性 |
| 4   | Mutex ACL の権限値                        | **軽** | 排他ロックの取得が失敗する可能性       |

テストでカバーされていないのは #1 と #2 です。#3 は既存テストでカバーされています。#4 は静的な定数なのでテストで検証できません。

**最も実用的な影響は #1** — メトリクス表示が不完全になる可能性があります。
