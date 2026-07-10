from __future__ import annotations

import queue
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, scrolledtext, ttk

from llama_server_manager import (
    LlamaServerController,
    ModelConfig,
    ServerConfig,
    WindowsServerRuntime,
)

CONFIG_PATH = Path(__file__).resolve().parents[1] / "Config.json"
EVENT_POLL_INTERVAL_MS = 25


def format_metric_summary(metrics: dict[str, float]) -> str:
    tg = metrics.get("tg")
    pp = metrics.get("pp")
    tg_text = "--" if tg is None else f"{tg:.2f}"
    pp_text = "--" if pp is None else f"{pp:.2f}"
    return f"Tg: {tg_text} tok/s | PP: {pp_text} tok/s"


class LlamaServerApp(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("llama-server Switch")
        self.minsize(720, 440)

        self._events: queue.Queue[tuple[str, object]] = queue.Queue()
        self._operations: queue.Queue[tuple[int, str, ModelConfig | None]] = queue.Queue()
        self._models: dict[str, ModelConfig] = {}
        self._metrics: dict[str, float] = {}
        self._stop_requested_at: float | None = None
        self._next_operation_id = 0
        self._stop_request_id = 0
        self._executing_operation_id = 0
        self._status = tk.StringVar(value="Config.json を読み込んでいます。")
        self._metrics_text = tk.StringVar(value=format_metric_summary(self._metrics))
        self._selected_model = tk.StringVar()
        self._controller = LlamaServerController(
            runtime=WindowsServerRuntime(),
            on_log=lambda line: self._events.put(("log", line)),
            on_metrics=lambda metrics: self._events.put(("metrics", metrics)),
            on_started=lambda model: self._events.put(
                ("operation_complete", (self._executing_operation_id, "start", model))
            ),
            on_exit=lambda exit_code: self._events.put(("server_exit", (time.monotonic(), exit_code))),
        )
        threading.Thread(
            target=self._operation_worker,
            name="llama-server-operation-worker",
            daemon=True,
        ).start()

        self._build_widgets()
        self._reload_config()
        self.after(EVENT_POLL_INTERVAL_MS, self._drain_events)

    def _build_widgets(self) -> None:
        container = ttk.Frame(self, padding=12)
        container.grid(sticky="nsew")
        self.columnconfigure(0, weight=1)
        self.rowconfigure(0, weight=1)
        container.columnconfigure(1, weight=1)
        container.rowconfigure(2, weight=1)

        ttk.Label(container, text="モデル").grid(row=0, column=0, sticky="w")
        self._model_selector = ttk.Combobox(
            container,
            textvariable=self._selected_model,
            state="readonly",
        )
        self._model_selector.grid(row=0, column=1, sticky="ew", padx=(8, 8))
        self._start_button = ttk.Button(container, text="選択モデルを起動", command=self._start)
        self._start_button.grid(row=0, column=2, sticky="ew")
        self._stop_button = ttk.Button(container, text="サーバーを停止", command=self._stop)
        self._stop_button.grid(row=0, column=3, sticky="ew", padx=(8, 0))

        ttk.Button(container, text="Config.json を再読込", command=self._reload_config).grid(
            row=1, column=0, columnspan=2, sticky="w", pady=(10, 10)
        )
        ttk.Label(container, textvariable=self._status).grid(
            row=1, column=2, columnspan=2, sticky="e", pady=(10, 10)
        )

        notebook = ttk.Notebook(container)
        notebook.grid(row=2, column=0, columnspan=4, sticky="nsew")

        status_tab = ttk.Frame(notebook, padding=18)
        status_tab.columnconfigure(0, weight=1)
        notebook.add(status_tab, text="状態")
        ttk.Label(status_tab, text="直近の推論速度", font=("Segoe UI", 12, "bold")).grid(
            row=0, column=0, sticky="w"
        )
        ttk.Label(status_tab, textvariable=self._metrics_text, font=("Consolas", 20)).grid(
            row=1, column=0, sticky="w", pady=(12, 0)
        )
        ttk.Label(
            status_tab,
            text="PP は prompt processing、Tg は token generation の tokens per second です。",
        ).grid(row=2, column=0, sticky="w", pady=(10, 0))

        log_tab = ttk.Frame(notebook, padding=8)
        log_tab.rowconfigure(0, weight=1)
        log_tab.columnconfigure(0, weight=1)
        notebook.add(log_tab, text="すべてのログ")
        self._log = scrolledtext.ScrolledText(log_tab, wrap=tk.WORD, state=tk.DISABLED)
        self._log.grid(row=0, column=0, sticky="nsew")

    def _reload_config(self) -> None:
        try:
            config = ServerConfig.load(CONFIG_PATH)
        except ValueError as error:
            self._models = {}
            self._model_selector["values"] = ()
            self._start_button.state(["disabled"])
            self._status.set("Config.json の読み込みに失敗しました。")
            messagebox.showerror("Config.json エラー", str(error), parent=self)
            return

        selected = self._selected_model.get()
        self._models = {model.name: model for model in config.models}
        names = tuple(self._models)
        self._model_selector["values"] = names
        if names:
            self._start_button.state(["!disabled"])
            self._selected_model.set(selected if selected in self._models else names[0])
            self._status.set(f"{len(names)} モデルを読み込みました。")
        else:
            self._selected_model.set("")
            self._start_button.state(["disabled"])
            self._status.set("Config.json にモデルがありません。")

    def _start(self) -> None:
        model = self._models.get(self._selected_model.get())
        if model is None:
            messagebox.showwarning("モデル未選択", "起動するモデルを選択してください。", parent=self)
            return

        self._status.set(f"起動中: {model.name}")
        self._run_operation("start", model)

    def _stop(self) -> None:
        self._stop_requested_at = time.monotonic()
        self._status.set("サーバーを停止中です。")
        self._run_operation("stop", None)

    def _run_operation(self, operation: str, model: ModelConfig | None) -> int:
        self._next_operation_id += 1
        operation_id = self._next_operation_id
        if operation == "stop":
            self._stop_request_id = operation_id
        self._operations.put((operation_id, operation, model))
        return operation_id

    def _operation_worker(self) -> None:
        while True:
            operation_id, operation, model = self._operations.get()
            try:
                self._executing_operation_id = operation_id
                self._execute_operation(operation_id, operation, model)
            finally:
                self._operations.task_done()

    def _execute_operation(
        self,
        operation_id: int,
        operation: str,
        model: ModelConfig | None,
    ) -> None:
        try:
            if operation == "start":
                if model is None:
                    raise RuntimeError("起動するモデルが選択されていません。")
                self._controller.start(model)
            else:
                self._controller.stop()
                self._events.put(("operation_complete", (operation_id, "stop", None)))
        except Exception as error:
            self._events.put(("operation_error", (operation_id, operation, str(error))))

    def _drain_events(self) -> None:
        for _ in range(200):
            try:
                kind, value = self._events.get_nowait()
            except queue.Empty:
                break

            if kind == "log":
                self._append_log(str(value))
            elif kind == "metrics":
                self._metrics.update(value)  # type: ignore[arg-type]
                self._metrics_text.set(format_metric_summary(self._metrics))
            elif kind == "operation_complete":
                operation_id, operation, model = value  # type: ignore[misc]
                if operation == "start" and isinstance(model, ModelConfig):
                    if operation_id < self._stop_request_id:
                        continue
                    self._stop_requested_at = None
                    self._metrics.clear()
                    self._metrics_text.set(format_metric_summary(self._metrics))
                    self._status.set(f"起動中: {model.name}")
                    self._append_log(f"$ llama-server {' '.join(model.arguments)}")
                elif operation == "stop" and operation_id == self._stop_request_id:
                    self._stop_requested_at = None
                    self._status.set("すべての llama-server を停止しました。")
                    self._append_log("llama-server を停止しました。")
            elif kind == "operation_error":
                operation_id, operation, message = value  # type: ignore[misc]
                if operation == "start" and operation_id < self._stop_request_id:
                    continue
                is_start = operation == "start"
                if not is_start and operation_id == self._stop_request_id:
                    self._stop_requested_at = None
                self._status.set(
                    "サーバーを起動できませんでした。"
                    if is_start
                    else "サーバーを停止できませんでした。"
                )
                self._append_log(message)
                messagebox.showerror("起動エラー" if is_start else "停止エラー", message, parent=self)
            elif kind == "server_exit":
                exit_at, exit_code = value  # type: ignore[misc]
                if (
                    self._stop_requested_at is not None
                    and float(exit_at) >= self._stop_requested_at
                ):
                    continue
                exit_code = int(exit_code)
                if exit_code == 0:
                    self._status.set("llama-server は終了しました。")
                else:
                    self._status.set(f"llama-server が異常終了しました (終了コード: {exit_code})。")
                self._append_log(f"llama-server は終了しました (終了コード: {exit_code})。")
        self.after(EVENT_POLL_INTERVAL_MS, self._drain_events)

    def _append_log(self, line: str) -> None:
        self._log.configure(state=tk.NORMAL)
        self._log.insert(tk.END, f"{line}\n")
        self._log.see(tk.END)
        self._log.configure(state=tk.DISABLED)


def main() -> None:
    app = LlamaServerApp()
    app.mainloop()


if __name__ == "__main__":
    main()
