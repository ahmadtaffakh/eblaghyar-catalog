from __future__ import annotations

import html
import json
import os
import queue
import re
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

from . import __version__
from .exchange import list_user_requests, publish_request, ready_package_path
from .ingest import scan_exchange, scan_historical
from .manifest import ManifestValidationError, copy_validated_archive
from .store import CatalogStore, normalize


SETTINGS_SCHEMA_VERSION = 1
SETTINGS_KEYS = ("source_root", "admin_db", "exchange_dir", "viewer_db")
REQUEST_MONITOR_INTERVAL_MS = 60_000


def settings_path() -> Path:
    local_app_data = os.environ.get("LOCALAPPDATA")
    root = Path(local_app_data) if local_app_data else Path.home() / "AppData" / "Local"
    return root / "EblaghyarCatalog" / "settings.json"


def load_settings(path: str | Path, defaults: dict[str, str]) -> dict[str, str]:
    values = {key: str(defaults.get(key) or "") for key in SETTINGS_KEYS}
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return values
    if not isinstance(data, dict) or data.get("schema_version") != SETTINGS_SCHEMA_VERSION:
        return values
    for key in SETTINGS_KEYS:
        if isinstance(data.get(key), str):
            values[key] = data[key]
    return values


def save_settings(path: str | Path, values: dict[str, str]) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(target.suffix + ".part")
    payload = {
        "schema_version": SETTINGS_SCHEMA_VERSION,
        **{key: str(values.get(key) or "") for key in SETTINGS_KEYS},
    }
    try:
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        os.replace(temporary, target)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def summary_text(result: dict[str, Any]) -> str:
    parts = [
        f"بررسی‌شده: {result.get('scanned', 0)}",
        f"ثبت/به‌روزرسانی: {result.get('indexed', 0)}",
    ]
    if "unchanged" in result:
        parts.append(f"بدون تغییر: {result.get('unchanged', 0)}")
    parts.append(f"دارای فایل ناقص: {result.get('incomplete', 0)}")
    if result.get("stopped"):
        parts.append("متوقف شد")
    if result.get("errors"):
        parts.append(f"خطا: {len(result['errors'])}")
    return " | ".join(parts)


def record_status(item: dict[str, Any]) -> str:
    if item.get("package_available"):
        return "بسته آماده"
    if item.get("source_kind") == "historical":
        if item.get("source_complete"):
            return "مبدأ کامل؛ ZIP آماده نیست"
        return "سابقه؛ فایل ناقص"
    return "بسته در دسترس نیست"


def incomplete_reason_text(reason: str) -> str:
    kind, separator, identifier = str(reason).partition(":")
    labels = {
        "notice_file_missing": "فایل اصل ابلاغیه یافت نشد",
        "attachment_file_missing": "فایل ضمیمه یافت نشد",
        "attachment_record_missing": "رکورد ضمیمه یافت نشد",
        "attachment_owner_mismatch": "مالک ضمیمه با ابلاغیه یکسان نیست",
    }
    label = labels.get(kind, reason)
    return f"{label} ({identifier})" if separator else label


def _display_text(value: Any) -> str:
    text = html.unescape(str(value or ""))
    text = re.sub(r"</?br\s*/?>", "\n", text, flags=re.IGNORECASE)
    return re.sub(r"<[^>]+>", "", text).strip() or "—"


def party_names_text(item: dict[str, Any]) -> str:
    names = [
        str(party.get("name") or "").strip()
        for party in item.get("parties") or []
        if isinstance(party, dict) and str(party.get("name") or "").strip()
    ]
    return "، ".join(dict.fromkeys(names)) or "—"


def table_sort_key(value: Any) -> tuple[int, int | str]:
    text = normalize(value)
    return (0, int(text)) if text.isdigit() else (1, text)


def request_status_text(status: str) -> str:
    return {
        "queued": "در صف",
        "processing": "در حال پردازش",
        "needs_acquisition": "نیازمند واکشی از ثنا",
        "ready": "✓ بسته آماده",
        "failed": "ناموفق",
    }.get(status, status or "نامشخص")


def _ready_request_candidates(
    items: list[dict], notified_request_ids: set[str]
) -> list[dict]:
    return [
        item
        for item in items
        if item.get("status") == "ready"
        and str(item.get("request_id") or "")
        and str(item["request_id"]) not in notified_request_ids
    ]


def request_datetime_text(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return "—"
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return text

    year = parsed.year
    month = parsed.month
    day = parsed.day
    month_days = (0, 31, 59, 90, 120, 151, 181, 212, 243, 273, 304, 334)
    adjusted_year = year + 1 if month > 2 else year
    days = (
        355666
        + 365 * year
        + (adjusted_year + 3) // 4
        - (adjusted_year + 99) // 100
        + (adjusted_year + 399) // 400
        + day
        + month_days[month - 1]
    )
    jalali_year = -1595 + 33 * (days // 12053)
    days %= 12053
    jalali_year += 4 * (days // 1461)
    days %= 1461
    if days > 365:
        jalali_year += (days - 1) // 365
        days = (days - 1) % 365
    if days < 186:
        jalali_month = 1 + days // 31
        jalali_day = 1 + days % 31
    else:
        jalali_month = 7 + (days - 186) // 30
        jalali_day = 1 + (days - 186) % 30
    return (
        f"{jalali_year:04d}/{jalali_month:02d}/{jalali_day:02d} "
        f"{parsed:%H:%M:%S}"
    )


def record_details_text(item: dict[str, Any]) -> str:
    role_labels = {
        "plaintiff": "خواهان",
        "defendant": "خوانده",
        "complainant": "شاکی",
        "accused": "متهم",
        "appellant": "تجدیدنظرخواه",
        "appellee": "تجدیدنظرخوانده",
    }
    parties = "\n".join(
        f"{role_labels.get(str(party.get('role') or ''), party.get('role') or 'طرف')}: "
        f"{party.get('name') or '—'}"
        for party in item.get("parties") or []
        if isinstance(party, dict)
    ) or "—"
    reasons = "\n".join(
        incomplete_reason_text(reason)
        for reason in item.get("incomplete_reasons") or []
    ) or "فایل‌های ثبت‌شده کامل هستند"
    return (
        f"شناسه: {item.get('notice_id') or '—'}\n"
        f"تاریخ ثبت: {item.get('web_datetime') or '—'}\n"
        f"تاریخ صدور: {item.get('issue_date') or '—'}\n"
        f"مرجع: {item.get('issuer_unit_name') or '—'}\n"
        f"موضوع: {item.get('subject_type') or '—'}\n"
        f"مخاطب / طرفین:\n{parties}\n"
        f"وضعیت فایل‌ها: {reasons}\n\n"
        f"متن ابلاغیه\n{'─' * 24}\n{_display_text(item.get('notice_text'))}\n\n"
        f"توضیحات تکمیلی ثنا\n{'─' * 24}\n"
        f"{_display_text(item.get('other_data_description'))}\n\n"
        f"متن استخراج‌شده از ضمایم (OCR)\n{'─' * 24}\n"
        f"{_display_text(item.get('ocr_text'))}"
    )


def main() -> int:
    import tkinter as tk
    from tkinter import filedialog, font as tkfont, messagebox, ttk

    root = tk.Tk()
    root.title(f"کاتالوگ ابلاغیار | نسخه {__version__}")
    root.geometry("1280x820")
    root.minsize(1000, 650)
    root.configure(background="#F2F5F8")
    assets_dir = Path(__file__).with_name("assets")
    try:
        icon_image = tk.PhotoImage(file=assets_dir / "app-icon.png")
        root.iconphoto(True, icon_image)
        root.iconbitmap(default=str(assets_dir / "app-icon.ico"))
        root._catalog_icon = icon_image
    except (OSError, tk.TclError):
        pass

    style = ttk.Style(root)
    if "clam" in style.theme_names():
        style.theme_use("clam")
    font = ("Segoe UI", 10)
    root.option_add("*Font", font)
    installed_fonts = set(tkfont.families(root))
    title_family = next(
        (
            name
            for name in ("B Nazanin", "Tahoma", "Segoe UI")
            if name in installed_fonts
        ),
        "TkDefaultFont",
    )
    style.configure("TFrame", background="#F2F5F8")
    style.configure("TLabel", background="#F2F5F8", foreground="#243447")
    style.configure(
        "TNotebook", background="#F2F5F8", borderwidth=0, tabposition="ne"
    )
    style.configure(
        "TNotebook.Tab", padding=(22, 10), font=("Segoe UI", 10, "bold")
    )
    style.map(
        "TNotebook.Tab",
        background=[("selected", "#FFFFFF"), ("!selected", "#DDE5ED")],
        foreground=[("selected", "#123B5D"), ("!selected", "#52677A")],
    )
    style.configure(
        "TButton", padding=(14, 8), font=("Segoe UI", 10, "bold")
    )
    style.configure(
        "Accent.TButton", background="#0B6E99", foreground="#FFFFFF"
    )
    style.map(
        "Accent.TButton",
        background=[("active", "#095A7D"), ("disabled", "#AABAC6")],
    )
    style.configure("TEntry", padding=7, fieldbackground="#FFFFFF")
    style.configure(
        "Treeview",
        rowheight=32,
        background="#FFFFFF",
        fieldbackground="#FFFFFF",
        foreground="#263746",
        borderwidth=0,
        font=("Segoe UI", 10, "bold"),
    )
    style.configure(
        "Treeview.Heading",
        padding=(8, 9),
        background="#173F5F",
        foreground="#FFFFFF",
        font=("Segoe UI", 9, "bold"),
    )
    style.map("Treeview", background=[("selected", "#D8EBF5")], foreground=[("selected", "#102A43")])

    def add_tooltip(widget: Any, text: str) -> None:
        state: dict[str, Any] = {"after": None, "window": None}

        def hide(_event: Any = None) -> None:
            if state["after"] is not None:
                widget.after_cancel(state["after"])
                state["after"] = None
            if state["window"] is not None:
                state["window"].destroy()
                state["window"] = None

        def show() -> None:
            state["after"] = None
            if state["window"] is not None:
                return
            window = tk.Toplevel(root)
            window.overrideredirect(True)
            window.attributes("-topmost", True)
            window.geometry(
                f"+{widget.winfo_pointerx() + 12}+{widget.winfo_pointery() + 14}"
            )
            tk.Label(
                window,
                text=text,
                justify="right",
                anchor="e",
                background="#173F5F",
                foreground="#FFFFFF",
                relief="solid",
                borderwidth=1,
                padx=9,
                pady=5,
                font=("Segoe UI", 9),
            ).pack()
            state["window"] = window

        def schedule(_event: Any = None) -> None:
            hide()
            state["after"] = widget.after(450, show)

        widget.bind("<Enter>", schedule, add="+")
        widget.bind("<Leave>", hide, add="+")
        widget.bind("<ButtonPress>", hide, add="+")

    config_path = settings_path()
    default_db = config_path.with_name("catalog.sqlite")
    persisted = load_settings(config_path, {
        "source_root": "",
        "admin_db": str(default_db),
        "exchange_dir": "",
        "viewer_db": str(default_db),
    })
    admin_db_var = tk.StringVar(value=persisted["admin_db"])
    viewer_db_var = tk.StringVar(value=persisted["viewer_db"])
    source_var = tk.StringVar(value=persisted["source_root"])
    exchange_var = tk.StringVar(value=persisted["exchange_dir"])
    query_var = tk.StringVar()
    admin_status = tk.StringVar(value="آماده")
    viewer_status = tk.StringVar(value="عبارت جست‌وجو را وارد کنید")
    total_notices_var = tk.StringVar(value="کل ابلاغیه‌ها: —")
    complete_notices_var = tk.StringVar(value="ابلاغیه‌های کامل: —")
    events: queue.Queue[tuple[str, Any]] = queue.Queue()
    stop_event = threading.Event()
    active = {"value": False}
    records: dict[str, dict[str, Any]] = {}
    request_dialog: dict[str, Any] = {}
    request_monitor: dict[str, Any] = {
        "after": None,
        "exchange": "",
        "initialized": False,
        "notified": set(),
        "running": False,
    }

    def current_settings() -> dict[str, str]:
        return {
            "source_root": source_var.get().strip(),
            "admin_db": admin_db_var.get().strip(),
            "exchange_dir": exchange_var.get().strip(),
            "viewer_db": viewer_db_var.get().strip(),
        }

    def save_current_settings(*, show_error: bool = False) -> bool:
        try:
            save_settings(config_path, current_settings())
        except OSError as error:
            if show_error:
                messagebox.showerror(
                    "ذخیره تنظیمات ناموفق بود",
                    f"تنظیمات ذخیره نشد:\n{error}",
                    parent=root,
                )
            return False
        return True

    header = tk.Frame(root, background="#173F5F", padx=20, pady=10)
    header.pack(fill="x", padx=12, pady=(12, 0))
    title_box = tk.Frame(header, background="#173F5F")
    title_box.pack(side="right")
    tk.Label(
        title_box,
        text="کاتالوگ ابلاغیار",
        background="#173F5F",
        foreground="#FFFFFF",
        anchor="e",
        justify="right",
        font=(title_family, 22, "bold"),
    ).pack(anchor="e")
    tk.Label(
        title_box,
        text=f"سامانه سازمانی نمایه‌سازی و بازیابی ابلاغیه‌ها  |  نسخه {__version__}",
        background="#173F5F",
        foreground="#C9D8E5",
        anchor="e",
        justify="right",
        font=("Segoe UI", 9),
    ).pack(anchor="e")
    stats_box = tk.Frame(header, background="#173F5F")
    stats_box.pack(side="right", padx=32)
    tk.Label(
        stats_box,
        textvariable=total_notices_var,
        background="#173F5F",
        foreground="#FFFFFF",
        font=("Segoe UI", 11, "bold"),
        padx=12,
    ).pack(side="right")
    tk.Label(
        stats_box,
        textvariable=complete_notices_var,
        background="#173F5F",
        foreground="#A9E5BB",
        font=("Segoe UI", 11, "bold"),
        padx=12,
    ).pack(side="right")
    exit_button = tk.Button(
        header,
        text="خروج",
        background="#C62828",
        activebackground="#A91F1F",
        foreground="#FFFFFF",
        activeforeground="#FFFFFF",
        relief="flat",
        cursor="hand2",
        padx=20,
        pady=7,
        font=("Segoe UI", 10, "bold"),
        command=lambda: exit_application(),
    )
    exit_button.pack(side="left")
    add_tooltip(exit_button, "خروج کامل از برنامه")

    notebook = ttk.Notebook(root)
    notebook.pack(fill="both", expand=True, padx=12, pady=(8, 12))
    admin = ttk.Frame(notebook, padding=14)
    viewer = ttk.Frame(notebook, padding=14)
    notebook.add(admin, text="تنظیمات")
    notebook.add(viewer, text="جست‌وجوی ابلاغیه‌ها")
    notebook.select(viewer)

    admin.columnconfigure(1, weight=1)

    def choose_directory(variable: tk.StringVar) -> None:
        selected = filedialog.askdirectory(initialdir=variable.get() or None)
        if selected:
            variable.set(selected)
            save_current_settings(show_error=True)

    def choose_database(variable: tk.StringVar) -> None:
        selected = filedialog.asksaveasfilename(
            initialfile=Path(variable.get()).name,
            defaultextension=".sqlite",
            filetypes=[("SQLite", "*.sqlite"), ("همه فایل‌ها", "*.*")],
        )
        if selected:
            variable.set(selected)
            save_current_settings(show_error=True)

    def path_row(row: int, title: str, variable: tk.StringVar, command: Any) -> None:
        ttk.Label(admin, text=title, anchor="e").grid(
            row=row, column=2, padx=(8, 0), pady=7, sticky="e"
        )
        entry = ttk.Entry(admin, textvariable=variable, justify="right")
        entry.grid(
            row=row, column=1, pady=7, sticky="ew"
        )
        entry.bind(
            "<FocusOut>",
            lambda _event: save_current_settings(show_error=True),
            add="+",
        )
        button = ttk.Button(admin, text="انتخاب…", command=command)
        button.grid(row=row, column=0, padx=(0, 8), pady=7)
        add_tooltip(button, f"انتخاب {title}")

    path_row(0, "ریشه دانلود ابلاغیار", source_var, lambda: choose_directory(source_var))
    path_row(
        1,
        "دیتابیس فعال مدیریت",
        admin_db_var,
        lambda: choose_database(admin_db_var),
    )
    path_row(2, "مسیر تبادل ZIP/CMF", exchange_var, lambda: choose_directory(exchange_var))
    path_row(
        3,
        "Snapshot محلی جست‌وجو",
        viewer_db_var,
        lambda: choose_database(viewer_db_var),
    )

    actions = ttk.Frame(admin)
    actions.grid(row=4, column=0, columnspan=3, pady=(12, 8), sticky="e")
    import_button = ttk.Button(
        actions, text="خواندن داده‌های تاریخی", style="Accent.TButton"
    )
    scan_button = ttk.Button(actions, text="اسکن ZIP/CMF", style="Accent.TButton")
    stop_button = ttk.Button(actions, text="توقف امن", state="disabled")
    import_button.pack(side="right", padx=4)
    scan_button.pack(side="right", padx=4)
    stop_button.pack(side="right", padx=4)
    add_tooltip(import_button, "خواندن سوابق موجود از Repository ابلاغیار")
    add_tooltip(scan_button, "اعتبارسنجی و ورود بسته‌های ZIP/CMF")
    add_tooltip(stop_button, "درخواست توقف امن عملیات در حال اجرا")

    progress = ttk.Progressbar(admin, mode="indeterminate")
    progress.grid(row=5, column=0, columnspan=3, pady=8, sticky="ew")
    ttk.Label(admin, textvariable=admin_status, anchor="e").grid(
        row=6, column=0, columnspan=3, pady=(0, 8), sticky="ew"
    )
    log = tk.Text(
        admin,
        height=17,
        wrap="word",
        state="disabled",
        relief="flat",
        padx=12,
        pady=10,
        background="#FFFFFF",
        foreground="#263746",
        selectbackground="#B7D9EA",
        font=font,
    )
    log.grid(row=7, column=0, columnspan=3, sticky="nsew")
    admin.rowconfigure(7, weight=1)

    def set_log(text: str) -> None:
        log.configure(state="normal")
        log.delete("1.0", "end")
        log.insert("1.0", text)
        log.configure(state="disabled")

    def finish_operation(result: dict[str, Any] | None, error: Exception | None) -> None:
        active["value"] = False
        progress.stop()
        import_button.configure(state="normal")
        scan_button.configure(state="normal")
        stop_button.configure(state="disabled")
        if error is not None:
            admin_status.set("عملیات ناموفق بود")
            set_log(str(error))
            messagebox.showerror("خطا", str(error), parent=root)
            return
        assert result is not None
        admin_status.set(summary_text(result))
        errors = result.get("errors") or []
        set_log("\n".join(str(item) for item in errors) or "عملیات بدون خطا پایان یافت.")

    def poll_events() -> None:
        try:
            while True:
                kind, value = events.get_nowait()
                if kind == "progress":
                    admin_status.set(summary_text(value))
                elif kind == "done":
                    finish_operation(value, None)
                elif kind == "error":
                    finish_operation(None, value)
                elif kind == "request_statuses":
                    finish_request_statuses(value, None)
                elif kind == "request_status_error":
                    finish_request_statuses(None, value)
                elif kind == "request_monitor_result":
                    finish_request_monitor(value)
                elif kind == "request_monitor_error":
                    finish_request_monitor(None)
                elif kind == "show_application":
                    restore_application()
                elif kind == "exit_application":
                    finish_exit()
        except queue.Empty:
            pass
        root.after(100, poll_events)

    def run_operation(worker: Any) -> None:
        if active["value"]:
            return
        if not admin_db_var.get().strip():
            messagebox.showwarning("مسیر لازم است", "مسیر دیتابیس را انتخاب کنید.", parent=root)
            return
        active["value"] = True
        stop_event.clear()
        import_button.configure(state="disabled")
        scan_button.configure(state="disabled")
        stop_button.configure(state="normal")
        progress.start(10)
        admin_status.set("در حال پردازش…")
        set_log("")

        def target() -> None:
            try:
                events.put(("done", worker()))
            except Exception as error:
                events.put(("error", error))

        threading.Thread(target=target, daemon=True).start()

    def import_historical() -> None:
        source = source_var.get().strip()
        if not source:
            messagebox.showwarning("مسیر لازم است", "ریشه دانلود ابلاغیار را انتخاب کنید.", parent=root)
            return
        run_operation(lambda: scan_historical(
            source,
            CatalogStore(admin_db_var.get()),
            on_progress=lambda value: events.put(("progress", value)),
            stop_requested=stop_event.is_set,
        ))

    def import_exchange() -> None:
        exchange = exchange_var.get().strip()
        if not exchange:
            messagebox.showwarning("مسیر لازم است", "مسیر تبادل را انتخاب کنید.", parent=root)
            return
        run_operation(lambda: scan_exchange(exchange, CatalogStore(admin_db_var.get())))

    import_button.configure(command=import_historical)
    scan_button.configure(command=import_exchange)
    stop_button.configure(command=stop_event.set)

    viewer.columnconfigure(0, weight=1)
    viewer.rowconfigure(2, weight=3)
    viewer.rowconfigure(4, weight=2)
    search_bar = ttk.Frame(viewer)
    search_bar.grid(row=0, column=0, sticky="ew")
    search_bar.columnconfigure(2, weight=1)
    search_button = ttk.Button(search_bar, text="جست‌وجو", style="Accent.TButton")
    clear_button = ttk.Button(search_bar, text="پاک کردن")
    clear_button.grid(row=0, column=0, padx=(0, 8))
    search_button.grid(row=0, column=1, padx=(0, 8))
    query_entry = ttk.Entry(search_bar, textvariable=query_var, justify="right")
    query_entry.grid(row=0, column=2, sticky="ew")
    ttk.Label(search_bar, text="عبارت جست‌وجو", anchor="e").grid(row=0, column=3, padx=(8, 0))
    add_tooltip(search_button, "جست‌وجوی فوری عبارت؛ کادر خالی یعنی آخرین ابلاغیه‌ها")
    add_tooltip(clear_button, "پاک‌کردن عبارت و نمایش آخرین ابلاغیه‌ها")
    ttk.Label(viewer, textvariable=viewer_status, anchor="e").grid(
        row=1, column=0, pady=8, sticky="ew"
    )

    columns = (
        "status", "registered", "parties", "subject", "issuer", "archive",
        "case", "notice",
    )
    results_frame = ttk.Frame(viewer)
    results_frame.grid(row=2, column=0, sticky="nsew")
    results_frame.columnconfigure(0, weight=1)
    results_frame.rowconfigure(0, weight=1)
    tree = ttk.Treeview(
        results_frame, columns=columns, show="headings", selectmode="browse"
    )
    headings = {
        "status": "وضعیت",
        "registered": "تاریخ ثبت",
        "parties": "مخاطب / طرفین",
        "subject": "موضوع",
        "issuer": "مرجع",
        "archive": "بایگانی",
        "case": "پرونده",
        "notice": "شماره ابلاغیه",
    }
    widths = {
        "status": 125,
        "registered": 130,
        "parties": 190,
        "subject": 150,
        "issuer": 185,
        "archive": 95,
        "case": 125,
        "notice": 140,
    }
    for column in columns:
        tree.heading(column, text=headings[column], anchor="e")
        tree.column(column, width=widths[column], minwidth=80, anchor="e")

    sort_state: dict[str, Any] = {"column": None, "descending": False}

    def reset_table_sort() -> None:
        sort_state.update(column=None, descending=False)
        for name in columns:
            tree.heading(name, text=headings[name])

    def sort_table(column: str) -> None:
        descending = (
            not sort_state["descending"]
            if sort_state["column"] == column
            else False
        )
        populated = []
        empty = []
        for item_id in tree.get_children(""):
            value = tree.set(item_id, column)
            (populated if str(value).strip() else empty).append((item_id, value))
        populated.sort(
            key=lambda pair: table_sort_key(pair[1]), reverse=descending
        )
        for index, (item_id, _value) in enumerate(populated + empty):
            tree.move(item_id, "", index)
        reset_table_sort()
        sort_state.update(column=column, descending=descending)
        marker = "▼" if descending else "▲"
        tree.heading(column, text=f"{headings[column]} {marker}")

    for column in columns:
        tree.heading(column, command=lambda selected=column: sort_table(selected))
    tree.grid(row=0, column=0, sticky="nsew")
    result_scroll_y = ttk.Scrollbar(
        results_frame, orient="vertical", command=tree.yview
    )
    result_scroll_y.grid(row=0, column=1, sticky="ns")
    result_scroll_x = ttk.Scrollbar(
        results_frame, orient="horizontal", command=tree.xview
    )
    result_scroll_x.grid(row=1, column=0, sticky="ew")
    tree.configure(
        yscrollcommand=result_scroll_y.set, xscrollcommand=result_scroll_x.set
    )

    details_header = ttk.Frame(viewer)
    details_header.grid(row=3, column=0, pady=(12, 5), sticky="ew")
    details_header.columnconfigure(0, weight=1)
    record_actions = ttk.Frame(details_header)
    record_actions.grid(row=0, column=0, sticky="w")
    copy_button = ttk.Button(record_actions, text="کپی متن")
    download_button = ttk.Button(
        record_actions, text="ZIP آماده نیست", state="disabled"
    )
    request_button = ttk.Button(
        record_actions, text="درخواست تهیه بسته", state="disabled"
    )
    requests_button = ttk.Button(record_actions, text="وضعیت درخواست‌های من")
    copy_button.pack(side="right", padx=6)
    download_button.pack(side="right", padx=6)
    request_button.pack(side="right", padx=6)
    requests_button.pack(side="right", padx=(6, 0))
    add_tooltip(copy_button, "کپی همه متن موجود در کادر جزئیات")
    add_tooltip(download_button, "ذخیره ZIP اعتبارسنجی‌شده بدون بازنویسی فایل موجود")
    add_tooltip(request_button, "ثبت درخواست تهیه بسته برای پردازش توسط ابلاغیار")
    add_tooltip(requests_button, "مشاهده آخرین وضعیت درخواست‌های ثبت‌شده شما")
    ttk.Label(
        details_header,
        text="جزئیات ابلاغیه",
        anchor="e",
        font=("Segoe UI", 10, "bold"),
    ).grid(row=0, column=1, sticky="e")

    details_frame = tk.Frame(
        viewer,
        background="#FFFFFF",
        highlightbackground="#B8C9D6",
        highlightcolor="#0B6E99",
        highlightthickness=1,
        bd=0,
    )
    details_frame.grid(row=4, column=0, sticky="nsew")
    details_frame.columnconfigure(0, weight=1)
    details_frame.rowconfigure(0, weight=1)
    details = tk.Text(
        details_frame,
        height=12,
        wrap="word",
        state="disabled",
        relief="flat",
        bd=0,
        padx=18,
        pady=14,
        spacing1=2,
        spacing3=4,
        background="#FFFFFF",
        foreground="#263746",
        selectbackground="#B7D9EA",
        font=("Segoe UI", 10, "bold"),
    )
    details.grid(row=0, column=0, sticky="nsew")
    details_scroll = ttk.Scrollbar(
        details_frame, orient="vertical", command=details.yview
    )
    details_scroll.grid(row=0, column=1, sticky="ns")
    details.configure(yscrollcommand=details_scroll.set)
    details.tag_configure(
        "rtl",
        justify="right",
        lmargin1=12,
        lmargin2=12,
        rmargin=12,
        spacing1=3,
        spacing3=5,
    )
    tree.tag_configure("odd", background="#F7FAFC")

    def copy_details(*, selected_only: bool = False) -> None:
        try:
            text = details.get("sel.first", "sel.last") if selected_only else details.get(
                "1.0", "end-1c"
            )
        except tk.TclError:
            return
        if text:
            root.clipboard_clear()
            root.clipboard_append(text)

    copy_button.configure(command=copy_details)
    details_menu = tk.Menu(root, tearoff=False, font=font)
    details_menu.add_command(
        label="کپی بخش انتخاب‌شده",
        command=lambda: copy_details(selected_only=True),
    )
    details_menu.add_command(label="کپی همه متن", command=copy_details)

    def show_details_menu(event: Any) -> None:
        details_menu.tk_popup(event.x_root, event.y_root)

    def scroll_details(event: Any) -> str:
        details.yview_scroll(-int(event.delta / 120), "units")
        return "break"

    details.bind("<Control-c>", lambda _event: (copy_details(selected_only=True), "break")[1])
    details.bind("<Button-3>", show_details_menu)
    details.bind("<MouseWheel>", scroll_details)

    def selected_record() -> dict[str, Any] | None:
        selected = tree.selection()
        return records.get(selected[0]) if selected else None

    def save_package_as(source: Path, parent: tk.Misc) -> None:
        destination = filedialog.asksaveasfilename(
            title="ذخیره بسته ابلاغیه",
            initialfile=source.name,
            defaultextension=".zip",
            filetypes=[("ZIP", "*.zip")],
            confirmoverwrite=False,
            parent=parent,
        )
        if not destination:
            return
        try:
            target = copy_validated_archive(source, destination)
        except FileExistsError:
            messagebox.showerror(
                "فایل موجود است",
                "برای جلوگیری از overwrite، نام یا مسیر دیگری انتخاب کنید.",
                parent=parent,
            )
            return
        except (ManifestValidationError, OSError) as error:
            messagebox.showerror("دریافت ناموفق", str(error), parent=parent)
            return
        messagebox.showinfo(
            "دریافت انجام شد", f"بسته در این مسیر ذخیره شد:\n{target}", parent=parent
        )

    def download_package() -> None:
        item = selected_record()
        if not item or not item.get("package_available"):
            messagebox.showwarning(
                "بسته موجود نیست",
                "برای این ابلاغیه هنوز بسته معتبر ZIP/CMF موجود نیست.",
                parent=root,
            )
            return
        save_package_as(Path(item["package_path"]), root)

    def request_acquisition() -> None:
        item = selected_record()
        if not item or item.get("package_available"):
            return
        exchange = exchange_var.get().strip()
        if not exchange:
            messagebox.showwarning(
                "مسیر تبادل لازم است",
                "ابتدا در تب تنظیمات، مسیر تبادل ZIP/CMF را انتخاب کنید.",
                parent=root,
            )
            notebook.select(admin)
            return
        notice_no = str(item.get("notice_no") or "").strip()
        if not notice_no:
            messagebox.showerror(
                "شماره ابلاغیه موجود نیست",
                "ثبت درخواست بدون شماره ابلاغیه ممکن نیست.",
                parent=root,
            )
            return
        if not messagebox.askyesno(
            "تأیید درخواست",
            "درخواست تهیه بسته برای ابلاغیار ثبت شود؟\n"
            "ابلاغیار در صورت نیاز واکشی از ثنا را انجام می‌دهد.",
            parent=root,
        ):
            return
        try:
            request_path = publish_request(exchange, notice_no)
        except (OSError, ValueError) as error:
            messagebox.showerror("ثبت درخواست ناموفق", str(error), parent=root)
            return
        viewer_status.set(f"درخواست ابلاغیه {notice_no} ثبت شد")
        messagebox.showinfo(
            "درخواست ثبت شد",
            f"درخواست برای پردازش توسط ابلاغیار ثبت شد:\n{request_path.name}",
            parent=root,
        )

    def finish_request_statuses(
        items: list[dict] | None, error: Exception | None
    ) -> None:
        window = request_dialog.get("window")
        try:
            if window is None or not window.winfo_exists():
                return
        except tk.TclError:
            return
        refresh = request_dialog["refresh"]
        status_var = request_dialog["status_var"]
        refresh.configure(state="normal")
        if error is not None:
            status_var.set("خواندن وضعیت درخواست‌ها ناموفق بود")
            messagebox.showerror("خطا", str(error), parent=window)
            return
        request_tree = request_dialog["tree"]
        request_tree.delete(*request_tree.get_children())
        assert items is not None
        request_items = {}
        for index, item in enumerate(items):
            detail = item.get("detail") or "—"
            if item.get("package_name"):
                detail = f"{detail} | {item['package_name']}"
            request_id = str(item.get("request_id") or index)
            request_items[request_id] = item
            tags = ["odd"] if index % 2 else []
            if item.get("status") == "ready":
                tags.append("ready")
            request_tree.insert("", "end", iid=request_id, values=(
                request_status_text(str(item.get("status") or "")),
                detail,
                request_datetime_text(item.get("updated_at")),
                request_datetime_text(item.get("created_at")),
                item.get("notice_no") or "—",
            ), tags=tags)
        request_dialog["items"] = request_items
        status_var.set(
            f"{len(items)} درخواست" if items else "درخواستی برای این کاربر ثبت نشده است"
        )

    def refresh_request_statuses() -> None:
        window = request_dialog.get("window")
        if window is None:
            return
        exchange = exchange_var.get().strip()
        if not exchange:
            messagebox.showwarning(
                "مسیر تبادل لازم است",
                "ابتدا در تب تنظیمات، مسیر تبادل ZIP/CMF را انتخاب کنید.",
                parent=window,
            )
            notebook.select(admin)
            return
        request_dialog["refresh"].configure(state="disabled")
        request_dialog["status_var"].set("در حال خواندن وضعیت درخواست‌ها…")

        def target() -> None:
            try:
                events.put(("request_statuses", list_user_requests(exchange)))
            except Exception as error:
                events.put(("request_status_error", error))

        threading.Thread(target=target, daemon=True).start()

    def show_request_statuses() -> None:
        current = request_dialog.get("window")
        try:
            if current is not None and current.winfo_exists():
                current.lift()
                refresh_request_statuses()
                return
        except tk.TclError:
            pass
        window = tk.Toplevel(root)
        window.title("وضعیت درخواست‌های من")
        window.geometry("1050x430")
        window.minsize(780, 320)
        window.configure(background="#F2F5F8")
        window.transient(root)

        toolbar = ttk.Frame(window, padding=(12, 12, 12, 8))
        toolbar.pack(fill="x")
        status_var = tk.StringVar(value="آماده")
        refresh = ttk.Button(toolbar, text="به‌روزرسانی")
        refresh.pack(side="right")
        add_tooltip(refresh, "خواندن دوباره وضعیت درخواست‌ها از مسیر تبادل")
        ttk.Label(toolbar, textvariable=status_var, anchor="e").pack(
            side="right", fill="x", expand=True
        )

        table_frame = ttk.Frame(window, padding=(12, 0, 12, 12))
        table_frame.pack(fill="both", expand=True)
        table_frame.columnconfigure(0, weight=1)
        table_frame.rowconfigure(0, weight=1)
        request_tree = ttk.Treeview(
            table_frame,
            columns=("status", "detail", "updated", "created", "notice"),
            show="headings",
        )
        request_headings = {
            "status": "وضعیت",
            "detail": "شرح / بسته",
            "updated": "آخرین به‌روزرسانی",
            "created": "زمان ثبت",
            "notice": "شماره ابلاغیه",
        }
        request_widths = {
            "status": 145,
            "detail": 330,
            "updated": 180,
            "created": 180,
            "notice": 150,
        }
        for column in request_headings:
            request_tree.heading(column, text=request_headings[column], anchor="e")
            request_tree.column(
                column, width=request_widths[column], minwidth=100, anchor="e"
            )
        request_tree.grid(row=0, column=0, sticky="nsew")
        request_scroll = ttk.Scrollbar(
            table_frame, orient="vertical", command=request_tree.yview
        )
        request_scroll.grid(row=0, column=1, sticky="ns")
        request_tree.configure(yscrollcommand=request_scroll.set)
        request_tree.tag_configure("odd", background="#F7FAFC")
        request_tree.tag_configure("ready", foreground="#16803A")

        def download_ready_request(event: Any) -> None:
            row = request_tree.identify_row(event.y)
            if not row:
                return
            request_tree.selection_set(row)
            item = request_dialog.get("items", {}).get(row)
            if not item or item.get("status") != "ready":
                return
            try:
                source = ready_package_path(
                    exchange_var.get().strip(), str(item.get("package_name") or "")
                )
            except (OSError, ValueError) as error:
                messagebox.showerror(
                    "دریافت ناموفق",
                    f"فایل بسته آماده در مسیر تبادل معتبر یا قابل دسترس نیست:\n{error}",
                    parent=window,
                )
                return
            save_package_as(source, window)

        def close_dialog() -> None:
            request_dialog.clear()
            window.destroy()

        request_dialog.update(
            window=window,
            tree=request_tree,
            status_var=status_var,
            refresh=refresh,
        )
        refresh.configure(command=refresh_request_statuses)
        request_tree.bind("<Double-1>", download_ready_request)
        window.protocol("WM_DELETE_WINDOW", close_dialog)
        refresh_request_statuses()

    download_button.configure(command=download_package)
    request_button.configure(command=request_acquisition)
    requests_button.configure(command=show_request_statuses)

    def notify_ready_requests(items: list[dict]) -> None:
        valid_items = []
        exchange = exchange_var.get().strip()
        for item in items:
            try:
                ready_package_path(exchange, str(item.get("package_name") or ""))
            except (OSError, ValueError):
                continue
            valid_items.append(item)
        if not valid_items:
            return

        notified = request_monitor["notified"]
        notified.update(str(item["request_id"]) for item in valid_items)
        if len(valid_items) == 1:
            notice_no = str(valid_items[0].get("notice_no") or "").strip() or "درخواستی"
            message = (
                f"بسته ابلاغیه {notice_no} آماده است. برای دریافت، "
                "«وضعیت درخواست‌های من» را باز کنید."
            )
            viewer_status.set(f"بسته ابلاغیه {notice_no} آماده شد")
        else:
            message = (
                f"{len(valid_items)} بسته درخواستی آماده است. برای دریافت، "
                "«وضعیت درخواست‌های من» را باز کنید."
            )
            viewer_status.set(f"{len(valid_items)} بسته درخواستی آماده شد")

        if ensure_tray_icon(show_error=False):
            try:
                tray_state["icon"].notify(message, "بسته آماده شد")
            except Exception:
                root.bell()
        else:
            root.bell()

    def finish_request_monitor(items: list[dict] | None) -> None:
        request_monitor["running"] = False
        if tray_state["exiting"]:
            return
        if items is not None:
            ready_items = _ready_request_candidates(
                items, request_monitor["notified"]
            )
            if request_monitor["initialized"]:
                notify_ready_requests(ready_items)
            else:
                request_monitor["notified"].update(
                    str(item["request_id"]) for item in ready_items
                )
                request_monitor["initialized"] = True
        request_monitor["after"] = root.after(
            REQUEST_MONITOR_INTERVAL_MS, check_request_statuses
        )

    def check_request_statuses() -> None:
        request_monitor["after"] = None
        if request_monitor["running"] or tray_state["exiting"]:
            return
        exchange = exchange_var.get().strip()
        if not exchange:
            request_monitor["after"] = root.after(
                REQUEST_MONITOR_INTERVAL_MS, check_request_statuses
            )
            return
        if exchange != request_monitor["exchange"]:
            request_monitor.update(
                exchange=exchange, initialized=False, notified=set()
            )
        request_monitor["running"] = True

        def target() -> None:
            try:
                events.put(("request_monitor_result", list_user_requests(exchange)))
            except Exception:
                events.put(("request_monitor_error", None))

        threading.Thread(target=target, daemon=True).start()

    def show_details(_event: Any = None) -> None:
        selected = tree.selection()
        if not selected:
            return
        item = records[selected[0]]
        download_button.configure(
            state="normal" if item.get("package_available") else "disabled",
            text="دریافت ZIP" if item.get("package_available") else "ZIP آماده نیست",
        )
        request_button.configure(
            state=(
                "normal"
                if not item.get("package_available") and item.get("notice_no")
                else "disabled"
            )
        )
        details.configure(state="normal")
        details.delete("1.0", "end")
        details.insert("1.0", record_details_text(item), ("rtl",))
        details.see("1.0")
        details.configure(state="disabled")

    def search() -> None:
        needle = query_var.get().strip()
        try:
            store = CatalogStore(viewer_db_var.get())
            result = store.search(needle, 100) if needle else store.recent(100)
            counts = store.notice_counts()
        except Exception as error:
            messagebox.showerror("خطا", str(error), parent=root)
            return
        total_notices_var.set(f"کل ابلاغیه‌ها: {counts['total']}")
        complete_notices_var.set(f"ابلاغیه‌های کامل: {counts['complete']}")
        tree.delete(*tree.get_children())
        records.clear()
        reset_table_sort()
        download_button.configure(state="disabled", text="ZIP آماده نیست")
        request_button.configure(state="disabled")
        details.configure(state="normal")
        details.delete("1.0", "end")
        details.configure(state="disabled")
        for index, item in enumerate(result):
            key = f"row-{index}"
            records[key] = item
            tree.insert("", "end", iid=key, values=(
                record_status(item),
                item.get("web_datetime") or "",
                party_names_text(item),
                item.get("subject_type") or "",
                item.get("issuer_unit_name") or "",
                item.get("archive_no") or "",
                item.get("case_no") or "",
                item.get("notice_no") or "",
            ), tags=("odd",) if index % 2 else ())
        viewer_status.set(
            f"{len(result)} نتیجه" if needle else f"آخرین {len(result)} ابلاغیه"
        )
        if result:
            tree.selection_set("row-0")
            tree.focus("row-0")
            tree.see("row-0")
            show_details()

    live_search: dict[str, str | None] = {"after": None}

    def cancel_live_search() -> None:
        if live_search["after"] is not None:
            root.after_cancel(live_search["after"])
            live_search["after"] = None

    def run_live_search() -> None:
        live_search["after"] = None
        search()

    def schedule_live_search(*_args: Any) -> None:
        cancel_live_search()
        live_search["after"] = root.after(350, run_live_search)

    def search_now() -> None:
        cancel_live_search()
        search()

    def clear_search() -> None:
        query_var.set("")
        search_now()
        query_entry.focus_set()

    query_var.trace_add("write", schedule_live_search)
    search_button.configure(command=search_now)
    clear_button.configure(command=clear_search)
    query_entry.bind("<Return>", lambda _event: search_now())
    tree.bind("<<TreeviewSelect>>", show_details)

    tray_state: dict[str, Any] = {"icon": None, "exiting": False}

    def restore_application() -> None:
        root.deiconify()
        root.state("normal")
        root.lift()
        root.focus_force()

    def show_application(_icon: Any = None, _item: Any = None) -> None:
        events.put(("show_application", None))

    def finish_exit() -> None:
        save_current_settings(show_error=True)
        icon = tray_state.get("icon")
        if icon is not None:
            icon.stop()
            tray_state["icon"] = None
        root.destroy()

    def exit_application(_icon: Any = None, _item: Any = None) -> None:
        if tray_state["exiting"]:
            return
        tray_state["exiting"] = True
        events.put(("exit_application", None))

    def ensure_tray_icon(*, show_error: bool) -> bool:
        if tray_state["icon"] is None:
            try:
                import pystray
                from PIL import Image

                with Image.open(assets_dir / "app-icon.png") as source:
                    tray_image = source.convert("RGBA").copy()
                tray_state["icon"] = pystray.Icon(
                    "eblaghyar-catalog",
                    tray_image,
                    "کاتالوگ ابلاغیار",
                    menu=pystray.Menu(
                        pystray.MenuItem("نمایش برنامه", show_application, default=True),
                        pystray.MenuItem("خروج", exit_application),
                    ),
                )
                tray_state["icon"].run_detached()
            except (ImportError, OSError) as error:
                tray_state["icon"] = None
                if show_error:
                    messagebox.showerror(
                        "System Tray در دسترس نیست",
                        f"امکان انتقال برنامه به System Tray وجود ندارد:\n{error}",
                        parent=root,
                    )
                return False
        return True

    def minimize_to_tray() -> None:
        if not ensure_tray_icon(show_error=True):
            return
        save_current_settings(show_error=False)
        root.withdraw()

    root.protocol("WM_DELETE_WINDOW", minimize_to_tray)
    root.after(100, poll_events)
    root.after(0, search)
    root.after(0, check_request_statuses)
    query_entry.focus_set()
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
