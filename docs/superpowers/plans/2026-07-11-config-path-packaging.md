# Config Path Packaging Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** ソース実行時はリポジトリ直下、PyInstaller実行時はEXE隣の `Config.json` を読み込む。

**Architecture:** 設定パスの決定を小さな関数へ分離する。`sys.frozen` が真なら `sys.executable`、それ以外は `__file__` を基準にする。

**Tech Stack:** Python 3.13, pathlib, unittest, uv, PyInstaller

## Global Constraints

- `Config.json` は外部ファイルとしてユーザーが編集可能な状態を維持する。
- PyInstallerの一時展開ディレクトリ `_MEI...` は設定保存先に使わない。
- 既存の設定読み込み処理は変更しない。

---

### Task 1: 実行形態に応じた設定パス解決

**Files:**
- Modify: `src/main.py`
- Test: `tests/test_main.py`

**Interfaces:**
- Consumes: `sys.frozen`, `sys.executable`, module `__file__`
- Produces: `get_config_path() -> Path`, `CONFIG_PATH`

- [ ] **Step 1: Write the failing tests**

通常実行でプロジェクト直下、凍結実行でEXE隣を返すテストを追加する。

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run python -m unittest tests.test_main -v`

Expected: `get_config_path` が未定義のためFAIL。

- [ ] **Step 3: Write minimal implementation**

```python
def get_config_path() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent / "Config.json"
    return Path(__file__).resolve().parent.parent / "Config.json"


CONFIG_PATH = get_config_path()
```

- [ ] **Step 4: Run focused and full tests**

Run: `uv run python -m unittest tests.test_main -v`

Expected: PASS.

Run: `uv run python -m unittest discover -s tests -p "test*.py"`

Expected: all tests PASS.

- [ ] **Step 5: Review the diff**

Run: `git diff -- src/main.py tests/test_main.py`

Expected: 設定パス解決とそのテストだけが変更されている。
