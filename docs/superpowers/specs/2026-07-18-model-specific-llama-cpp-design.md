# モデルごとの llama.cpp 実行ファイル指定 設計書

## 背景と目的

現在は `LlamaServerController` が常に `llama-server` alias を起動するため、Turboquant 版の llama.cpp に固定されている。モデルごとに異なる llama.cpp ビルドを使えるよう、モデル設定に実行ファイルの絶対パスを指定できるようにする。

## 要件

- `Config.json` の各モデルに任意の `llama_cpp` フィールドを追加できる。
- `llama_cpp` は空でない Windows 絶対パスとして検証する。
- `llama_cpp` がない既存設定は、従来どおり `llama-server` alias を使う。
- 指定した実行ファイルは、既存の停止処理、排他ロック、ログ収集、メトリクス処理を経由して起動する。
- 指定パスが起動時に解決できない場合は、対象パスを含むエラーを表示し、新しいサーバーを起動しない。
- 起動ログには、実際に選択した実行ファイルを表示する。

## 採用する構成

### 設定モデル

`ModelConfig` に次のフィールドを追加する。

```python
llama_cpp: str | None = None
```

`ServerConfig.load()` は、フィールドが省略された場合は `None` を設定する。指定値は文字列で、空白だけでなく、Windows の絶対パスでなければ `ValueError` とする。パスの存在確認は設定読込時には行わず、実行ファイルを配置してから再読込できるようにする。

### 起動コマンド

モデルから実行ファイルを選び、既存の引数配列をそのまま後ろに連結する。

- 指定あり: `[model.llama_cpp, *model.arguments]`
- 指定なし: `["llama-server", *model.arguments]`

`LlamaServerController` はこのコマンドを `ServerRuntime.start()` に渡すだけにし、停止・切替・ログ監視のライフサイクルは変更しない。UI の起動ログも同じ実行ファイル名を使う。

### 実行環境

`WindowsServerRuntime.start()` は argv[0] が alias または絶対パスであることを意識せず、既存の `shutil.which()` による解決確認と `subprocess.Popen()` を適用する。解決に失敗した場合は、対象の実行ファイルをメッセージに含める。PyInstaller の DLL 検索ディレクトリ退避・復元処理はそのまま利用する。

## 設定例

```json
{
  "models": [
    {
      "name": "Turboquant版",
      "llama_cpp": "D:\\AI\\llama-cpp-turboquant\\build-vulkan\\bin\\llama-server.exe",
      "arguments": ["--model", "D:\\models\\turboquant.gguf"]
    },
    {
      "name": "標準版",
      "llama_cpp": "D:\\AI\\llama.cpp\\build\\bin\\llama-server.exe",
      "arguments": ["--model", "D:\\models\\standard.gguf"]
    }
  ]
}
```

既存の `llama_cpp` なしのエントリも有効で、`llama-server` alias にフォールバックする。

## エラー処理

- JSON の型不正、空文字、相対パスは `ServerConfig.load()` の既存エラー表示経路で通知する。
- 実行ファイルが存在しない、または実行できない場合は `WindowsServerRuntime.start()` が対象パス付きの `RuntimeError` を送出する。
- コントローラは起動に失敗した時点でプロセスを登録せず、UI は既存の起動エラー表示を利用する。

## テスト方針

既存の unittest に次を追加・更新する。

1. `ServerConfig.load()` が明示した絶対パスを `ModelConfig.llama_cpp` に保持する。
2. `llama_cpp` 省略時に `None` となり、既存設定の期待値が変わらない。
3. 相対パス、空文字、非文字列を拒否する。
4. `LlamaServerController.start()` が明示パスを argv[0] にしたコマンドを runtime に渡す。
5. `WindowsServerRuntime.start()` の解決失敗メッセージに指定実行ファイルが含まれる。
6. UI の起動ログが選択した実行ファイルを表示する（既存のログテストがあれば更新する）。

変更後は対象 unittest、ruff、basedpyright、最後にリポジトリ全体のテストを実行する。

## 非対象

- 実行ファイルを自動検出・ダウンロード・インストールする機能。
- 相対パスや環境変数展開のサポート。
- トップレベルの実行ファイル辞書や別の設定ファイル形式。
