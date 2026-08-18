import os
import sys
import random
import subprocess
import tkinter as tk
from tkinter import ttk, messagebox

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
TARGET_SCRIPT = os.path.join(SCRIPT_DIR, "generation_wold.py")

MIN_SEED = 0
MAX_SEED = 10_000


class LauncherApp:
    def __init__(self, root):
        self.root = root
        self.root.title("Launcher")
        self.root.geometry("560x300")
        self.root.resizable(False, False)
        self.root.configure(bg="#1e1f26")

        self._process = None

        self._build_ui()

    # ------------------------------------------------------------------
    def _build_ui(self):
        style = ttk.Style()
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass

        style.configure("TFrame", background="#1e1f26")
        style.configure("Title.TLabel", background="#1e1f26", foreground="#f2f2f2",
                         font=("Segoe UI", 16, "bold"))
        style.configure("Sub.TLabel", background="#1e1f26", foreground="#9a9dab",
                         font=("Segoe UI", 10))
        style.configure("TLabel", background="#1e1f26", foreground="#e6e6e6",
                         font=("Segoe UI", 11))
        style.configure("TButton", font=("Segoe UI", 11), padding=8)
        style.configure("Accent.TButton", font=("Segoe UI", 11, "bold"), padding=10)

        container = ttk.Frame(self.root, padding=24)
        container.pack(fill="both", expand=True)

        ttk.Label(container, text="IsoFED - Generation World Launcher", style="Title.TLabel").pack(anchor="w")
        ttk.Label(container, text="Enter the SEED or leave it blank for random. Press (Enter) to start or click (Start world).",
                  style="Sub.TLabel").pack(anchor="w", pady=(2, 18))

        row = ttk.Frame(container)
        row.pack(fill="x")

        ttk.Label(row, text="seed:").pack(side="left")

        self.seed_var = tk.StringVar()
        self.seed_entry = ttk.Entry(row, textvariable=self.seed_var, font=("Segoe UI", 12))
        self.seed_entry.pack(side="left", fill="x", expand=True, padx=(10, 0))
        self.seed_entry.focus_set()
        self.seed_entry.bind("<Return>", lambda e: self._launch())

        btn_row = ttk.Frame(container)
        btn_row.pack(fill="x", pady=(12, 0))

        ttk.Button(btn_row, text="Random SEED", command=self._randomize_seed).pack(
            side="left")

        self.status_var = tk.StringVar(value="")
        ttk.Label(container, textvariable=self.status_var, style="Sub.TLabel").pack(
            anchor="w", pady=(14, 0))

        launch_row = ttk.Frame(container)
        launch_row.pack(fill="x", pady=(18, 0))

        self.launch_btn = ttk.Button(launch_row, text="Start world", style="Accent.TButton",
                                      command=self._launch)
        self.launch_btn.pack(fill="x")

    # ------------------------------------------------------------------
    def _randomize_seed(self):
        self.seed_var.set(str(random.randint(MIN_SEED, MAX_SEED)))

    def _launch(self):
        seed_text = self.seed_var.get().strip()

        if seed_text and not self._is_valid_seed(seed_text):
            messagebox.showerror(
                "Incorrect SEED",
                "The seed must be an integer. Leave the field empty for a random seed."
            )
            return

        if not os.path.isfile(TARGET_SCRIPT):
            messagebox.showerror(
                "File not found",
                f"File not found {TARGET_SCRIPT}.\n"
                "Make sure that launcher.py is in the same folder as generation_wold.py."
            )
            return

        cmd = [sys.executable, TARGET_SCRIPT]
        if seed_text:
            cmd += ["--seed", seed_text]

        self.status_var.set("Launching the world...")
        self.launch_btn.state(["disabled"])
        self.root.update_idletasks()

        try:
            self._process = subprocess.Popen(cmd, cwd=SCRIPT_DIR)
        except Exception as e:
            messagebox.showerror("Error", f"Failed to launch the world:\n{e}")
            self.launch_btn.state(["!disabled"])
            self.status_var.set("")
            return

        self.root.after(500, self._poll_process)

    def _poll_process(self):
        if self._process is None:
            return
        if self._process.poll() is None:
            self.root.after(500, self._poll_process)
        else:
            self.launch_btn.state(["!disabled"])
            self.status_var.set("Window closed")
            self._process = None

    @staticmethod
    def _is_valid_seed(text):
        try:
            int(text)
            return True
        except ValueError:
            return False


def main():
    root = tk.Tk()
    LauncherApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
