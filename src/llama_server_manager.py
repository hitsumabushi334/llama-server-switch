from __future__ import annotations

import csv
import ctypes
import json
import ntpath
import re
import shutil
import subprocess
import sys
import time
from contextlib import contextmanager, suppress
import threading
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from ctypes import wintypes
from pathlib import Path
from typing import Protocol

EXECUTABLE_ALIAS = "llama-server"
SERVER_MUTEX_NAME = r"Global\llama-server-switch"
MUTEX_SECURITY_SDDL = "D:(A;;0x001F0001;;;WD)"
SERVER_STOP_POLL_INTERVAL_SECONDS = 0.1
SERVER_STOP_MAX_POLLS = 5


def _is_windows_absolute_path(path: str) -> bool:
    drive, _ = ntpath.splitdrive(path)
    if not ntpath.isabs(path):
        return False
    if (
        len(drive) == 2
        and drive[0].isascii()
        and drive[0].isalpha()
        and drive[1] == ":"
    ):
        return True
    if not drive.startswith(("\\\\", "//")):
        return False
    server, *shares = drive[2:].replace("/", "\\").split("\\")
    return server not in ("", ".", "?") and len(shares) == 1 and bool(shares[0])


class _DllDirectoryRestoreError(OSError):
    pass


class SecurityAttributes(ctypes.Structure):
    _fields_ = [
        ("nLength", wintypes.DWORD),
        ("lpSecurityDescriptor", wintypes.LPVOID),
        ("bInheritHandle", wintypes.BOOL),
    ]


@dataclass(frozen=True)
class ModelConfig:
    name: str
    arguments: tuple[str, ...]
    llama_cpp: str | None = None

    @property
    def executable(self) -> str:
        return self.llama_cpp or EXECUTABLE_ALIAS


@dataclass(frozen=True)
class ServerConfig:
    models: tuple[ModelConfig, ...]

    @classmethod
    def load(cls, path: Path) -> "ServerConfig":
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError as error:
            raise ValueError(f"設定ファイルが見つかりません: {path}") from error
        except json.JSONDecodeError as error:
            raise ValueError(f"Config.json の形式が不正です: {error.msg}") from error

        if not isinstance(data, dict) or not isinstance(data.get("models"), list):
            raise ValueError('Config.json は "models" 配列を含む必要があります。')

        models: list[ModelConfig] = []
        names: set[str] = set()
        for index, entry in enumerate(data["models"], start=1):
            if not isinstance(entry, dict):
                raise ValueError(f"models[{index}] はオブジェクトである必要があります。")
            name = entry.get("name")
            arguments = entry.get("arguments")
            llama_cpp = entry.get("llama_cpp")
            if not isinstance(name, str) or not name.strip():
                raise ValueError(f"models[{index}].name は空でない文字列である必要があります。")
            if not isinstance(arguments, list) or not all(
                isinstance(argument, str) and argument for argument in arguments
            ):
                raise ValueError(f"models[{index}].arguments は文字列配列である必要があります。")

            if "llama_cpp" in entry and (
                not isinstance(llama_cpp, str)
                or not llama_cpp.strip()
                or not _is_windows_absolute_path(llama_cpp)
            ):
                raise ValueError(
                    f"models[{index}].llama_cpp は空でない絶対パス文字列である必要があります。"
                )

            normalized_name = name.casefold()
            if normalized_name in names:
                raise ValueError(f"モデル名が重複しています: {name}")
            names.add(normalized_name)
            models.append(
                ModelConfig(
                    name=name,
                    arguments=tuple(arguments),
                    llama_cpp=llama_cpp,
                )
            )

        return cls(models=tuple(models))


class RunningProcess(Protocol):
    stdout: Iterable[str]

    def wait(self) -> int: ...


class ServerRuntime(Protocol):
    def find_server_pids(self) -> list[int]: ...

    def terminate_tree(self, pid: int) -> None: ...

    def start(self, command: list[str]) -> RunningProcess: ...

    def exclusive_server_operation(self): ...


class WindowsServerRuntime:
    """Windows 標準コマンドで llama-server.exe を管理する実行環境。"""

    @contextmanager
    def exclusive_server_operation(self):
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        create_mutex = kernel32.CreateMutexW
        create_mutex.argtypes = [ctypes.POINTER(SecurityAttributes), wintypes.BOOL, wintypes.LPCWSTR]
        create_mutex.restype = wintypes.HANDLE
        wait_for_mutex = kernel32.WaitForSingleObject
        wait_for_mutex.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        wait_for_mutex.restype = wintypes.DWORD

        advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
        convert_descriptor = advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW
        convert_descriptor.argtypes = [
            wintypes.LPCWSTR,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.LPVOID),
            ctypes.POINTER(wintypes.DWORD),
        ]
        convert_descriptor.restype = wintypes.BOOL

        descriptor = wintypes.LPVOID()
        descriptor_size = wintypes.DWORD()
        if not convert_descriptor(
            MUTEX_SECURITY_SDDL,
            1,
            ctypes.byref(descriptor),
            ctypes.byref(descriptor_size),
        ):
            raise OSError(ctypes.get_last_error(), "llama-server 排他ロックの ACL を作成できません。")

        security_attributes = SecurityAttributes(
            ctypes.sizeof(SecurityAttributes),
            descriptor,
            False,
        )
        mutex = create_mutex(ctypes.byref(security_attributes), False, SERVER_MUTEX_NAME)
        create_error = ctypes.get_last_error()
        kernel32.LocalFree(descriptor)
        if not mutex:
            raise OSError(create_error, "llama-server の排他ロックを作成できません。")

        acquired = False
        try:
            wait_result = wait_for_mutex(mutex, 30_000)
            if wait_result not in (0, 0x80):
                raise RuntimeError("別の管理ツールが llama-server を切り替え中です。")
            acquired = True
            yield
        finally:
            if acquired:
                kernel32.ReleaseMutex(mutex)
            kernel32.CloseHandle(mutex)

    def find_server_pids(self) -> list[int]:
        result = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq llama-server.exe", "/FO", "CSV", "/NH"],
            capture_output=True,
            check=False,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=15,
        )
        if result.returncode != 0:
            details = result.stderr.strip()
            raise RuntimeError(f"tasklist に失敗しました: {details or result.returncode}")
        pids: list[int] = []
        for row in csv.reader(result.stdout.splitlines()):
            if len(row) < 2 or row[0].casefold() != "llama-server.exe":
                continue
            try:
                pids.append(int(row[1]))
            except ValueError:
                continue
        return pids

    def terminate_tree(self, pid: int) -> None:
        subprocess.run(
            ["taskkill", "/PID", str(pid), "/T", "/F"],
            capture_output=True,
            check=False,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=15,
        )

    @contextmanager
    def _without_frozen_dll_directory(self):
        # PyInstaller の frozen 実行では sys._MEIPASS も非空で設定される前提。
        if sys.platform != "win32" or not getattr(sys, "frozen", False):
            yield
            return

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        get_dll_directory = kernel32.GetDllDirectoryW
        get_dll_directory.argtypes = [wintypes.DWORD, wintypes.LPWSTR]
        get_dll_directory.restype = wintypes.DWORD
        set_dll_directory = kernel32.SetDllDirectoryW
        set_dll_directory.argtypes = [wintypes.LPCWSTR]
        set_dll_directory.restype = wintypes.BOOL

        ctypes.set_last_error(0)
        required_size = get_dll_directory(0, None)
        if required_size == 0:
            error = ctypes.get_last_error()
            if error:
                raise OSError(error, "DLL 検索ディレクトリを取得できません。")
            previous_directory = None
        else:
            buffer = ctypes.create_unicode_buffer(required_size)
            ctypes.set_last_error(0)
            copied_size = get_dll_directory(required_size, buffer)
            if copied_size == 0:
                error = ctypes.get_last_error()
                if error:
                    raise OSError(error, "DLL 検索ディレクトリを取得できません。")
            if copied_size >= required_size:
                raise OSError("DLL 検索ディレクトリの取得中にサイズが変化しました。")
            previous_directory = buffer.value

        if not set_dll_directory(None):
            raise OSError(
                ctypes.get_last_error(), "DLL 検索ディレクトリを解除できません。"
            )
        try:
            yield
        finally:
            if not set_dll_directory(previous_directory):
                raise _DllDirectoryRestoreError(
                    ctypes.get_last_error(), "DLL 検索ディレクトリを復元できません。"
                )

    def start(self, command: list[str]) -> RunningProcess:
        executable = command[0]
        if shutil.which(executable) is None:
            if executable == EXECUTABLE_ALIAS:
                raise RuntimeError(
                    '"llama-server" が PATH 上に見つかりません。Vulkan ビルドの '
                    "llama-server.exe を PATH に追加してください。"
                )
            raise RuntimeError(
                f"指定された llama.cpp 実行ファイルが見つかりません: {executable}"
            )
        process = None
        try:
            with self._without_frozen_dll_directory():
                process = subprocess.Popen(
                    command,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    bufsize=1,
                )
        except _DllDirectoryRestoreError:
            if process is not None:
                with suppress(OSError):
                    process.terminate()
                with suppress(OSError):
                    process.wait()
            raise
        return process


_TOKENS_PER_SECOND = re.compile(
    r"\(\s*[^,]*,\s*([0-9]+(?:\.[0-9]+)?)\s+tokens per second\s*\)",
    re.IGNORECASE,
)
_PROMPT_PROCESSING_SPEED = re.compile(
    r"\bprompt processing\b.*?/\s*([0-9]+(?:\.[0-9]+)?)\s+tokens per second\b",
    re.IGNORECASE,
)
_TOKEN_GENERATION_SPEED = re.compile(
    r"\btg\s*=\s*([0-9]+(?:\.[0-9]+)?)\s*t/s\b",
    re.IGNORECASE,
)


def extract_metrics(line: str) -> dict[str, float]:
    """llama.cpp の各 timing ログ行から PP/Tg の最新 tok/s を取り出す。"""
    metrics: dict[str, float] = {}

    prompt_processing = _PROMPT_PROCESSING_SPEED.search(line)
    if prompt_processing is not None:
        metrics["pp"] = float(prompt_processing.group(1))
    elif "prompt eval time" in line.casefold():
        match = _TOKENS_PER_SECOND.search(line)
        if match is not None:
            metrics["pp"] = float(match.group(1))

    token_generation = _TOKEN_GENERATION_SPEED.search(line)
    if token_generation is not None:
        metrics["tg"] = float(token_generation.group(1))
    elif (
        "prompt eval time" not in line.casefold()
        and re.search(r"\beval time\s*=", line, re.IGNORECASE)
    ):
        match = _TOKENS_PER_SECOND.search(line)
        if match is not None:
            metrics["tg"] = float(match.group(1))

    return metrics


class LlamaServerController:
    def __init__(
        self,
        runtime: ServerRuntime,
        on_log: Callable[[str], None],
        on_metrics: Callable[[dict[str, float]], None] | None = None,
        on_started: Callable[[ModelConfig], None] | None = None,
        on_exit: Callable[[int], None] | None = None,
    ) -> None:
        self._runtime = runtime
        self._on_log = on_log
        self._on_metrics = on_metrics or (lambda _metrics: None)
        self._on_started = on_started or (lambda _model: None)
        self._on_exit = on_exit or (lambda _exit_code: None)
        self._lock = threading.RLock()
        self._process: RunningProcess | None = None
        self._generation = 0

    def start(self, model: ModelConfig) -> None:
        with self._lock, self._runtime.exclusive_server_operation():
            self._stop_existing_servers()
            self._generation += 1
            generation = self._generation
            command = [model.executable, *model.arguments]
            process = self._runtime.start(command)
            self._process = process
            self._on_started(model)
            threading.Thread(
                target=self._stream_logs,
                args=(process, generation),
                name="llama-server-log-reader",
                daemon=True,
            ).start()

    def stop(self) -> None:
        with self._lock, self._runtime.exclusive_server_operation():
            self._stop_existing_servers()
            self._generation += 1

    def _stop_existing_servers(self) -> None:
        server_pids = self._runtime.find_server_pids()
        for pid in server_pids:
            self._runtime.terminate_tree(pid)
        remaining_pids: list[int] = []
        if server_pids:
            remaining_pids = server_pids
            for _ in range(SERVER_STOP_MAX_POLLS):
                time.sleep(SERVER_STOP_POLL_INTERVAL_SECONDS)
                remaining_pids = self._runtime.find_server_pids()
                if not remaining_pids:
                    break
        if remaining_pids:
            pids = ", ".join(map(str, remaining_pids))
            raise RuntimeError(f"llama-server を停止できませんでした (PID: {pids})。")
        self._process = None

    def _stream_logs(self, process: RunningProcess, generation: int) -> None:
        try:
            for raw_line in process.stdout:
                line = raw_line.rstrip("\r\n")
                if not line:
                    continue
                self._on_log(line)
                metrics = extract_metrics(line)
                if metrics:
                    with self._lock:
                        is_current_process = (
                            process is self._process and generation == self._generation
                        )
                        if is_current_process:
                            self._on_metrics(metrics)
        finally:
            exit_code = process.wait()
            with self._lock:
                is_current_process = (
                    process is self._process and generation == self._generation
                )
                if is_current_process:
                    self._process = None
                    self._on_exit(exit_code)
