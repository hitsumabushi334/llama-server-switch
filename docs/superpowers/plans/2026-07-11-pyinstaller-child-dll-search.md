# PyInstaller Child DLL Search Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** PyInstaller版アプリから外部 `llama-server` を起動するとき、`_MEI...` のDLL検索先を子プロセスへ継承させない。

**Architecture:** Windowsの凍結実行時だけ、`Popen` の直前に現在のDLLディレクトリを保存して `SetDllDirectoryW(None)` を呼ぶ。`Popen` 成否にかかわらず `finally` で親プロセスの設定を復元する。

**Tech Stack:** Python 3.13, ctypes, subprocess, unittest, PyInstaller

## Global Constraints

- 既存の標準出力・標準エラー統合とイベント処理を維持する。
- 非Windowsまたは非凍結実行ではDLL検索設定を変更しない。
- `Popen` が例外を送出しても設定を復元する。
- ユーザーの既存変更を上書きしない。

---

### Task 1: 外部プロセス起動時のDLL検索先隔離

**Files:**
- Modify: `src/llama_server_manager.py`
- Modify: `test/test_llama_server_manager.py`

**Interfaces:**
- Consumes: `sys.frozen`, Windows `GetDllDirectoryW`, `SetDllDirectoryW`, `subprocess.Popen`
- Produces: 既存の `WindowsServerRuntime.start(command)` と同じ公開動作

- [ ] **Step 1: Write failing tests**

凍結Windows実行で「保存→NULL設定→Popen→復元」の順序、Popen例外時の復元、非凍結時の無操作を検証する。

- [ ] **Step 2: Verify RED**

Run: `uv run python -m unittest test.test_llama_server_manager -v`

Expected: DLL検索先の隔離が未実装のため追加テストがFAIL。

- [ ] **Step 3: Implement minimally**

`WindowsServerRuntime.start()` のPopen境界だけでDLL検索先を一時解除し、`finally` で復元する。Win32 API失敗時は明示的な例外にする。

- [ ] **Step 4: Verify GREEN and regression suite**

Run: `uv run python -m unittest test.test_llama_server_manager -v`

Run: `uv run python -m unittest discover -s test -p "test*.py"`

Run: `uv run python -m unittest discover -s tests -p "test*.py"`

Expected: all tests PASS.

- [ ] **Step 5: Static checks and package build**

Run: `uv run ruff check src/llama_server_manager.py test/test_llama_server_manager.py`

Run: `uv run pyinstaller --clean llama-server-manager.spec`

Expected: lint and build succeed.
