#!/usr/bin/env python3
"""A simple point-and-click front end for fetch_syllabi.py.

Collects the same inputs the command-line script takes (requester username,
course list, output folder) in a window, then runs fetch_syllabi.py as a
subprocess and streams its output into a log pane -- so nobody has to touch
a terminal or hand-edit a courses.txt file.

Usage: double-click run_gui.bat (Windows) or run_gui.command (Mac), or run
`python run_gui.py` directly.
"""

from __future__ import annotations

import queue
import subprocess
import sys
import tempfile
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, scrolledtext, ttk

PROJECT_DIR = Path(__file__).resolve().parent
SCRIPT_PATH = PROJECT_DIR / "fetch_syllabi.py"
DEFAULT_OUTPUT_DIR = Path.home() / "Desktop" / "Syllabi"


def venv_python() -> str:
    """Use the project's own virtual environment's Python if it's been set
    up (via setup.bat/setup.command), falling back to whatever interpreter
    is running this GUI."""
    candidate = (
        PROJECT_DIR / ".venv" / "Scripts" / "python.exe"
        if sys.platform == "win32"
        else PROJECT_DIR / ".venv" / "bin" / "python3"
    )
    return str(candidate) if candidate.exists() else sys.executable


class App:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        root.title("Syllabus Fetcher")
        root.geometry("720x640")
        root.minsize(560, 480)

        self.process: subprocess.Popen | None = None
        self.log_queue: queue.Queue[str] = queue.Queue()
        self.temp_courses_file: Path | None = None

        self._build_widgets()
        self.root.after(100, self._poll_log_queue)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    def _build_widgets(self) -> None:
        pad = {"padx": 8, "pady": 4}

        top = ttk.Frame(self.root)
        top.pack(fill="x", **pad)
        top.columnconfigure(0, weight=1)

        ttk.Label(
            top, text="Fetching for someone else? Their Brightspace username (leave blank for your own courses):"
        ).grid(row=0, column=0, sticky="w")
        self.requester_var = tk.StringVar()
        ttk.Entry(top, textvariable=self.requester_var).grid(row=1, column=0, sticky="we", pady=(0, 8))

        courses_hdr = ttk.Frame(self.root)
        courses_hdr.pack(fill="x", **pad)
        ttk.Label(courses_hdr, text="Course codes (one per line) -- paste a list or type them in:").pack(
            side="left"
        )
        ttk.Button(courses_hdr, text="Load from file...", command=self._load_from_file).pack(side="right")

        self.courses_text = scrolledtext.ScrolledText(self.root, height=10, width=70)
        self.courses_text.pack(fill="both", expand=True, padx=8, pady=(0, 4))

        opts = ttk.Frame(self.root)
        opts.pack(fill="x", **pad)
        self.allow_other_var = tk.BooleanVar()
        self.guess_newest_var = tk.BooleanVar()
        ttk.Checkbutton(
            opts,
            text="If their exact course has no syllabus, allow using another section's (requester mode only)",
            variable=self.allow_other_var,
        ).pack(anchor="w")
        ttk.Checkbutton(
            opts,
            text="When a course name matches more than one of their courses, guess the newest (requester mode only)",
            variable=self.guess_newest_var,
        ).pack(anchor="w")

        out_frm = ttk.Frame(self.root)
        out_frm.pack(fill="x", **pad)
        ttk.Label(out_frm, text="Save syllabi to:").pack(side="left")
        self.output_dir_var = tk.StringVar(value=str(DEFAULT_OUTPUT_DIR))
        ttk.Entry(out_frm, textvariable=self.output_dir_var).pack(side="left", fill="x", expand=True, padx=8)
        ttk.Button(out_frm, text="Browse...", command=self._browse_output_dir).pack(side="left")

        btn_frm = ttk.Frame(self.root)
        btn_frm.pack(fill="x", **pad)
        self.run_button = ttk.Button(btn_frm, text="Run", command=self._on_run)
        self.run_button.pack(side="left")
        self.cancel_button = ttk.Button(btn_frm, text="Cancel", command=self._on_cancel, state="disabled")
        self.cancel_button.pack(side="left", padx=8)

        ttk.Label(self.root, text="Log:").pack(anchor="w", padx=8)
        self.log_text = scrolledtext.ScrolledText(self.root, height=14, width=70, state="disabled")
        self.log_text.pack(fill="both", expand=True, padx=8, pady=(0, 8))

    def _load_from_file(self) -> None:
        path = filedialog.askopenfilename(
            title="Select a course list file",
            filetypes=[("Text files", "*.txt"), ("All files", "*.*")],
        )
        if not path:
            return
        text = Path(path).read_text(errors="replace")
        self.courses_text.delete("1.0", "end")
        self.courses_text.insert("1.0", text)

    def _browse_output_dir(self) -> None:
        path = filedialog.askdirectory(title="Select output folder")
        if path:
            self.output_dir_var.set(path)

    def _append_log(self, text: str) -> None:
        self.log_text.configure(state="normal")
        self.log_text.insert("end", text)
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def _on_run(self) -> None:
        if self.process is not None:
            return
        raw = self.courses_text.get("1.0", "end").strip()
        if not raw:
            messagebox.showerror("No courses", "Enter at least one course code or paste a list.")
            return
        if not SCRIPT_PATH.exists():
            messagebox.showerror("Missing script", f"Can't find {SCRIPT_PATH}")
            return

        tmp = tempfile.NamedTemporaryFile(
            mode="w", suffix=".txt", delete=False, prefix="syllabus_fetcher_courses_"
        )
        tmp.write(raw + "\n")
        tmp.close()
        self.temp_courses_file = Path(tmp.name)

        args = [venv_python(), str(SCRIPT_PATH), "--courses-file", str(self.temp_courses_file)]
        requester = self.requester_var.get().strip()
        if requester:
            args += ["--requester", requester]
            if self.allow_other_var.get():
                args.append("--allow-other-sections")
            if self.guess_newest_var.get():
                args.append("--guess-newest")
        output_dir = self.output_dir_var.get().strip()
        if output_dir:
            args += ["--output-dir", output_dir]

        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.configure(state="disabled")
        self._append_log("Starting -- a Chrome window will open for you to log in...\n\n")
        self.run_button.configure(state="disabled")
        self.cancel_button.configure(state="normal")

        try:
            self.process = subprocess.Popen(
                args,
                cwd=str(PROJECT_DIR),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
            )
        except OSError as exc:
            messagebox.showerror("Couldn't start", str(exc))
            self.run_button.configure(state="normal")
            self.cancel_button.configure(state="disabled")
            return

        threading.Thread(target=self._stream_output, daemon=True).start()

    def _stream_output(self) -> None:
        process = self.process
        assert process is not None and process.stdout is not None
        for line in process.stdout:
            self.log_queue.put(line)
        process.wait()
        self.log_queue.put(f"\n[Finished with exit code {process.returncode}]\n")
        self.log_queue.put("__DONE__")

    def _poll_log_queue(self) -> None:
        try:
            while True:
                line = self.log_queue.get_nowait()
                if line == "__DONE__":
                    self._on_process_done()
                else:
                    self._append_log(line)
        except queue.Empty:
            pass
        self.root.after(100, self._poll_log_queue)

    def _on_process_done(self) -> None:
        self.process = None
        self.run_button.configure(state="normal")
        self.cancel_button.configure(state="disabled")
        if self.temp_courses_file is not None and self.temp_courses_file.exists():
            try:
                self.temp_courses_file.unlink()
            except OSError:
                pass
            self.temp_courses_file = None

    def _on_cancel(self) -> None:
        if self.process is not None:
            self.process.terminate()
            self._append_log("\n[Cancelling...]\n")

    def _on_close(self) -> None:
        if self.process is not None and self.process.poll() is None:
            if not messagebox.askyesno("Quit", "A fetch is still running. Quit anyway?"):
                return
            self.process.terminate()
        self.root.destroy()


def main() -> None:
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
