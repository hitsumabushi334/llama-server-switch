from __future__ import annotations

import json
import subprocess

import queue
import sys
import tempfile
import unittest
from contextlib import contextmanager
from unittest.mock import MagicMock, patch
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from llama_server_manager import (
    LlamaServerController,
    ModelConfig,
    WindowsServerRuntime,
    SERVER_MUTEX_NAME,
    MUTEX_SECURITY_SDDL,
    ServerConfig,
    extract_metrics,
)
from main import LlamaServerApp, format_metric_summary


class FakeRuntime:
    def __init__(self, pids: list[int] | None = None) -> None:
        self.pids = list(pids or [])
        self.terminated: list[int] = []
        self.commands: list[list[str]] = []
        self.operation_entries = 0
        self.operation_exits = 0
        self.process = FakeProcess([])

    def find_server_pids(self) -> list[int]:
        return list(self.pids)

    def terminate_tree(self, pid: int) -> None:
        self.terminated.append(pid)
        self.pids.remove(pid)

    def start(self, command: list[str]) -> "FakeProcess":
        self.commands.append(command)
        return self.process

    @contextmanager
    def exclusive_server_operation(self):
        self.operation_entries += 1
        try:
            yield
        finally:
            self.operation_exits += 1


class FakeProcess:
    def __init__(self, lines: list[str], exit_code: int = 0) -> None:
        self.stdout = lines
        self._exit_code = exit_code
        self.terminated = False
        self.wait_calls = 0

    def terminate(self) -> None:
        self.terminated = True

    def wait(self) -> int:
        self.wait_calls += 1
        return self._exit_code


class ServerConfigTests(unittest.TestCase):
    def test_loads_model_name_and_arguments_from_config_json(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "Config.json"
            config_path.write_text(
                '{"models": [{"name": "Qwen", "arguments": ["--model", "Qwen.gguf"]}]}',
                encoding="utf-8",
            )

            config = ServerConfig.load(config_path)

        self.assertEqual(
            config.models,
            (
                ModelConfig(
                    name="Qwen",
                    arguments=("--model", "Qwen.gguf"),
                    llama_cpp=None,
                ),
            ),
        )
        self.assertEqual(config.models[0].executable, "llama-server")

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
        self.assertEqual(config.models[0].executable, r"D:\\AI\\llama.cpp\\bin\\llama-server.exe")

    def test_loads_model_specific_llama_cpp_unc_path(self) -> None:
        llama_cpp = r"\\server\share\llama-server.exe"
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "Config.json"
            config_path.write_text(
                json.dumps(
                    {"models": [{"name": "Qwen", "llama_cpp": llama_cpp, "arguments": []}]}
                ),
                encoding="utf-8",
            )

            config = ServerConfig.load(config_path)

        self.assertEqual(config.models[0].llama_cpp, llama_cpp)
        self.assertEqual(config.models[0].executable, llama_cpp)

    def test_rejects_invalid_llama_cpp_values(self) -> None:
        invalid_values = (
            "",
            r"bin\\llama-server.exe",
            123,
            None,
            r"\\server",
            "\\\\server\\",
            r"\\.\C:\llama-server.exe",
            r"1:\llama-server.exe",
            r"?:\llama-server.exe",
            r" :\llama-server.exe",
        )
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

                with self.assertRaisesRegex(
                    ValueError,
                    r"llama_cpp は空でない絶対パス文字列である必要があります。",
                ):
                    ServerConfig.load(config_path)

    def test_rejects_duplicate_model_names(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "Config.json"
            config_path.write_text(
                '{"models": [{"name": "Qwen", "arguments": []}, {"name": "Qwen", "arguments": []}]}',
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "重複"):
                ServerConfig.load(config_path)


class MetricExtractionTests(unittest.TestCase):
    def test_extracts_prompt_processing_and_token_generation_speeds(self) -> None:
        prompt_line = (
            "llama_perf_context_print: prompt eval time = 1000.00 ms / 20 tokens "
            "(50.00 ms per token, 20.00 tokens per second)"
        )
        generation_line = (
            "llama_perf_context_print: eval time = 2000.00 ms / 50 runs "
            "(40.00 ms per token, 25.00 tokens per second)"
        )

        self.assertEqual(extract_metrics(prompt_line), {"pp": 20.0})
        self.assertEqual(extract_metrics(generation_line), {"tg": 25.0})

    def test_extracts_pp_and_tg_from_current_slot_timing_logs(self) -> None:
        prompt_log = (
            "1.30.169.827 I slot print_timing: id  0 | task 0 | "
            "prompt processing, n_tokens =  16384, progress = 0.66, "
            "t =  35.64 s / 459.68 tokens per second"
        )
        generation_log = (
            "4.38.994.944 I slot print_timing: id  0 | task 544 | "
            "n_decoded =    206, tg =  35.33 t/s"
        )

        self.assertEqual(extract_metrics(prompt_log), {"pp": 459.68})
        self.assertEqual(extract_metrics(generation_log), {"tg": 35.33})



class MetricSummaryTests(unittest.TestCase):
    def test_formats_missing_and_observed_speeds_for_management_ui(self) -> None:
        self.assertEqual(format_metric_summary({}), "Tg: -- tok/s | PP: -- tok/s")
        self.assertEqual(
            format_metric_summary({"tg": 25.0, "pp": 20.0}),
            "Tg: 25.00 tok/s | PP: 20.00 tok/s",
        )


class OperationQueueTests(unittest.TestCase):
    def test_stop_failure_is_reported_to_the_ui_queue(self) -> None:
        class FailingController:
            def stop(self) -> None:
                raise RuntimeError("llama-server を停止できませんでした (PID: 444)。")

        app = object.__new__(LlamaServerApp)
        app._controller = FailingController()
        app._events = queue.Queue()

        app._execute_operation(0, "stop", None)

        self.assertEqual(
            app._events.get_nowait(),
            ("operation_error", (0, "stop", "llama-server を停止できませんでした (PID: 444)。")),
        )

    def test_operation_requests_are_queued_in_submission_order(self) -> None:
        class WorkingController:
            def start(self, _model: ModelConfig) -> None:
                pass

            def stop(self) -> None:
                pass

        model = ModelConfig(name="Qwen", arguments=("--model", "Qwen.gguf"))
        app = object.__new__(LlamaServerApp)
        app._controller = WorkingController()
        app._events = queue.Queue()
        app._operations = queue.Queue()
        app._next_operation_id = 0
        app._stop_request_id = 0

        app._run_operation("start", model)
        app._run_operation("stop", None)

        self.assertEqual(app._operations.get_nowait(), (1, "start", model))
        self.assertEqual(app._operations.get_nowait(), (2, "stop", None))

    def test_start_completion_discards_prior_model_metric_events(self) -> None:
        class Value:
            def __init__(self) -> None:
                self.value = ""

            def set(self, value: str) -> None:
                self.value = value

        model = ModelConfig(name="B", arguments=("--model", "B.gguf"))
        app = object.__new__(LlamaServerApp)
        app._events = queue.Queue()
        app._events.put(("metrics", {"tg": 25.0, "pp": 20.0}))
        app._events.put(("operation_complete", (1, "start", model)))
        app._stop_request_id = 0
        app._metrics = {}
        app._metrics_text = Value()
        app._status = Value()
        app._append_log = lambda _line: None
        app.after = lambda _delay, _callback: None

        app._drain_events()

        self.assertEqual(app._metrics, {})
        self.assertEqual(app._metrics_text.value, "Tg: -- tok/s | PP: -- tok/s")

    def test_later_stop_request_is_not_cleared_by_an_older_start_completion(self) -> None:
        class Value:
            def __init__(self) -> None:
                self.value = ""

            def set(self, value: str) -> None:
                self.value = value

        model = ModelConfig(name="A", arguments=("--model", "A.gguf"))
        app = object.__new__(LlamaServerApp)
        app._events = queue.Queue()
        app._events.put(("operation_complete", (1, "start", model)))
        app._stop_request_id = 2
        app._stop_requested_at = 2.0
        app._metrics = {}
        app._metrics_text = Value()
        app._status = Value()
        app._append_log = lambda _line: None
        app.after = lambda _delay, _callback: None

        app._drain_events()

        self.assertEqual(app._stop_requested_at, 2.0)
        self.assertEqual(app._status.value, "")

    def test_manual_stop_ignores_a_late_server_exit_event(self) -> None:
        class Value:
            def __init__(self) -> None:
                self.value = ""

            def set(self, value: str) -> None:
                self.value = value

        app = object.__new__(LlamaServerApp)
        app._events = queue.Queue()
        app._status = Value()
        app._run_operation = lambda _operation, _model: None
        logs: list[str] = []
        app.after = lambda _delay, _callback: None

        app._stop()
        app._events.put(("server_exit", (app._stop_requested_at + 1.0, 1)))
        app._drain_events()

        self.assertIsNotNone(app._stop_requested_at)
        self.assertEqual(app._status.value, "サーバーを停止中です。")
        self.assertEqual(logs, [])

    def test_successful_stop_clears_exit_suppression_timestamp(self) -> None:
        class Value:
            def __init__(self) -> None:
                self.value = ""

            def set(self, value: str) -> None:
                self.value = value

        app = object.__new__(LlamaServerApp)
        app._events = queue.Queue()
        app._events.put(("operation_complete", (2, "stop", None)))
        app._stop_request_id = 2
        app._stop_requested_at = 1.0
        app._status = Value()
        app._append_log = lambda _line: None
        app.after = lambda _delay, _callback: None

        app._drain_events()

        self.assertIsNone(app._stop_requested_at)
        self.assertEqual(app._status.value, "すべての llama-server を停止しました。")

    def test_crash_before_manual_stop_is_still_reported(self) -> None:
        class Value:
            def __init__(self) -> None:
                self.value = ""

            def set(self, value: str) -> None:
                self.value = value

        app = object.__new__(LlamaServerApp)
        app._events = queue.Queue()
        app._events.put(("server_exit", (1.0, 1)))
        app._stop_requested_at = 2.0
        app._status = Value()
        logs: list[str] = []
        app._append_log = logs.append
        app.after = lambda _delay, _callback: None

        app._drain_events()

        self.assertEqual(app._status.value, "llama-server が異常終了しました (終了コード: 1)。")
        self.assertEqual(logs, ["llama-server は終了しました (終了コード: 1)。"])
class LlamaServerControllerTests(unittest.TestCase):
    def test_start_terminates_all_existing_servers_before_using_alias(self) -> None:
        runtime = FakeRuntime(pids=[101, 202])
        controller = LlamaServerController(runtime=runtime, on_log=lambda _line: None)
        model = ModelConfig(name="Qwen", arguments=("--model", "Qwen.gguf", "--port", "8080"))

        controller.start(model)

        self.assertEqual(runtime.terminated, [101, 202])
        self.assertEqual(
            runtime.commands,
            [["llama-server", "--model", "Qwen.gguf", "--port", "8080"]],
        )
        self.assertEqual(runtime.operation_entries, 1)
        self.assertEqual(runtime.operation_exits, 1)

    def test_stop_terminates_servers_not_started_by_this_application(self) -> None:
        runtime = FakeRuntime(pids=[333])
        controller = LlamaServerController(runtime=runtime, on_log=lambda _line: None)

        controller.stop()

        self.assertEqual(runtime.terminated, [333])
        self.assertEqual(runtime.operation_entries, 1)
        self.assertEqual(runtime.operation_exits, 1)

    def test_stop_waits_for_terminated_server_to_disappear_from_process_list(self) -> None:
        class DelayedTerminationRuntime(FakeRuntime):
            def __init__(self) -> None:
                super().__init__(pids=[35668])
                self.find_calls = 0

            def find_server_pids(self) -> list[int]:
                self.find_calls += 1
                if self.find_calls < 3:
                    return [35668]
                return []

            def terminate_tree(self, pid: int) -> None:
                self.terminated.append(pid)

        runtime = DelayedTerminationRuntime()
        controller = LlamaServerController(runtime=runtime, on_log=lambda _line: None)

        controller.stop()

        self.assertEqual(runtime.terminated, [35668])
        self.assertEqual(runtime.find_calls, 3)

    def test_start_aborts_when_an_existing_server_cannot_be_terminated(self) -> None:
        class StubbornRuntime(FakeRuntime):
            def terminate_tree(self, pid: int) -> None:
                self.terminated.append(pid)

        runtime = StubbornRuntime(pids=[444])
        controller = LlamaServerController(runtime=runtime, on_log=lambda _line: None)

        with self.assertRaisesRegex(RuntimeError, "停止"):
            controller.start(ModelConfig(name="Qwen", arguments=()))

        self.assertEqual(runtime.commands, [])

    def test_stream_forwards_all_logs_and_current_model_metrics(self) -> None:
        prompt_log = (
            "prompt eval time = 1000.00 ms / 20 tokens "
            "(50.00 ms per token, 20.00 tokens per second)"
        )
        logs: list[str] = []
        metrics: list[dict[str, float]] = []
        process = FakeProcess([prompt_log])
        controller = LlamaServerController(
            runtime=FakeRuntime(),
            on_log=logs.append,
            on_metrics=metrics.append,
        )
        controller._process = process
        controller._generation = 1

        controller._stream_logs(process, generation=1)

        self.assertEqual(logs, [prompt_log])
        self.assertEqual(metrics, [{"pp": 20.0}])

    def test_stream_does_not_publish_metrics_from_a_replaced_process(self) -> None:
        generation_log = (
            "eval time = 2000.00 ms / 50 runs "
            "(40.00 ms per token, 25.00 tokens per second)"
        )
        logs: list[str] = []
        metrics: list[dict[str, float]] = []
        old_process = FakeProcess([generation_log])
        controller = LlamaServerController(
            runtime=FakeRuntime(),
            on_log=logs.append,
            on_metrics=metrics.append,
        )
        controller._process = FakeProcess([])
        controller._generation = 2

        controller._stream_logs(old_process, generation=1)

        self.assertEqual(logs, [generation_log])
        self.assertEqual(metrics, [])

    def test_current_process_exit_is_reported_to_the_ui_callback(self) -> None:
        exit_codes: list[int] = []
        process = FakeProcess([], exit_code=17)
        controller = LlamaServerController(
            runtime=FakeRuntime(),
            on_log=lambda _line: None,
            on_exit=exit_codes.append,
        )
        controller._process = process
        controller._generation = 1

        controller._stream_logs(process, generation=1)

        self.assertEqual(exit_codes, [17])


class WindowsServerRuntimeTests(unittest.TestCase):
    @staticmethod
    def _kernel32(
        events, current_directory=r"C:\bundle\_MEI123", set_results=None
    ):
        kernel32 = MagicMock()
        results = iter(set_results or [True, True])

        def get_dll_directory(size, buffer):
            events.append(("get", None))
            if size == 0:
                return len(current_directory) + 1
            buffer.value = current_directory
            return len(current_directory)

        def set_dll_directory(path):
            events.append(("set", path))
            return next(results)

        kernel32.GetDllDirectoryW.side_effect = get_dll_directory
        kernel32.SetDllDirectoryW.side_effect = set_dll_directory
        return kernel32

    def test_frozen_start_temporarily_clears_and_restores_dll_directory(self) -> None:
        events = []
        kernel32 = self._kernel32(events)
        process = FakeProcess([])

        def popen(*_args, **_kwargs):
            events.append(("popen", None))
            return process

        with (
            patch.object(sys, "platform", "win32"),
            patch.object(sys, "frozen", True, create=True),
            patch("llama_server_manager.shutil.which", return_value="llama-server.exe"),
            patch("llama_server_manager.ctypes.WinDLL", return_value=kernel32),
            patch("llama_server_manager.subprocess.Popen", side_effect=popen),
        ):
            result = WindowsServerRuntime().start(["llama-server"])
        self.assertIs(result, process)
        self.assertEqual(
            events,
            [
                ("get", None),
                ("get", None),
                ("set", None),
                ("popen", None),
                ("set", r"C:\bundle\_MEI123"),
            ],
        )

    def test_frozen_start_restores_dll_directory_when_popen_raises(self) -> None:
        events = []
        kernel32 = self._kernel32(events)

        def popen(*_args, **_kwargs):
            events.append(("popen", None))
            raise OSError("launch failed")

        with (
            patch.object(sys, "platform", "win32"),
            patch.object(sys, "frozen", True, create=True),
            patch("llama_server_manager.shutil.which", return_value="llama-server.exe"),
            patch("llama_server_manager.ctypes.WinDLL", return_value=kernel32),
            patch("llama_server_manager.subprocess.Popen", side_effect=popen),
        ):
            with self.assertRaisesRegex(OSError, "launch failed"):
                WindowsServerRuntime().start(["llama-server"])
        self.assertEqual(events[-2:], [("popen", None), ("set", r"C:\bundle\_MEI123")])

    def test_restore_failure_terminates_and_reaps_started_process(self) -> None:
        events = []
        kernel32 = self._kernel32(events, set_results=[True, False])
        process = FakeProcess([])
        with (
            patch.object(sys, "platform", "win32"),
            patch.object(sys, "frozen", True, create=True),
            patch("llama_server_manager.shutil.which", return_value="llama-server.exe"),
            patch("llama_server_manager.ctypes.WinDLL", return_value=kernel32),
            patch("llama_server_manager.subprocess.Popen", return_value=process),
        ):
            with self.assertRaisesRegex(OSError, "復元"):
                WindowsServerRuntime().start(["llama-server"])

        self.assertTrue(process.terminated)
        self.assertEqual(process.wait_calls, 1)

    def test_restore_failure_keeps_popen_failure_as_exception_context(self) -> None:
        events = []
        kernel32 = self._kernel32(events, set_results=[True, False])
        launch_error = OSError("launch failed")
        with (
            patch.object(sys, "platform", "win32"),
            patch.object(sys, "frozen", True, create=True),
            patch("llama_server_manager.shutil.which", return_value="llama-server.exe"),
            patch("llama_server_manager.ctypes.WinDLL", return_value=kernel32),
            patch("llama_server_manager.subprocess.Popen", side_effect=launch_error),
        ):
            with self.assertRaisesRegex(OSError, "復元") as raised:
                WindowsServerRuntime().start(["llama-server"])

        self.assertIs(raised.exception.__context__, launch_error)

    def test_non_frozen_start_does_not_access_dll_directory(self) -> None:
        process = FakeProcess([])
        with (
            patch.object(sys, "platform", "win32"),
            patch.object(sys, "frozen", False, create=True),
            patch("llama_server_manager.shutil.which", return_value="llama-server.exe"),
            patch("llama_server_manager.ctypes.WinDLL") as win_dll,
            patch("llama_server_manager.subprocess.Popen", return_value=process),
        ):
            result = WindowsServerRuntime().start(["llama-server"])
        self.assertIs(result, process)
        win_dll.assert_not_called()

    def test_refuses_to_report_no_servers_when_tasklist_fails(self) -> None:
        failed_result = subprocess.CompletedProcess(
            args=["tasklist"], returncode=1, stdout="", stderr="access denied"
        )
        with patch("llama_server_manager.subprocess.run", return_value=failed_result):
            with self.assertRaisesRegex(RuntimeError, "tasklist"):
                WindowsServerRuntime().find_server_pids()

    def test_uses_a_global_mutex_name(self) -> None:
        self.assertEqual(SERVER_MUTEX_NAME, r"Global\llama-server-switch")

    def test_grants_all_sessions_access_to_the_global_mutex(self) -> None:
        self.assertEqual(MUTEX_SECURITY_SDDL, "D:(A;;0x001F0001;;;WD)")


if __name__ == "__main__":
    unittest.main()
