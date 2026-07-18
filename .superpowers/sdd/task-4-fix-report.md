# Task 4 型整合修正レポート

## 対象と変更範囲

`LlamaServerController(runtime=FakeRuntime(...))` に対する basedpyright の Protocol 不整合だけを修正した。実行時の contextmanager 処理、サーバー起動・停止の feature code は変更していない。

- `ServerRuntime.exclusive_server_operation` の戻り値を `AbstractContextManager[None]` と明示した。
- `FakeProcess.stdout` を `list[str]` と明示した。
- `RunningProcess.stdout` は呼び出し側が読み取るだけなので、可変属性ではなく読み取り専用 Protocol property として表現した。これにより `list[str]` の FakeProcess を `Iterable[str]` と共変に適合させた。

## RED

実行コマンド:

```powershell
$env:UV_CACHE_DIR = (Join-Path (Get-Location) '.uv-cache'); uv run basedpyright test/test_llama_server_manager.py
```

変更前は exit 1、`39 errors, 143 warnings`。`test/test_llama_server_manager.py:439:52` を含む `FakeRuntime` の引数エラーで、次の二つが原因だった。

1. 未注釈の `ServerRuntime.exclusive_server_operation` が `None` と推論され、`@contextmanager` の戻り値と適合しない。
2. `FakeProcess.stdout` の型が Protocol 判定に十分でなく、明示的に `list[str]` とした後は `RunningProcess.stdout` の可変属性不変性が残る。

## GREEN

同じ focused basedpyright を再実行した。コマンド全体は既存診断のため exit 1 のままだが、対象回帰は解消した。

- 変更後: `25 errors, 142 warnings`
- 対象 14 箇所（439 行を含む）の Protocol 関連 error: `0`
- `FakeRuntime`、`FakeProcess`、`exclusive_server_operation`、`RunningProcess` を含む残存 error: `0`

新規エラーがない根拠は、focused error 総数が 39 から 25 に減少し、JSON 診断で残存 25 件がすべて既存の UI テストスタブ（240--419 行）の型不整合であることを確認した点にある。変更した `src` の Protocol 定義と `FakeProcess.stdout` に診断はない。

## 残存する既存 basedpyright 診断

focused 対象には `25 errors, 142 warnings` が残る。主な残存 error は `LlamaServerApp` にテスト用の controller、`StringVar`、`after` callback を直接差し替える既存テストスタブの型不整合であり、本修正の範囲外である。

## 検証結果

| コマンド | 結果 |
| --- | --- |
| `uv run basedpyright test/test_llama_server_manager.py` | 対象 Protocol error は 0。残存 `25 errors, 142 warnings` は既存分。 |
| `uv run python -m unittest test.test_llama_server_manager tests.test_main -q` | `Ran 35 tests`、`OK`（0.942s）。 |
| `uv run ruff check src test tests` | exit 0。 |
| `git diff --check` | exit 0。 |

## コミット

型整合修正コミット: `442e3c0f2a5655ec0025ce7984356c1825224550` (`fix: Protocol 型を整合`)
## 最終レビュー指摘修正

### 指摘

明示した llama.cpp 実行ファイルについて、`shutil.which(executable)` が対象パスを返した後に `subprocess.Popen()` が `OSError` を送出すると、生の `OSError` が伝播していた。設計要件どおり、存在しない場合だけでなく起動できない場合も対象パス付きの日本語 `RuntimeError` にする必要がある。

### RED

`WindowsServerRuntimeTests.test_unlaunchable_model_specific_executable_is_reported_with_path` を追加した。`which` は明示パスを返し、`Popen` は `OSError("launch failed")` を送出する。実装前は `OSError` が捕捉され、`RuntimeError` を要求するアサーションが失敗した。

### 最小修正

`WindowsServerRuntime.start` の既存 `_DllDirectoryRestoreError` ハンドラの直後に `OSError` ハンドラを追加した。明示 executable の場合だけ、次の形式で例外を変換する。

```python
RuntimeError(f"指定された llama.cpp 実行ファイルを起動できません: {executable} ({error})")
```

`_DllDirectoryRestoreError` は先に捕捉されるため、DLL 復元失敗の優先処理と開始済みプロセスの terminate/wait 後始末は維持される。`llama-server` alias の `OSError` は従来どおり再送出される。

### GREEN と検証

- 新規 focused test: `Ran 1 test`、`OK`。
- `WindowsServerRuntimeTests`: `Ran 10 tests`、`OK`。alias の起動失敗、DLL 復元失敗時の例外優先・後始末を含む既存ケースも通過。
- 全 unittest: 新規テスト追加により `Ran 36 tests`、`OK`（従来の 35 件から 1 件増加）。
- `uv run ruff check src test tests`: exit 0。
- `git diff --check`: exit 0。