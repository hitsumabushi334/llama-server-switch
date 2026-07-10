# llama-server Switch

Vulkan ビルド済みの `llama-server.exe` を、モデルごとの設定で切り替えて起動する Tkinter 管理ツールです。

## 起動

`llama-server.exe` が `llama-server` という alias で PATH から解決できることを確認してから実行します。

```powershell
where llama-server
uv run python src/main.py
```

この環境では `D:\AI\llama-cpp-turboquant\build-vulkan\bin\llama-server.exe` が alias として検出されています。ツールはこの alias のみを起動し、別のバックエンドをビルドまたは使用しません。

## `Config.json`

`Config.json` の `models` に、表示名と `llama-server` 起動引数を追加します。引数はシェル文字列ではなく、各要素を分けた JSON 配列です。

```json
{
  "models": [
    {
      "name": "Qwen 14B",
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

設定を保存後、アプリの **Config.json を再読込** を押します。モデルの起動・切替前には、OS 上で検出した全 `llama-server.exe` プロセスツリーを停止し、停止を再確認してから `llama-server` を起動します。停止できないプロセスが残った場合は、新しいサーバーを起動しません。

## 表示

- **状態** タブ: `PP`（prompt processing）と `Tg`（token generation）の最新速度を `tok/s` で表示します。
- **すべてのログ** タブ: `llama-server` の標準出力・標準エラーをリアルタイム表示します。
