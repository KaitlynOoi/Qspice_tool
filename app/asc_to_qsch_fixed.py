#!/usr/bin/env python
# -------------------------------------------------------------------------------
# asc_to_qsch_fixed_gui.py
#
# GUI wrapper around asc_to_qsch_fixed.py.
# Core conversion logic is kept the same; this version only adds:
#   - file pickers for ASC/QSCH
#   - an optional list of extra .asy search folders
#   - a log window for conversion status
#   - CLI compatibility (same behavior as the original when args are passed)
# -------------------------------------------------------------------------------
import os
import sys
import logging
import xml.etree.ElementTree as ET
from optparse import OptionParser

from spicelib.editor.asy_reader import AsyReader
from spicelib.editor.asc_editor import AscEditor
from spicelib.editor.qsch_editor import QschEditor, QschTag
from spicelib.utils.file_search import find_file_in_directory

def _patch_spicelib_updated():
    try:
        import spicelib.editor.base_schematic as bs
        cls = getattr(bs, "SpiceCircuit", None)
        if cls is not None and not hasattr(cls, "updated"):
            def updated(self):
                if hasattr(self, "was_modified"):
                    self.was_modified = True
            cls.updated = updated
    except Exception:
        pass

_patch_spicelib_updated()

_logger = logging.getLogger("spicelib.AscToQschFixed")


class _MissingSymbolStub:
    """Minimal stand-in returned when AscEditor._get_symbol() can't find a
    component's .asy file."""

    symbol_type = "CELL"

    def is_subcircuit(self):
        return False

    def get_library(self):
        return None


def _patch_asc_editor_symbol_lookup():
    """Wraps AscEditor._get_symbol so a missing .asy file logs a warning
    and returns a safe stub instead of raising FileNotFoundError."""
    original = AscEditor._get_symbol

    def patched(self, symbol):
        try:
            return original(self, symbol)
        except FileNotFoundError:
            print(
                f"  NOTE: no .asy found for symbol type '{symbol}' during initial "
                f"parse -- will retry with full search path list below, and use a "
                f"placeholder if still unresolved."
            )
            return _MissingSymbolStub()

    AscEditor._get_symbol = patched


def _default_search_paths(asc_file, extra_paths):
    """Same default locations the stock converter looks in, plus whatever
    the user passed with -a or selected in the GUI."""
    return extra_paths + [
        os.path.split(asc_file)[0],
        os.path.expanduser("~/AppData/Local/LTspice/lib/sym"),
        os.path.expanduser("~/Documents/LtspiceXVII/lib/sym"),
        os.path.expanduser("~/Library/Application Support/LTspice/lib/sym"),
    ]


def _build_placeholder_symbol(reference, symbol_type, value):
    """Builds a visible, clearly-labeled placeholder for a component whose
    .asy could not be found anywhere."""
    symbol = QschTag("symbol", "?")
    symbol.items.append(QschTag("type:", "?"))
    symbol.items.append(QschTag("description:", f"UNRESOLVED SYMBOL: {symbol_type}"))
    symbol.items.append(QschTag("shorted pins:", "false"))

    box, _ = QschTag.parse("«rect (0,0) (300,-300) 0 2 0 0xff0000 0x1000000 -1 0 -1»")
    symbol.items.append(box)

    ref_text, _ = QschTag.parse(f'«text (20,-150) 1 7 0 0x1000000 -1 -1 "{reference}"»')
    symbol.items.append(ref_text)

    val_text, _ = QschTag.parse(f'«text (20,-100) 1 7 0 0x1000000 -1 -1 "{value}"»')
    symbol.items.append(val_text)

    warn_text, _ = QschTag.parse(
        f'«text (20,-50) 1 7 0 0xff0000 -1 -1 "NO SYMBOL: {symbol_type}"»'
    )
    symbol.items.append(warn_text)

    return symbol


def convert_asc_to_qsch(asc_file, qsch_file, search_paths=None, log=None):
    """Converts an ASC file to a QSCH schematic, preserving every instance,
    with visible placeholders for unresolvable symbols."""
    if search_paths is None:
        search_paths = []

    def emit(msg=""):
        if log is None:
            print(msg)
        else:
            log(msg)

    all_search_paths = _default_search_paths(asc_file, search_paths)

    existing_paths = [p for p in all_search_paths if p and os.path.isdir(p)]
    if existing_paths:
        AscEditor.set_custom_library_paths(*existing_paths)

    _patch_asc_editor_symbol_lookup()

    asc_editor = AscEditor(asc_file)

    parent_dir = os.path.dirname(
        os.path.realpath(
            __import__("spicelib.scripts.asc_to_qsch", fromlist=["dummy"]).__file__
        )
    )
    xml_file = os.path.join(parent_dir, "asc_to_qsch_data.xml")
    conversion_data = ET.parse(xml_file)
    root = conversion_data.getroot()

    offset = root.find("offset")
    offset_x = float(offset.get("x"))
    offset_y = float(offset.get("y"))
    scale = root.find("scaling")
    scale_x = float(scale.get("x"))
    scale_y = float(scale.get("y"))

    asc_editor.scale(offset_x=offset_x, offset_y=offset_y, scale_x=scale_x, scale_y=scale_y)

    asy_reader_cache = {}

    total = 0
    converted = 0
    placeholders = 0
    resolution_log = []

    for comp in asc_editor.components.values():
        total += 1
        asy_reader = asy_reader_cache.get(comp.symbol, None)
        resolved_from = "cache" if asy_reader is not None else None

        if asy_reader is None:
            for sym_root in all_search_paths:
                if not sym_root or not os.path.exists(sym_root):
                    continue
                symbol_asy_file = find_file_in_directory(sym_root, comp.symbol + ".asy")
                if symbol_asy_file is not None:
                    asy_reader = AsyReader(symbol_asy_file)
                    asy_reader_cache[comp.symbol] = asy_reader
                    resolved_from = symbol_asy_file
                    break

        if comp.rotation == 90:
            comp.rotation = 270
        elif comp.rotation == 270:
            comp.rotation = 90
        elif comp.rotation == 90 + 360:
            comp.rotation = 270 + 360
        elif comp.rotation == 270 + 360:
            comp.rotation = 90 + 360

        value = comp.attributes.get("Value", "<val>")

        if asy_reader is not None:
            symbol_tag = asy_reader.to_qsch(comp.reference, value)
            comp.attributes["symbol"] = symbol_tag
            converted += 1
        else:
            symbol_tag = _build_placeholder_symbol(comp.reference, comp.symbol, value)
            comp.attributes["symbol"] = symbol_tag
            placeholders += 1

        resolution_log.append((comp.reference, comp.symbol, resolved_from, value))

    qsch_editor = QschEditor(qsch_file, create_blank=True)
    qsch_editor.copy_from(asc_editor)
    qsch_editor.save_netlist(qsch_file)

    emit("")
    emit("--- Per-component resolution log ---")
    for ref, symtype, resolved_from, value in resolution_log:
        status = resolved_from if resolved_from else "*** NOT FOUND ***"
        emit(f"  {ref:8s} type={symtype:20s} value={value:15s} -> {status}")

    emit("")
    emit(
        f"Summary: {converted}/{total} components resolved with a real symbol, "
        f"{placeholders} placeholder(s) (visible but no pins -- fix these via "
        f"Track B or by adding the missing .asy to a search path)."
    )
    if placeholders:
        emit("WARNING: placeholders do not carry net connectivity. "
             "Do not trust simulation results until these are resolved.")


# ----------------------- GUI -----------------------

def _normalize_paths(paths):
    cleaned = []
    seen = set()
    for p in paths:
        if not p:
            continue
        p = os.path.normpath(p.strip().strip('"').strip("'"))
        if p and p not in seen:
            seen.add(p)
            cleaned.append(p)
    return cleaned


def run_gui():
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk

    root = tk.Tk()
    root.title("asc_to_qsch_fixed")
    root.geometry("860x620")

    asc_var = tk.StringVar()
    qsch_var = tk.StringVar()
    use_default_output_var = tk.BooleanVar(value=True)

    main = ttk.Frame(root, padding=12)
    main.pack(fill="both", expand=True)
    main.columnconfigure(0, weight=1)
    main.rowconfigure(5, weight=1)

    # File selectors
    files = ttk.LabelFrame(main, text="Files", padding=10)
    files.grid(row=0, column=0, sticky="ew")
    files.columnconfigure(0, weight=1)

    def browse_asc():
        path = filedialog.askopenfilename(
            parent=root,
            title="Select LTspice .asc file",
            filetypes=[("LTspice schematic", "*.asc"), ("All files", "*.*")],
        )
        if path:
            asc_var.set(path)
            if use_default_output_var.get():
                base, _ = os.path.splitext(path)
                qsch_var.set(base + ".qsch")

    def browse_qsch():
        path = filedialog.asksaveasfilename(
            parent=root,
            title="Select output .qsch file",
            defaultextension=".qsch",
            filetypes=[("QSpice schematic", "*.qsch"), ("All files", "*.*")],
        )
        if path:
            qsch_var.set(path)
            use_default_output_var.set(False)

    ttk.Label(files, text="Input .asc").grid(row=0, column=0, sticky="w")
    ttk.Entry(files, textvariable=asc_var).grid(row=1, column=0, sticky="ew", padx=(0, 8))
    ttk.Button(files, text="Browse...", command=browse_asc).grid(row=1, column=1, sticky="ew")

    ttk.Label(files, text="Output .qsch").grid(row=2, column=0, sticky="w", pady=(10, 0))
    ttk.Entry(files, textvariable=qsch_var).grid(row=3, column=0, sticky="ew", padx=(0, 8))
    ttk.Button(files, text="Browse...", command=browse_qsch).grid(row=3, column=1, sticky="ew")

    ttk.Checkbutton(
        files,
        text="Use same base name for output when input is chosen",
        variable=use_default_output_var,
    ).grid(row=4, column=0, columnspan=2, sticky="w", pady=(8, 0))

    # Search paths
    paths_frame = ttk.LabelFrame(main, text="Extra .asy search folders", padding=10)
    paths_frame.grid(row=1, column=0, sticky="ew", pady=(10, 0))
    paths_frame.columnconfigure(0, weight=1)
    paths_frame.rowconfigure(0, weight=1)

    list_frame = ttk.Frame(paths_frame)
    list_frame.grid(row=0, column=0, sticky="nsew")
    list_frame.columnconfigure(0, weight=1)

    path_list = tk.Listbox(list_frame, height=5, selectmode=tk.EXTENDED)
    path_list.grid(row=0, column=0, sticky="nsew")
    path_scroll = ttk.Scrollbar(list_frame, orient="vertical", command=path_list.yview)
    path_scroll.grid(row=0, column=1, sticky="ns")
    path_list.configure(yscrollcommand=path_scroll.set)

    def refresh_path_list(items=None):
        path_list.delete(0, tk.END)
        for p in (items or []):
            path_list.insert(tk.END, p)

    custom_paths = []

    def add_path():
        nonlocal custom_paths
        path = filedialog.askdirectory(parent=root, title="Select symbol search folder")
        if path:
            custom_paths = _normalize_paths(custom_paths + [path])
            refresh_path_list(custom_paths)

    def remove_selected_path():
        nonlocal custom_paths
        selected = list(path_list.curselection())
        if not selected:
            return
        custom_paths = [p for i, p in enumerate(custom_paths) if i not in set(selected)]
        refresh_path_list(custom_paths)

    btns = ttk.Frame(paths_frame)
    btns.grid(row=1, column=0, sticky="w", pady=(8, 0))
    ttk.Button(btns, text="Add folder...", command=add_path).pack(side="left")
    ttk.Button(btns, text="Remove selected", command=remove_selected_path).pack(side="left", padx=(8, 0))

    # Log
    log_frame = ttk.LabelFrame(main, text="Log", padding=10)
    log_frame.grid(row=5, column=0, sticky="nsew", pady=(10, 0))
    log_frame.columnconfigure(0, weight=1)
    log_frame.rowconfigure(0, weight=1)

    log_text = tk.Text(log_frame, wrap="word", height=18)
    log_text.grid(row=0, column=0, sticky="nsew")
    log_scroll = ttk.Scrollbar(log_frame, orient="vertical", command=log_text.yview)
    log_scroll.grid(row=0, column=1, sticky="ns")
    log_text.configure(yscrollcommand=log_scroll.set)

    def log(msg=""):
        log_text.insert(tk.END, msg + "\n")
        log_text.see(tk.END)
        root.update_idletasks()

    def run_conversion():
        asc_file = asc_var.get().strip()
        qsch_file = qsch_var.get().strip()

        if not asc_file or not os.path.isfile(asc_file):
            messagebox.showerror("Missing file", "Please select a valid .asc file.", parent=root)
            return
        if not qsch_file:
            messagebox.showerror("Missing file", "Please select an output .qsch file.", parent=root)
            return

        log_text.delete("1.0", tk.END)
        log("Starting conversion...")
        log(f"ASC:  {asc_file}")
        log(f"QSCH: {qsch_file}")
        log("")

        try:
            convert_asc_to_qsch(
                asc_file,
                qsch_file,
                search_paths=_normalize_paths(custom_paths),
                log=log,
            )
            messagebox.showinfo("Done", f"Saved: {qsch_file}", parent=root)
        except Exception as exc:
            log("")
            log(f"ERROR: {exc}")
            messagebox.showerror("Conversion failed", str(exc), parent=root)

    action_row = ttk.Frame(main)
    action_row.grid(row=2, column=0, sticky="w", pady=(10, 0))
    ttk.Button(action_row, text="Run conversion", command=run_conversion).pack(side="left")
    ttk.Button(action_row, text="Quit", command=root.destroy).pack(side="left", padx=(8, 0))

    hint = ttk.Label(
        main,
        text="Tip: choose the .asc first, then add any extra symbol folders if your parts are not in LTspice's default library.",
        wraplength=820,
    )
    hint.grid(row=3, column=0, sticky="w", pady=(10, 0))

    root.mainloop()


# ----------------------- CLI (original behavior) -----------------------

def main():
    # No args or an explicit GUI flag opens the GUI.
    if len(sys.argv) == 1 or sys.argv[1] in ("--gui", "-g"):
        run_gui()
        return

    opts = OptionParser(
        usage="usage: %prog [options] ASC_FILE [QSCH_FILE]",
        version="%prog 0.3-fixed",
    )
    opts.add_option(
        "-a",
        "--add",
        action="append",
        type="string",
        dest="path",
        help="Add a path for searching for symbols (.asy files). Can be given multiple times.",
    )

    (options, args) = opts.parse_args()

    if len(args) < 1:
        opts.print_help()
        sys.exit(-1)

    asc_file = args[0]
    if len(args) > 1:
        qsch_file = args[1]
    else:
        qsch_file = os.path.splitext(asc_file)[0] + ".qsch"

    search_paths = [] if options.path is None else options.path
    search_paths = _normalize_paths(search_paths)

    print(f"Using {qsch_file} as output file")
    convert_asc_to_qsch(asc_file, qsch_file, search_paths)


if __name__ == "__main__":
    main()
