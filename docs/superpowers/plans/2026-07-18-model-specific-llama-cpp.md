# モデルごとの llama.cpp 実行ファイル指定 実装計画

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans (推奨) または superpowers:subagent-driven-development を使い、この計画をタスク単位で実行する。各ステップはチェックボックスで追跡する。

**Goal:** `Config.json` のモデルごとに llama.cpp の実行ファイル絶対パスを指定でき、未指定時は従来の `llama-server` alias にフォールバックする。

**Architecture:** `ModelConfig` に任意の `llama_cpp` フィールドと、alias へのフォールバックを隠す `executable` プロパティを追加する。コントローラはモデルの `executable` を argv[0] として既存の runtime に渡し、runtime の停止・排他・ログ監視のライフサイクルは変更しない。設定検証、起動コマンド、UI ログ、ドキュメントを既存ファイルの責務に沿って更新する。

**Tech Stack:** Python 3.13、標準ライブラリ（`dataclasses`、`ntpath`、`subprocess`）、`unittest`、uv、ruff、basedpyright。

## Global Constraints

- 設定フィールド名は `llama_cpp` とする。
- `llama_cpp` は空でない Windows 絶対パス（ドライブまたは UNC を含む）だけを受け付ける。
- フィールド省略時は `llama-server` alias を使い、既存の Config.json との後方互換を維持する。
- 相対パス、環境変数展開、自動検出・ダウンロードは実装しない。
- プロジェクトの Python 依存関係は追加しない。
- 変更とテストは日本語の既存 UI/エラー表現を維持する。

---

### Task 1: 設定モデルと絶対パス検証

**Files:**
- Modify: `src/llama_server_manager.py:1-82`（`ModelConfig` と `ServerConfig.load`）
- Test: `test/test_llama_server_manager.py:70-99`（`ServerConfigTests`）

**Interfaces:**
- Consumes: 既存の `Config.json` の `models[].name` / `arguments`。
- Produces: `ModelConfig(name: str, arguments: tuple[str, ...], llama_cpp: str | None = None)` と `ModelConfig.executable -> str`。

- [ ] **Step 1: 明示パス・後方互換・不正値の失敗テストを書く**

`test/test_llama_server_manager.py` の `ServerConfigTests` に次のテストを追加し、既存の読み込みテストの期待値には `llama_cpp is None` が保たれることを含める。

```python
    def test_loads_model_specific_llama_cpp_absolute_path(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "Config.json"
            config_path.write_text(
                json.dumps(
                    {
                        "models": [
                            {
                                "name": "Qwen",
                                "llama_cpp": r"D:\\AI\\llama.cpp\\bin\\llama-server.exe",
                                "arguments": ["--model", "Qwen.gguf"],
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )

            config = ServerConfig.load(config_path)

        self.assertEqual(
            config.models,
            (
                ModelConfig(
                    name="Qwen",
                    arguments=("--model", "Qwen.gguf"),
                    llama_cpp=r"D:\\AI\\llama.cpp\\bin\\llama-server.exe",
                ),
            ),
        )

    def test_rejects_empty_relative_and_non_string_llama_cpp_values(self) -> None:
        invalid_values = ("", r"bin\\llama-server.exe", 123)
        for invalid_value in invalid_values:
            with self.subTest(invalid_value=invalid_value), tempfile.TemporaryDirectory() as directory:
                config_path = Path(directory) / "Config.json"
                config_path.write_text(
                    json.dumps(
                        {
                            "models": [
                                {
                                    "name": "Qwen",
                                    "llama_cpp": invalid_value,
                                    "arguments": [],
                                }
                            ]
                        }
                    ),
                    encoding="utf-8",
                )

                with self.assertRaisesRegex(ValueError, r"llama_cpp"):
                    ServerConfig.load(config_path)
```

`test/test_llama_server_manager.py` の import に `json` を追加する。既存の `test_loads_model_name_and_arguments_from_config_json` は省略時の `None` と alias フォールバックを確認するために残す。

- [ ] **Step 2: 失敗を確認する**

Run: `uv run python -m unittest test.test_llama_server_manager.ServerConfigTests -v`

Expected: 明示パスがモデルに保持されず、不正値テストが `ValueError` を受け取れないため FAIL。

- [ ] **Step 3: `ModelConfig` と `ServerConfig.load` を最小実装する**

`src/llama_server_manager.py` に `ntpath` を import し、次の形でフィールド・プロパティ・検証を追加する。

```python
import ntpath


@dataclass(frozen=True)
class ModelConfig:
    name: str
    arguments: tuple[str, ...]
    llama_cpp: str | None = None

    @property
    def executable(self) -> str:
        return self.llama_cpp or EXECUTABLE_ALIAS
```

`ServerConfig.load` の各 entry で `llama_cpp = entry.get("llama_cpp")` を取得し、指定値が `None` でない場合は、文字列・空白でないこと、`ntpath.isabs(llama_cpp)` と `ntpath.splitdrive(llama_cpp)[0]` が真であることを検証する。失敗時は `models[{index}].llama_cpp は空でない絶対パス文字列である必要があります。` を `ValueError` として送出する。`ModelConfig` 生成時に `llama_cpp=llama_cpp` を渡す。

- [ ] **Step 4: テストが通ることを確認する**

Run: `uv run python -m unittest test.test_llama_server_manager.ServerConfigTests -v`

Expected: PASS。既存の省略設定は `llama_cpp=None` のまま。

- [ ] **Step 5: タスクをコミットする**

```powershell
git add src/llama_server_manager.py test/test_llama_server_manager.py
git commit -m "モデル設定にllama.cpp実行ファイルパスを追加"
```

### Task 2: 起動コマンドと実行ファイル解決エラー

**Files:**
- Modify: `src/llama_server_manager.py:200-250`（`WindowsServerRuntime.start` と `LlamaServerController.start`）
- Test: `test/test_llama_server_manager.py:285-390`（コントローラ・runtime テスト）

**Interfaces:**
- Consumes: Task 1 の `ModelConfig.executable`。
- Produces: `ServerRuntime.start(command)` に `[model.executable, *model.arguments]` を渡す既存の公開動作。

- [ ] **Step 1: 明示パスの argv[0] と runtime のエラーを検証する失敗テストを書く**

`LlamaServerControllerTests` に次を追加する。

```python
    def test_start_uses_model_specific_llama_cpp_path(self) -> None:
        runtime = FakeRuntime()
        controller = LlamaServerController(runtime=runtime, on_log=lambda _line: None)
        model = ModelConfig(
            name="Qwen",
            llama_cpp=r"D:\\AI\\llama.cpp\\bin\\llama-server.exe",
            arguments=("--model", "Qwen.gguf"),
        )

        controller.start(model)

        self.assertEqual(
            runtime.commands,
            [[r"D:\\AI\\llama.cpp\\bin\\llama-server.exe", "--model", "Qwen.gguf"]],
        )

    def test_missing_model_specific_executable_is_reported_with_path(self) -> None:
        executable = r"D:\\missing\\llama-server.exe"
        with patch("llama_server_manager.shutil.which", return_value=None):
            with self.assertRaisesRegex(RuntimeError, re.escape(executable)):
                WindowsServerRuntime().start([executable])
```

このテストの import に `json` と `re` が必要になる場合は同じファイルの先頭へ追加する。

- [ ] **Step 2: 失敗を確認する**

Run: `uv run python -m unittest test.test_llama_server_manager.LlamaServerControllerTests test.test_llama_server_manager.WindowsServerRuntimeTests -v`

Expected: コントローラが `llama-server` を argv[0] にして明示パステストが FAILし、runtime のエラーメッセージにもパスが含まれず FAIL。

- [ ] **Step 3: 起動コマンドと解決失敗メッセージを実装する**

`LlamaServerController.start` のコマンド生成を次に変更する。

```python
command = [model.executable, *model.arguments]
```

`WindowsServerRuntime.start` は `executable = command[0]` とし、`shutil.which(executable)` が `None` の場合に次を使い分ける。

```python
if shutil.which(executable) is None:
    if executable == EXECUTABLE_ALIAS:
        raise RuntimeError(
            '"llama-server" が PATH 上に見つかりません。Vulkan ビルドの '
            "llama-server.exe を PATH に追加してください。"
        )
    raise RuntimeError(
        f"指定された llama.cpp 実行ファイルが見つかりません: {executable}"
    )
```

既存の DLL ディレクトリ処理と `subprocess.Popen(command, ...)` は変更しない。

- [ ] **Step 4: テストが通ることを確認する**

Run: `uv run python -m unittest test.test_llama_server_manager.LlamaServerControllerTests test.test_llama_server_manager.WindowsServerRuntimeTests -v`

Expected: PASS。既存 alias のテストと PyInstaller DLL テストも PASS。

- [ ] **Step 5: タスクをコミットする**

```powershell
git add src/llama_server_manager.py test/test_llama_server_manager.py
git commit -m "モデル指定のllama.cppを起動コマンドに反映"
```

### Task 3: UI の起動ログ、サンプル設定、利用方法の更新

**Files:**
- Modify: `src/main.py:219-230`（起動完了イベントのログ）
- Modify: `test/test_llama_server_manager.py:150-280`（UI イベントテスト）
- Modify: `README.md:5-38`（起動と Config.json の説明）
- Modify: `Config.json:3-68`（現行モデルの明示設定）

**Interfaces:**
- Consumes: Task 1 の `ModelConfig.executable`。
- Produces: UI ログに `$ {model.executable} {arguments}` を表示し、既存設定例と実ファイルの形式を一致させる。

- [ ] **Step 1: 起動ログが選択実行ファイルを表示する失敗テストを書く**

`OperationQueueTests` に、既存の `_drain_events` テストと同じ最小 `object.__new__(LlamaServerApp)` 方式で次を追加する。

```python
    def test_start_completion_logs_selected_executable(self) -> None:
        class Value:
            def __init__(self) -> None:
                self.value = ""

            def set(self, value: str) -> None:
                self.value = value

        executable = r"D:\\AI\\llama.cpp\\bin\\llama-server.exe"
        model = ModelConfig(
            name="Qwen",
            llama_cpp=executable,
            arguments=("--model", "Qwen.gguf"),
        )
        app = object.__new__(LlamaServerApp)
        app._events = queue.Queue()
        app._events.put(("operation_complete", (1, "start", model)))
        app._stop_request_id = 0
        app._stop_requested_at = None
        app._metrics = {}
        app._metrics_text = Value()
        app._status = Value()
        logs: list[str] = []
        app._append_log = logs.append
        app.after = lambda _delay, _callback: None

        app._drain_events()

        self.assertEqual(
            logs,
            [f"$ {executable} --model Qwen.gguf"],
        )
```

- [ ] **Step 2: 失敗を確認する**

Run: `uv run python -m unittest test.test_llama_server_manager.OperationQueueTests.test_start_completion_logs_selected_executable -v`

Expected: 現状の `$ llama-server ...` ログとの差分で FAIL。

- [ ] **Step 3: UI ログをモデルの実行ファイルに変更する**

`src/main.py` の該当行を次に変更する。

```python
self._append_log(f"$ {model.executable} {' '.join(model.arguments)}")
```

README の起動説明から「alias のみを起動する」という固定記述を削除し、`llama_cpp` の任意指定、絶対パス、未指定時の alias フォールバック、設定再読込の手順を記載する。`Config.json` の各現行モデルには、現行環境の `D:\\AI\\llama-cpp-turboquant\\build-vulkan\\bin\\llama-server.exe` を `llama_cpp` として追加し、従来動作を維持する。

- [ ] **Step 4: UI テストとドキュメント整合性を確認する**

Run: `uv run python -m unittest test.test_llama_server_manager.OperationQueueTests -v`

Expected: PASS。続けて `Get-Content Config.json | ConvertFrom-Json` が成功し、README の JSON 例に `llama_cpp` と絶対パスの説明がある。

- [ ] **Step 5: タスクをコミットする**

```powershell
git add src/main.py test/test_llama_server_manager.py README.md Config.json
git commit -m "UIログと設定例にモデル別llama.cppを反映"
```

### Task 4: 全体検証と最終確認

**Files:**
- Verify: `src/llama_server_manager.py`, `src/main.py`, `test/test_llama_server_manager.py`, `tests/test_main.py`, `README.md`, `Config.json`

**Interfaces:**
- Consumes: Task 1〜3 の変更。
- Produces: テスト・静的解析が通り、仕様書の要件を満たす作業ツリー。

- [ ] **Step 1: 全 unittest を実行する**

Run: `uv run python -m unittest test.test_llama_server_manager tests.test_main -v`

Expected: 全テスト PASS、失敗・エラー 0 件。

- [ ] **Step 2: lint と型チェックを実行する**

Run: `uv run ruff check src test tests`

Expected: `All checks passed!`

Run: `uv run basedpyright src test tests`

Expected: 型エラー 0 件。既存の環境依存警告が出る場合も、新規エラーがないことを確認する。

- [ ] **Step 3: JSON と差分を確認する**

Run: `Get-Content Config.json | ConvertFrom-Json | Out-Null; git diff --check; git status --short`

Expected: JSON パース成功、空白エラーなし、意図したファイルだけが変更されている。

- [ ] **Step 4: 最終フルスイートを再実行する**

Run: `uv run python -m unittest test.test_llama_server_manager tests.test_main -v`

Expected: 直前の変更を含めて全テスト PASS。

- [ ] **Step 5: 検証結果を記録する**

最終報告に、変更ファイル、`llama_cpp` の設定例、未指定時の alias フォールバック、実行したテスト・lint・型チェックの結果を日本語で記載する。
