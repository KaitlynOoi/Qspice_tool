#!/usr/bin/env python3
"""
combined_app.py

One tool, one run: converts an LTspice .asc straight into a fixed,
model-complete QSpice .qsch, without having to run two separate apps.

Internally it still uses your two original scripts unchanged:
  1) asc_to_qsch_fixed.convert_asc_to_qsch()   -- ASC -> QSCH conversion
  2) fix_and_import_models.process_models()    -- fixes/model-imports on that QSCH

This file is what PyInstaller packages into the .exe.
"""
import os
import sys
import traceback
import difflib
import shutil
from pathlib import Path


def _fatal_error_box(title, exc):
    try:
        import tkinter as tk
        from tkinter import messagebox
        root = tk.Tk()
        root.withdraw()
        messagebox.showerror(title, f"{exc}\n\n{traceback.format_exc()}")
        root.destroy()
    except Exception:
        print(f"{title}: {exc}")
        traceback.print_exc()


def _build_intermediate_paths(final_qsch: str) -> tuple[str, str]:
    """Return (step1_path, diff_path) for the requested final output file."""
    final_path = Path(final_qsch)
    step1_path = final_path.with_name(f"{final_path.stem}.step1.qsch")
    diff_path = final_path.with_name(f"{final_path.stem}.diff.txt")
    return str(step1_path), str(diff_path)


def _write_qsch_diff(step1_qsch: str, final_qsch: str, diff_path: str) -> int:
    """Write a unified diff between the raw conversion and the final fixed file."""
    try:
        step1_lines = Path(step1_qsch).read_text(encoding='cp1252', errors='replace').splitlines(keepends=True)
        final_lines = Path(final_qsch).read_text(encoding='cp1252', errors='replace').splitlines(keepends=True)
    except Exception:
        return 0

    diff = list(
        difflib.unified_diff(
            step1_lines,
            final_lines,
            fromfile=os.path.basename(step1_qsch),
            tofile=os.path.basename(final_qsch),
        )
    )
    try:
        Path(diff_path).write_text(''.join(diff), encoding='utf-8')
    except Exception:
        return 0
    return len(diff)




def run_gui():
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk

    import asc_to_qsch_fixed as step1
    import fix_and_import_models as step2

    root = tk.Tk()
    root.title("LTspice -> QSpice: Convert + Fix (one run)")
    root.geometry("900x700")

    asc_var = tk.StringVar()
    qsch_var = tk.StringVar()
    use_default_output_var = tk.BooleanVar(value=True)
    auto_search_var = tk.BooleanVar(value=True)
    fix_annotations_var = tk.BooleanVar(value=True)
    replace_zero_ohm_var = tk.BooleanVar(value=True)
    device_model_fix_var = tk.BooleanVar(value=True)

    main = ttk.Frame(root, padding=12)
    main.pack(fill="both", expand=True)
    main.columnconfigure(0, weight=1)
    main.rowconfigure(6, weight=1)

    # ---- Files ----
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

    ttk.Label(files, text="Output .qsch (final, fixed result)").grid(row=2, column=0, sticky="w", pady=(10, 0))
    ttk.Entry(files, textvariable=qsch_var).grid(row=3, column=0, sticky="ew", padx=(0, 8))
    ttk.Button(files, text="Browse...", command=browse_qsch).grid(row=3, column=1, sticky="ew")

    ttk.Checkbutton(
        files,
        text="Use same base name for output when input is chosen",
        variable=use_default_output_var,
    ).grid(row=4, column=0, columnspan=2, sticky="w", pady=(8, 0))

    # ---- Extra symbol search paths (step 1) ----
    paths_frame = ttk.LabelFrame(main, text="Extra .asy symbol search folders (for step 1)", padding=10)
    paths_frame.grid(row=1, column=0, sticky="ew", pady=(10, 0))
    paths_frame.columnconfigure(0, weight=1)

    list_frame = ttk.Frame(paths_frame)
    list_frame.grid(row=0, column=0, sticky="nsew")
    list_frame.columnconfigure(0, weight=1)

    path_list = tk.Listbox(list_frame, height=4, selectmode=tk.EXTENDED)
    path_list.grid(row=0, column=0, sticky="nsew")
    path_scroll = ttk.Scrollbar(list_frame, orient="vertical", command=path_list.yview)
    path_scroll.grid(row=0, column=1, sticky="ns")
    path_list.configure(yscrollcommand=path_scroll.set)

    custom_paths = []

    def refresh_path_list():
        path_list.delete(0, tk.END)
        for p in custom_paths:
            path_list.insert(tk.END, p)

    def add_path():
        path = filedialog.askdirectory(parent=root, title="Select symbol search folder")
        if path:
            custom_paths[:] = step1._normalize_paths(custom_paths + [path])
            refresh_path_list()

    def remove_selected_path():
        selected = set(path_list.curselection())
        custom_paths[:] = [p for i, p in enumerate(custom_paths) if i not in selected]
        refresh_path_list()

    btns = ttk.Frame(paths_frame)
    btns.grid(row=1, column=0, sticky="w", pady=(8, 0))
    ttk.Button(btns, text="Add folder...", command=add_path).pack(side="left")
    ttk.Button(btns, text="Remove selected", command=remove_selected_path).pack(side="left", padx=(8, 0))

    # ---- Fix options (step 2) ----
    fix_frame = ttk.LabelFrame(main, text="Model fix-up options (for step 2)", padding=10)
    fix_frame.grid(row=2, column=0, sticky="ew", pady=(10, 0))
    ttk.Checkbutton(
        fix_frame, text="Auto-search default LTspice library folders first",
        variable=auto_search_var,
    ).grid(row=0, column=0, sticky="w")
    ttk.Checkbutton(
        fix_frame, text="Fix plain text annotations / hidden notes as comments (recommended)",
        variable=fix_annotations_var,
    ).grid(row=1, column=0, sticky="w")
    ttk.Checkbutton(
        fix_frame, text="Replace 0-ohm resistors with wires (recommended)",
        variable=replace_zero_ohm_var,
    ).grid(row=2, column=0, sticky="w")
    ttk.Checkbutton(
        fix_frame, text="Detect & prompt for missing component model/subcircuit definitions (recommended)",
        variable=device_model_fix_var,
    ).grid(row=3, column=0, sticky="w")

    # ---- Log ----
    log_frame = ttk.LabelFrame(main, text="Log", padding=10)
    log_frame.grid(row=6, column=0, sticky="nsew", pady=(10, 0))
    log_frame.columnconfigure(0, weight=1)
    log_frame.rowconfigure(0, weight=1)

    log_text = tk.Text(log_frame, wrap="word", height=18)
    log_text.grid(row=0, column=0, sticky="nsew")
    log_scroll = ttk.Scrollbar(log_frame, orient="vertical", command=log_text.yview)
    log_scroll.grid(row=0, column=1, sticky="ns")
    log_text.configure(yscrollcommand=log_scroll.set)

    def log(msg=""):
        log_text.insert(tk.END, str(msg) + "\n")
        log_text.see(tk.END)
        root.update_idletasks()

    def run_all():
        asc_file = asc_var.get().strip()
        qsch_file = qsch_var.get().strip()

        if not asc_file or not os.path.isfile(asc_file):
            messagebox.showerror("Missing file", "Please select a valid .asc file.", parent=root)
            return
        if not qsch_file:
            messagebox.showerror("Missing file", "Please select an output .qsch file.", parent=root)
            return

        step1_qsch_file, diff_file = _build_intermediate_paths(qsch_file)

        # Clean up any old intermediate files from a previous run so the comparison is clear.
        for old_path in (step1_qsch_file, diff_file):
            try:
                if os.path.exists(old_path):
                    os.remove(old_path)
            except OSError:
                pass

        log_text.delete("1.0", tk.END)
        log("=" * 60)
        log("STEP 1/2: Converting .asc -> intermediate .qsch")
        log("=" * 60)
        log(f"ASC:         {asc_file}")
        log(f"Step 1 .QSCH: {step1_qsch_file}")
        log(f"Final .QSCH:  {qsch_file}")
        log("")

        try:
            step1.convert_asc_to_qsch(
                asc_file,
                step1_qsch_file,
                search_paths=step1._normalize_paths(custom_paths),
                log=log,
            )
        except Exception:
            import traceback

            tb = traceback.format_exc()

            log("")
            log(tb)

            messagebox.showerror(
                "Conversion failed",
                tb,
                parent=root,
            )
            return

        # Copy the raw converted file to the final target first, then step 2 edits that copy.
        try:
            shutil.copy2(step1_qsch_file, qsch_file)
        except Exception as exc:
            log("")
            log(f"ERROR copying intermediate output to final file: {exc}")
            messagebox.showerror("Copy failed", str(exc), parent=root)
            return

        log("")
        log("=" * 60)
        log("STEP 2/2: Fixing & importing missing models into the final .qsch")
        log("=" * 60)
        log("")

        search_roots = step2.DEFAULT_SEARCH_ROOTS if auto_search_var.get() else []

        def chooser(model, found):
            return step2.choose_model_path_gui(model, found, root)

        def device_chooser(model, found, refs):
            return step2.choose_device_model_gui(model, found, refs, root)

        try:
            step2.process_models(
                asc_file=asc_file,
                qsch_file=qsch_file,
                search_roots=search_roots,
                choose_model_path=chooser,
                log=log,
                fix_annotations=fix_annotations_var.get(),
                replace_zero_ohm=replace_zero_ohm_var.get(),
                choose_device_model=device_chooser if device_model_fix_var.get() else None,
            )
        except Exception as exc:
            log("")
            log(f"ERROR during model fix-up: {exc}")
            messagebox.showerror("Model fix-up failed", str(exc), parent=root)
            return

        diff_lines = _write_qsch_diff(step1_qsch_file, qsch_file, diff_file)
        if diff_lines > 0:
            log("")
            log("=" * 60)
            log("COMPARE RESULT")
            log("=" * 60)
            log(f"Saved raw conversion: {step1_qsch_file}")
            log(f"Saved final output:   {qsch_file}")
            log(f"Saved diff report:     {diff_file}")
            log(f"Diff lines written:    {diff_lines}")
        else:
            log("")
            log("No diff report was written (or the files could not be compared).")

        log("")
        log("=" * 60)
        log("ALL DONE.")
        log("=" * 60)
        messagebox.showinfo(
            "Done",
            f"Finished!\nSaved final: {qsch_file}\nSaved step 1: {step1_qsch_file}",
            parent=root,
        )

    action_row = ttk.Frame(main)
    action_row.grid(row=3, column=0, sticky="w", pady=(10, 0))
    ttk.Button(action_row, text="Run both steps", command=run_all).pack(side="left")
    ttk.Button(action_row, text="Quit", command=root.destroy).pack(side="left", padx=(8, 0))

    hint = ttk.Label(
        main,
        text=("Pick your .asc, adjust options if needed, then click 'Run both steps'.\n"
              "This converts to .qsch AND fixes/imports missing models in a single pass."),
        wraplength=860,
        foreground="#555555",
    )
    hint.grid(row=4, column=0, sticky="w", pady=(10, 0))

    root.mainloop()


def main():
    try:
        run_gui()
    except Exception as exc:
        _fatal_error_box("Startup Error", exc)
        sys.exit(1)


if __name__ == "__main__":
    main()
