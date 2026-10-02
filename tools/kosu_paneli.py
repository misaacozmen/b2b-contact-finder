"""Desktop panel that starts and watches fair runs without the terminal (Talimat 21)."""

from __future__ import annotations

import os
from pathlib import Path
import sys
import tkinter as tk
from tkinter import filedialog, messagebox, simpledialog, ttk
from tkinter.scrolledtext import ScrolledText

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from modules import run_launcher  # noqa: E402


POLL_MS = 2000
SECTION_FONT = ("Segoe UI", 10, "bold")
SUCCESS_STATUS = "TAMAMLANDI"


def _duration(seconds: float | None) -> str:
    if seconds is None:
        return "hesaplanıyor"
    minutes = int(round(seconds / 60))
    if minutes < 1:
        return "1 dk'dan az"
    hours, minutes = divmod(minutes, 60)
    return f"{hours} sa {minutes} dk" if hours else f"{minutes} dk"


class Panel:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.session: run_launcher.RunSession | None = None
        self.input_info: dict = {}
        self.finished_dir: Path | None = None
        self.last_stage = 0
        root.title("Fuar koşu paneli")
        root.geometry("680x620")
        root.minsize(600, 560)
        root.protocol("WM_DELETE_WINDOW", self.on_close)
        frame = ttk.Frame(root, padding=14)
        frame.pack(fill="both", expand=True)

        ttk.Label(frame, text="1. Fuar listesi (Excel)", font=SECTION_FONT).pack(anchor="w")
        row = ttk.Frame(frame)
        row.pack(fill="x", pady=(4, 2))
        self.input_var = tk.StringVar()
        ttk.Entry(row, textvariable=self.input_var, state="readonly").pack(side="left", fill="x", expand=True)
        self.choose_button = ttk.Button(row, text="Seç…", command=self.choose_input)
        self.choose_button.pack(side="left", padx=(8, 0))
        self.input_label = ttk.Label(frame, text="Bir .xlsx dosyası seçin. İlk satırda \"company\" başlığı olmalı.")
        self.input_label.pack(anchor="w", pady=(0, 10))

        ttk.Label(frame, text="2. Koşu türü", font=SECTION_FONT).pack(anchor="w")
        self.mode_var = tk.StringVar(value=run_launcher.MODE_FREE)
        for mode, label in run_launcher.MODE_LABELS.items():
            ttk.Radiobutton(frame, text=label, value=mode, variable=self.mode_var, command=self.refresh_limits).pack(anchor="w", pady=1)
        self.limits_label = ttk.Label(frame, text="", wraplength=620, justify="left")
        self.limits_label.pack(anchor="w", pady=(2, 10))

        buttons = ttk.Frame(frame)
        buttons.pack(fill="x", pady=(0, 10))
        self.start_button = ttk.Button(buttons, text="Koşuyu başlat", command=self.start_run)
        self.start_button.pack(side="left")
        self.stop_button = ttk.Button(buttons, text="Durdur", command=self.stop_run, state="disabled")
        self.stop_button.pack(side="left", padx=(8, 0))

        self.stage_label = ttk.Label(frame, text="Koşu başlamadı.")
        self.stage_label.pack(anchor="w")
        self.progress = ttk.Progressbar(frame, mode="determinate", maximum=100)
        self.progress.pack(fill="x", pady=4)
        self.time_label = ttk.Label(frame, text="")
        self.time_label.pack(anchor="w")
        self.events = ScrolledText(frame, height=9, wrap="word", state="disabled", font=("Consolas", 9))
        self.events.pack(fill="both", expand=True, pady=(8, 10))

        results = ttk.Frame(frame)
        results.pack(fill="x")
        self.open_results_button = ttk.Button(results, text="Sonuçları aç", command=self.open_results, state="disabled")
        self.open_results_button.pack(side="left")
        self.open_folder_button = ttk.Button(results, text="Klasörü aç", command=self.open_folder, state="disabled")
        self.open_folder_button.pack(side="left", padx=(8, 0))
        self.archive_button = ttk.Button(results, text="Arşive kopyala", command=self.archive, state="disabled")
        self.archive_button.pack(side="left", padx=(8, 0))
        self.refresh_limits()

    def log(self, message: str) -> None:
        self.events.configure(state="normal")
        self.events.insert("end", message + "\n")
        self.events.see("end")
        self.events.configure(state="disabled")

    def choose_input(self) -> None:
        initial = PROJECT_ROOT / "input"
        path = filedialog.askopenfilename(
            title="Fuar listesini seçin", initialdir=str(initial if initial.is_dir() else PROJECT_ROOT),
            filetypes=[("Excel", "*.xlsx")],
        )
        if not path:
            return
        self.input_var.set(path)
        self.input_info = run_launcher.inspect_input(Path(path))
        if self.input_info["ok"]:
            parts = [f"{self.input_info['company_count']} firma"]
            parts.append("web sitesi sütunu var" if self.input_info["has_website"] else "web sitesi sütunu yok")
            parts.append("telefon sütunu var" if self.input_info["has_phone"] else "telefon sütunu yok")
            self.input_label.configure(text=" · ".join(parts), foreground="#1e7a34")
        else:
            self.input_label.configure(text=self.input_info["error"], foreground="#b42318")
        self.refresh_limits()

    def refresh_limits(self) -> None:
        mode = self.mode_var.get()
        count = int(self.input_info.get("company_count", 0) or 0)
        if mode == run_launcher.MODE_FREE:
            text = "Ücretli çağrı yapılmaz."
        elif not count:
            text = "Dosya seçilince ücretli çağrı sınırları burada görünür."
        else:
            limits = run_launcher.paid_limits(mode, count)
            parts = [f"{run_launcher.PROVIDER_LABELS[name]} en çok {calls}" for name, calls in limits.items()]
            text = "Çağrı sınırları: " + ", ".join(parts) + "."
            if "brightdata" in limits:
                text += f" Bright Data üst sınırı ~{run_launcher.brightdata_cost_ceiling(limits['brightdata']):.2f} $."
        self.limits_label.configure(text=text)

    def set_running(self, running: bool) -> None:
        state = "disabled" if running else "normal"
        self.start_button.configure(state=state)
        self.choose_button.configure(state=state)
        self.stop_button.configure(state="normal" if running else "disabled")
        if running:
            for button in (self.open_results_button, self.open_folder_button, self.archive_button):
                button.configure(state="disabled")

    def start_run(self) -> None:
        if not self.input_info.get("ok"):
            messagebox.showwarning("Fuar koşu paneli", "Önce geçerli bir fuar listesi seçin.")
            return
        mode = self.mode_var.get()
        count = int(self.input_info["company_count"])
        if run_launcher.pipeline_running():
            messagebox.showerror("Fuar koşu paneli", "Başka bir koşu çalışıyor. Aynı anda tek koşu yapılır.")
            return
        if mode != run_launcher.MODE_FREE and not messagebox.askyesno(
            "Ücretli koşu", run_launcher.paid_confirmation_text(mode, count), icon="warning",
        ):
            return
        self.session = run_launcher.RunSession(mode, Path(self.input_var.get()), count)
        try:
            self.session.start()
        except OSError as exc:
            self.session = None
            messagebox.showerror("Fuar koşu paneli", f"Koşu başlatılamadı: {exc}")
            return
        run_launcher.keep_awake(True)
        self.finished_dir = None
        self.last_stage = 0
        self.progress.configure(value=0)
        self.set_running(True)
        self.log(f"Koşu başladı: {Path(self.input_var.get()).name} · {run_launcher.MODE_LABELS[mode]} · {count} firma")
        self.root.after(POLL_MS, self.poll)

    def poll(self) -> None:
        if self.session is None:
            return
        state = self.session.poll()
        if state["stage"] != self.last_stage and state["phase"]:
            self.last_stage = state["stage"]
            self.log(f"Aşama {state['stage']}/3: {run_launcher.phase_label(state['phase'])}")
        if state["total"]:
            self.stage_label.configure(text=f"Aşama {state['stage']}/3 · {run_launcher.phase_label(state['phase'])} · {state['done']} / {state['total']} firma")
            self.progress.configure(value=100 * state["done"] / state["total"])
        elif state["phase"]:
            self.stage_label.configure(text=f"Aşama {state['stage']}/3 · {run_launcher.phase_label(state['phase'])}")
        else:
            self.stage_label.configure(text="Hazırlanıyor…")
        self.time_label.configure(text=f"Geçen süre {_duration(state['elapsed'])} · bu aşamada kalan {_duration(state['remaining'])}")
        if state["running"]:
            self.root.after(POLL_MS, self.poll)
            return
        self.finish(state)

    def finish(self, state: dict) -> None:
        run_launcher.keep_awake(False)
        self.set_running(False)
        session, self.session = self.session, None
        run_dir = Path(state["run_dir"]) if state["run_dir"] else None
        status = run_launcher.read_run_status(run_dir) if run_dir else {}
        if run_dir and (run_dir / "output" / "sonuclar.xlsx").is_file():
            self.finished_dir = run_dir
            for button in (self.open_results_button, self.open_folder_button, self.archive_button):
                button.configure(state="normal")
        run_status = str(status.get("run_status") or "")
        if state["exit_code"] == 0 and run_status == SUCCESS_STATUS:
            summary = run_launcher.summarize_results(run_dir)
            self.progress.configure(value=100)
            message = (
                f"Koşu tamamlandı: {summary['firms']} firma · web sitesi {summary['website']} · "
                f"e-posta {summary['email']} · telefon {summary['phone']}"
            )
            self.log(message)
            messagebox.showinfo("Fuar koşu paneli", message)
            return
        detail = run_status or f"çıkış kodu {state['exit_code']}"
        lines = [f"Koşu tamamlanmadı ({detail})."]
        if self.finished_dir:
            lines.append("Sonuçlar o ana kadar bulunanlarla yazıldı.")
        lines.append("Koşuyu yeniden başlatmayın; kaldığı yerden devam için mimara haber verin.")
        if run_dir:
            lines.append(f"Koşu klasörü: {run_dir}")
        if session is not None and session.console_path:
            lines.append(f"Konsol kaydı: {session.console_path}")
            tail = run_launcher.tail_text(session.console_path, lines=4)
            if tail:
                self.log(tail)
        self.log(" ".join(lines))
        messagebox.showwarning("Fuar koşu paneli", "\n".join(lines))

    def stop_run(self) -> None:
        if self.session is None:
            return
        if messagebox.askyesno(
            "Koşuyu durdur",
            "Koşu durdurulacak. O ana kadar bulunanlar kayıtlı kalır; kaldığı yerden devam için mimara haber verin.\n\nDurdurulsun mu?",
            icon="warning",
        ):
            self.session.stop()
            self.log("Koşu durduruldu.")

    def open_results(self) -> None:
        if self.finished_dir:
            os.startfile(self.finished_dir / "output" / "sonuclar.xlsx")

    def open_folder(self) -> None:
        if self.finished_dir:
            os.startfile(self.finished_dir / "output")

    def archive(self) -> None:
        if not self.finished_dir:
            return
        name = simpledialog.askstring("Arşive kopyala", "Fuar adı (ör. WoodTech):", parent=self.root)
        if not name:
            return
        try:
            target = run_launcher.archive_run(self.finished_dir, Path(self.input_var.get()), name)
        except (ValueError, FileNotFoundError, FileExistsError, OSError) as exc:
            messagebox.showerror("Arşive kopyala", str(exc))
            return
        self.log(f"Arşive kopyalandı: {target}")
        if messagebox.askyesno("Arşive kopyala", f"Kopyalandı:\n{target}\n\nKlasör açılsın mı?"):
            os.startfile(target)

    def on_close(self) -> None:
        if self.session is not None:
            if not messagebox.askyesno(
                "Fuar koşu paneli",
                "Koşu sürüyor. Pencere kapatılırsa koşu durdurulur.\n\nKapatılsın mı?",
                icon="warning",
            ):
                return
            self.session.stop()
            run_launcher.keep_awake(False)
        self.root.destroy()


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    root = tk.Tk()
    Panel(root)
    if "--self-check" in args:
        root.update()
        root.destroy()
        print("PANEL_OK")
        return 0
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
