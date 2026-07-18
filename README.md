# llama-server Switch

Vulkan ビルド済みの `llama-server.exe` を、モデルごとの設定で切り替えて起動する Tkinter 管理ツールです。

## 起動

`Config.json` の各モデルでは、`llama_cpp` に `llama-server.exe` の Windows 絶対パスを指定して起動できます。

`llama_cpp` を省略したモデルは、従来どおり PATH 上の `llama-server` alias を起動します。省略する場合は、起動前に次で解決できることを確認してください。

```powershell
where.exe llama-server
uv run python src/main.py
```

`llama_cpp` を指定したモデルはそのパスを使用するため、alias の設定は不要です。

## `Config.json`

`Config.json` の `models` に、表示名、任意の `llama_cpp`、`llama-server` 起動引数を追加します。引数はシェル文字列ではなく、各要素を分けた JSON 配列です。
`llama_cpp` はドライブまたは UNC を含む空でない Windows 絶対パスだけを指定できます。相対パスと環境変数展開はサポートしません。

```json
{
  "models": [
    {
      "name": "Qwen 14B",
      "llama_cpp": "D:\\AI\\llama-cpp-turboquant\\build-vulkan\\bin\\llama-server.exe",
      "arguments": [
        "--model", "D:\\models\\Qwen-14B.gguf",
        "--gpu-layers", "999",
        "--ctx-size", "8192",
        "--port", "8080"
      ]
    }
  ]
}
```

設定を保存後、アプリの **Config.json を再読込** を押して、変更したモデル設定を再読込します。モデルの起動・切替前には、OS 上で検出した全 `llama-server.exe` プロセスツリーを停止し、停止を再確認してから、各モデルの `llama_cpp` または `llama-server` alias を起動します。停止できないプロセスが残った場合は、新しいサーバーを起動しません。

## 表示

- **状態** タブ: `PP`（prompt processing）と `Tg`（token generation）の最新速度を `tok/s` で表示します。
- **すべてのログ** タブ: `llama-server` の標準出力・標準エラーをリアルタイム表示します。
