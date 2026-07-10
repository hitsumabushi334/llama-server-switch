from __future__ import annotations
import subprocess

import queue
import sys
import tempfile
import unittest
from contextlib import contextmanager
from unittest.mock import patch
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

    def wait(self) -> int:
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
            (ModelConfig(name="Qwen", arguments=("--model", "Qwen.gguf")),),
        )

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

        app._execute_operation("stop", None)

        self.assertEqual(
            app._events.get_nowait(),
            ("operation_error", ("stop", "llama-server を停止できませんでした (PID: 444)。")),
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

        app._run_operation("start", model)
        app._run_operation("stop", None)

        self.assertEqual(app._operations.get_nowait(), ("start", model))
        self.assertEqual(app._operations.get_nowait(), ("stop", None))

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
        app._events.put(("operation_complete", ("start", model)))
        app._metrics = {}
        app._metrics_text = Value()
        app._status = Value()
        app._append_log = lambda _line: None
        app.after = lambda _delay, _callback: None

        app._drain_events()

        self.assertEqual(app._metrics, {})
        self.assertEqual(app._metrics_text.value, "Tg: -- tok/s | PP: -- tok/s")
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
