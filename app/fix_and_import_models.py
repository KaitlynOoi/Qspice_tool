#!/usr/bin/env python3
"""
fix_and_import_models_gui_safe_v5.py (Full Fixed Version)
Goals:
  - Extract actual component values/part numbers from .asc SYMATTR Value.
  - Preserve the original conversion/import behavior.
  - GUI for choosing the .asc, .qsch, and matching .cir/.lib files.
  - Repair pass for plain text annotations/hidden text that should be comments.
  - Normalize malformed .lib instructions so there are no stray spaces inside path.
  - Replace 0-ohm resistors with wire connections in the QSCH tree.
  - Universally detect both discrete primitives (.model) and subcircuit-backed
    FETs/diodes (such as SiS414DN, SiS447DN, and custom transistors).
"""
from __future__ import annotations
import argparse
import os
import re
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional, Set, Tuple

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

BUILTIN_PRIMITIVES = {
    "res", "cap", "ind", "voltage", "current", "diode", "schottky",
    "npn", "pnp", "nmos", "pmos", "zener", "polcap", "1n4148", "2n2222"
}

DEFAULT_SEARCH_ROOTS = [
    os.path.expanduser("~/AppData/Local/LTspice/lib"),
    os.path.expanduser("~/Documents/LtspiceXVII/lib"),
]

ZERO_OHM_TOLERANCE = 1e-15


def _patch_spicelib_qsch_colon_parsing(log: Callable[[str], None] = print) -> bool:
    """Fix a parsing bug in spicelib's QschTag.parse()."""
    try:
        import spicelib.editor.qsch_editor as _qsch_mod
    except ImportError:
        log("  NOTE: spicelib not importable yet; skipping QSCH parser patch.")
        return False
    QschTag = getattr(_qsch_mod, "QschTag", None)
    smart_split = getattr(_qsch_mod, "smart_split", None)
    if QschTag is None or smart_split is None:
        log("  NOTE: spicelib internals changed shape; skipping QSCH parser patch.")
        return False
    if getattr(QschTag, "_colon_quote_patch_applied", False):
        return True

    @classmethod
    def _patched_parse(cls, stream: str, start: int = 0):
        self = cls()
        assert stream[start] == '«'
        i = start + 1
        i0 = i
        stop = None
        while i < len(stream):
            if stream[i] == '«':
                child, i = cls.parse(stream, i)
                i0 = i + 1
                self.items.append(child)
            elif stream[i] == '"':
                i += 1
                while stream[i] != '"':
                    i += 1
            elif stream[i] == '»':
                stop = i + 1
                break
            elif stream[i] == '\n':
                if i > i0:
                    self.tokens.extend(smart_split(stream[i0:i]))
                i0 = i + 1
            i += 1
        else:
            raise OSError("Missing » when reading file")
        line = stream[i0:i]
        if ': ' in line and '"' not in line:
            name, text = line.split(': ', 1)
            self.tokens.append(name + ":")
            self.tokens.append(text)
        else:
            self.tokens.extend(smart_split(line))
        return self, stop

    try:
        QschTag.parse = _patched_parse
        QschTag._colon_quote_patch_applied = True
    except Exception as exc:
        log(f"  WARNING: could not patch spicelib QSCH parser: {exc}")
        return False
    log("  Patched spicelib QSCH parser (colon-inside-quotes annotation bug).")
    return True


_patch_spicelib_qsch_colon_parsing(log=lambda _msg: None)


def _clean_path(path: str) -> str:
    """Remove accidental whitespace/quotes and normalize path separators."""
    cleaned = str(path).replace(' ', ' ').strip()
    cleaned = cleaned.strip('"').strip("'").strip()
    cleaned = re.sub(r'\s+', ' ', cleaned) if '\n' not in cleaned else cleaned
    return os.path.normpath(cleaned)


def _is_zero_ohm_value(value: object) -> bool:
    """Best-effort detection of a literal 0-ohm resistor value."""
    if value is None:
        return False
    if isinstance(value, (int, float)):
        return abs(float(value)) <= ZERO_OHM_TOLERANCE
    text = str(value).strip().replace("Ω", "").replace("ohm", "").replace("OHM", "")
    if not text:
        return False
    try:
        return abs(float(text)) <= ZERO_OHM_TOLERANCE
    except ValueError:
        return False


def parse_asc_ref_to_symbol(asc_path: str) -> Dict[str, str]:
    """Returns dict: {reference_designator: part_value_or_symbol}"""
    with open(asc_path, encoding="cp1252", errors="replace") as f:
        data = f.read()
    mapping: Dict[str, str] = {}
    blocks = re.split(r"(?=^SYMBOL )", data, flags=re.MULTILINE)
    for block in blocks:
        if not block.startswith("SYMBOL"):
            continue
        symtype = block.split()[1]
        
        # Grab reference designator (e.g., M1, Q1, D1)
        ref_match = re.search(r"SYMATTR InstName (\S+)", block)
        if not ref_match:
            continue
        ref = ref_match.group(1)
        
        # Grab actual part name/value (e.g., SiS414DN, SiS447DN, 2N3904)
        val_match = re.search(r"SYMATTR Value (\S+)", block)
        if val_match:
            mapping[ref] = val_match.group(1)
        else:
            mapping[ref] = symtype
            
    return mapping


def parse_qsch_x_type_refs(qsch_path: str) -> set[str]:
    """Returns reference designators whose component block is type X."""
    with open(qsch_path, encoding="cp1252", errors="replace") as f:
        data = f.read()
    x_refs: set[str] = set()
    comp_blocks = re.split(r"(?=«component )", data)
    ref_text_pattern = re.compile(r'«text \([^)]*\) 1 7 0 0x1000000 -1 -1 "([^"]+)"»')
    for block in comp_blocks:
        if not block.startswith("«component"):
            continue
        if "«type: X»" not in block:
            continue
        texts = ref_text_pattern.findall(block)
        if texts:
            x_refs.add(texts[0])
    return x_refs


def _attr(obj: object, *names: str, default=None):
    """Best-effort attribute lookup across a spicelib component's attributes."""
    attrs = getattr(obj, 'attributes', None) or {}
    for name in names:
        if isinstance(attrs, dict) and name in attrs:
            val = attrs[name]
            if val not in (None, ''):
                return val
    for name in names:
        if hasattr(obj, name):
            val = getattr(obj, name)
            if val not in (None, ''):
                return val
    return default


def bare_model_name(symtype: str) -> str:
    """Opamps\\LTC6244HV -> LTC6244HV"""
    return symtype.split("\\")[-1]


def search_for_model(model_name: str, search_roots: Iterable[str]) -> Optional[str]:
    pattern = re.compile(
        r"^\s*\.(subckt|model)\s+" + re.escape(model_name) + r"\b",
        re.IGNORECASE | re.MULTILINE,
    )
    for root in search_roots:
        if not root or not os.path.isdir(root):
            continue
        for dirpath, _, filenames in os.walk(root):
            for fname in filenames:
                if not fname.lower().endswith((".lib", ".cir", ".txt", ".sub", ".mod")):
                    continue
                fpath = os.path.join(dirpath, fname)
                try:
                    with open(fpath, encoding="cp1252", errors="replace") as f:
                        content = f.read()
                except (IOError, OSError):
                    continue
                if pattern.search(content):
                    return fpath
    return None


def _inject_raw_instruction(qsch_editor, instruction: str,
                             log: Callable[[str], None] = print) -> None:
    """Safely add ANY single-line SPICE instruction/directive to a QSCH file."""
    from spicelib.editor.qsch_editor import QschTag, QSCH_TEXT_INSTR_QUALIFIER
    instruction = instruction.strip()
    x, y = qsch_editor._get_text_space()
    tag = QschTag()
    tag.tokens = [
        "text", f"({x},{y})", "1", "0", "0", "0x1000000", "-1", "-1",
        f'"{QSCH_TEXT_INSTR_QUALIFIER}{instruction}"',
    ]
    qsch_editor.schematic.items.append(tag)
    qsch_editor.canvas_updated = True
    log(f'  Injected (safe): {instruction}')


def _inject_lib_instruction(qsch_editor, path: str, kind: str = "lib",
                             log: Callable[[str], None] = print) -> None:
    """Safely add a '.lib "path"' / '.include "path"' instruction to a QSCH file."""
    clean_path = _clean_path(path)
    instruction = f'.{kind} "{clean_path}"'
    _inject_raw_instruction(qsch_editor, instruction, log=log)


DEVICE_MODEL_TYPE_EXPECTATIONS: Dict[str, set] = {
    "D": {"D"},
    "QN": {"NPN"},
    "QP": {"PNP"},
    "NMOS": {"NMOS", "VDMOS"},
    "PMOS": {"PMOS", "VDMOS"},
    "NJF": {"NJF"},
    "PJF": {"PJF"},
}

PASSIVE_QSCH_TYPES = {"R", "C", "L", "V", "I", "K"}


def _read_text_file_robust(file_path: str) -> Optional[List[str]]:
    """Read a text file's lines with fallback encoding checks."""
    for encoding in ("utf-8-sig", "cp1252"):
        try:
            with open(file_path, encoding=encoding, errors="strict") as f:
                return f.readlines()
        except (UnicodeDecodeError, LookupError):
            continue
        except (IOError, OSError):
            return None
    try:
        with open(file_path, encoding="cp1252", errors="replace") as f:
            return f.readlines()
    except (IOError, OSError):
        return None


def _extract_model_definition(
    file_path: str, model_name: str, log: Optional[Callable[[str], None]] = None
) -> Optional[str]:
    """Pull the exact '.model' card or '.subckt ... .ends' definition out of a file."""
    lines = _read_text_file_robust(file_path)
    if not lines:
        return None
    
    # 1. Try extracting a .model definition first
    model_re = re.compile(r'^\s*\.model\s+' + re.escape(model_name) + r'\b', re.IGNORECASE)
    idx = 0
    while idx < len(lines):
        line = lines[idx]
        if not model_re.match(line):
            idx += 1
            continue
        collected = [line.strip()]
        depth = line.count('(') - line.count(')')
        j = idx + 1
        while j < len(lines) and (depth > 0 or lines[j].lstrip().startswith('+')):
            nxt = lines[j]
            collected.append(nxt.strip())
            depth += nxt.count('(') - nxt.count(')')
            j += 1
        last = collected[-1]
        close_idx = last.rfind(')')
        if close_idx != -1:
            remainder = last[close_idx + 1:].strip()
            if remainder.startswith(';') or remainder.startswith('*'):
                collected[-1] = last[:close_idx + 1]
        return '\n'.join(collected)

    # 2. Try extracting a .subckt definition if .model wasn't found
    subckt_re = re.compile(r'^\s*\.subckt\s+' + re.escape(model_name) + r'\b', re.IGNORECASE)
    ends_re = re.compile(r'^\s*\.ends\b', re.IGNORECASE)
    idx = 0
    while idx < len(lines):
        line = lines[idx]
        if not subckt_re.match(line):
            idx += 1
            continue
        collected = [line.strip()]
        j = idx + 1
        while j < len(lines):
            nxt = lines[j]
            collected.append(nxt.strip())
            if ends_re.match(nxt):
                break
            j += 1
        return '\n'.join(collected)

    return None


def parse_qsch_primitive_device_refs(
    qsch_editor, log: Callable[[str], None] = print
) -> Dict[str, List[tuple]]:
    """Find missing component models universally."""
    refs_by_model: Dict[str, List[tuple]] = defaultdict(list)
    try:
        components = list(getattr(qsch_editor, 'components', {}).items())
    except Exception as exc:
        log(f"  WARNING: could not enumerate components: {exc}")
        return refs_by_model

    for refdes, comp in components:
        comp_type = str(_attr(comp, 'type', 'Type', 'symbol_type', default='')).strip().upper()
        if comp_type in PASSIVE_QSCH_TYPES:
            continue
            
        ref = str(_attr(comp, 'reference', 'refdes', 'InstName', default=refdes)).strip() or str(refdes)
        value = _attr(comp, 'value', 'Value', default=None)
        if value is None:
            continue
            
        model_name = str(value).strip().split()[0]
        if not model_name or model_name.lower() in BUILTIN_PRIMITIVES:
            continue
            
        refs_by_model[model_name].append((ref, comp_type))
    return refs_by_model


def _existing_model_definitions(qsch_editor) -> Dict[str, str]:
    """Scan instructions already in the QSCH tree for defined .model/.subckt cards."""
    from spicelib.editor.qsch_editor import QSCH_TEXT_INSTR_QUALIFIER, QSCH_TEXT_STR_ATTR
    found: Dict[str, str] = {}
    model_re = re.compile(r'^\s*\.(model|subckt)\s+(\S+)\s*(\w+)?', re.IGNORECASE)
    for tag in qsch_editor.schematic.get_items('text'):
        try:
            content = tag.get_attr(QSCH_TEXT_STR_ATTR)
        except Exception:
            continue
        if not isinstance(content, str):
            continue
        if content.startswith(QSCH_TEXT_INSTR_QUALIFIER):
            content = content[len(QSCH_TEXT_INSTR_QUALIFIER):]
        m = model_re.match(content)
        if m:
            m_name = m.group(2)
            m_type = m.group(3).upper() if m.group(3) else "SUBCKT"
            found[m_name] = m_type
    return found


def _parse_model_lines(raw_text: str) -> List[tuple]:
    """Parse pasted-in SPICE text into printable instruction lines."""
    model_re = re.compile(r'^\s*\.model\s+(\S+)\s+(\w+)', re.IGNORECASE)
    results = []
    for raw_line in raw_text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        m = model_re.match(line)
        if m:
            results.append((line, m.group(1), m.group(2).upper()))
        else:
            results.append((line, None, None))
    return results


def validate_and_inject_device_model(
    qsch_editor, model_name: str, raw_text: str,
    refs: List[tuple], log: Callable[[str], None] = print
) -> int:
    """Inject one or more pasted '.model'/'.subckt' lines into the QSCH file."""
    lines = _parse_model_lines(raw_text)
    if not lines:
        log(f"  WARNING: no non-blank lines to inject for {model_name}; skipping.")
        return 0
    injected = 0
    for line, parsed_name, parsed_type in lines:
        if parsed_name is not None and parsed_type is not None:
            for ref, qsch_type in refs:
                exp = DEVICE_MODEL_TYPE_EXPECTATIONS.get(qsch_type)
                if exp and parsed_type not in exp:
                    log(
                        f'  WARNING: {ref} is placed as {qsch_type} '
                        f'(expects {"/".join(sorted(exp))}), but the model '
                        f'supplied for "{model_name}" is {parsed_type}.'
                    )
        _inject_raw_instruction(qsch_editor, line, log=log)
        injected += 1
    return injected


def _normalize_lib_tags(qsch_editor, log: Callable[[str], None] = print) -> int:
    """Clean up '.lib'/'.include' instructions already living in the QSCH tree."""
    changed = 0
    instr_re = re.compile(r'^(?P<qual>.*?)\.(?P<kind>lib|include)\s+(?P<rest>.*)$', re.IGNORECASE)
    for tag in qsch_editor.schematic.get_items('text'):
        tokens = tag.tokens
        if len(tokens) < 9:
            continue
        if len(tokens) == 9 and tokens[8].startswith('"') and tokens[8].endswith('"'):
            content = tokens[8][1:-1]
            m = instr_re.match(content)
            if not m:
                continue
            qual, kind, rest = m.group('qual'), m.group('kind').lower(), m.group('rest').strip()
            rest = rest.strip('"').strip("'").strip()
            cleaned = _clean_path(rest)
            new_instruction = f'.{kind} "{cleaned}"'
            new_token = f'"{qual}{new_instruction}"'
            if new_token != tokens[8]:
                tokens[8] = new_token
                changed += 1
                log(f'  Normalized: {new_instruction}')
            continue
        head = tokens[8]
        m = re.match(r'^"(?P<qual>.*?)\.(?P<kind>lib|include)\s*"$', head, re.IGNORECASE)
        if not m:
            continue
        qual, kind = m.group('qual'), m.group('kind').lower()
        tail = tokens[-1]
        path_tokens = tokens[9:-1] if tail in ('""', '"') else tokens[9:]
        raw_path = ' '.join(path_tokens)
        cleaned = _clean_path(raw_path)
        new_instruction = f'.{kind} "{cleaned}"'
        tag.tokens = tokens[:8] + [f'"{qual}{new_instruction}"']
        changed += 1
        log(f'  Repaired corrupted instruction: {new_instruction}')
    return changed


def _remove_tag_from_tree(root_tag, target_tag) -> bool:
    """Recursively remove a tag object from a QschTag tree by identity."""
    items = getattr(root_tag, "items", None)
    if not items:
        return False
    for idx, child in enumerate(list(items)):
        if child is target_tag:
            del items[idx]
            return True
        if _remove_tag_from_tree(child, target_tag):
            return True
    return False


def _next_wire_name(qsch_editor) -> str:
    """Generate a net name that does not collide with existing N## names."""
    max_no = 0
    def scan_name(name: object) -> None:
        nonlocal max_no
        if not isinstance(name, str):
            return
        m = re.fullmatch(r"N(\d+)", name.strip())
        if m:
            max_no = max(max_no, int(m.group(1)))
    try:
        for wire in qsch_editor.schematic.get_items('wire'):
            try:
                scan_name(wire.get_attr(3))
            except Exception:
                pass
        for net in qsch_editor.schematic.get_items('net'):
            try:
                scan_name(net.get_attr(5))
            except Exception:
                pass
    except Exception:
        pass
    return f"N{max_no + 1:02d}"


def replace_zero_ohm_resistors_with_wires(qsch_editor, log: Callable[[str], None] = print) -> int:
    """Replace literal 0-ohm resistors with wires in the QSCH tree."""
    from spicelib.editor.qsch_editor import QschTag
    from spicelib.editor.base_schematic import Point, Line
    def _point_xy(pos: object):
        if pos is None:
            raise ValueError('missing component position')
        if isinstance(pos, tuple) and len(pos) >= 2:
            return int(pos[0]), int(pos[1])
        for ax_x, ax_y in (('X', 'Y'), ('x', 'y')):
            if hasattr(pos, ax_x) and hasattr(pos, ax_y):
                return int(getattr(pos, ax_x)), int(getattr(pos, ax_y))
        raise ValueError(f'unrecognized position object: {pos!r}')
    replaced = 0
    to_remove: list[str] = []
    wires_to_add: list[tuple[int, int, int, int, str]] = []
    components = list(getattr(qsch_editor, 'components', {}).items())
    for refdes, comp in components:
        ref = str(_attr(comp, 'reference', 'refdes', 'InstName', default=refdes)).strip() or str(refdes)
        comp_type = str(_attr(comp, 'type', 'Type', 'symbol_type', default='')).strip().upper()
        value = _attr(comp, 'value', 'Value', default=None)
        if comp_type != 'R' and not ref.upper().startswith('R'):
            continue
        if not _is_zero_ohm_value(value):
            continue
        component_tag = _attr(comp, 'tag', default=None)
        symbol_tag = None
        if component_tag is not None:
            try:
                symbol_items = component_tag.get_items('symbol')
                if symbol_items:
                    symbol_tag = symbol_items[0]
            except Exception:
                symbol_tag = None
        if symbol_tag is None:
            log(f'  WARNING: {ref} is 0 ohm but has no symbol geometry; skipping wire replacement.')
            continue
        try:
            pins = list(symbol_tag.get_items('pin'))
        except Exception:
            pins = []
        if len(pins) < 2:
            log(f'  WARNING: {ref} is 0 ohm but has fewer than 2 pins; skipping wire replacement.')
            continue
        try:
            position = _attr(comp, 'position', 'pos', default=None)
            rot = _attr(comp, 'rotation', 'rot', default=0)
            rot_val = int(getattr(rot, 'value', rot))
            orientation = (rot_val // 45) % 8
            px, py = _point_xy(position)
            p1 = qsch_editor._find_pin_position((px, py), orientation, pins[0])
            p2 = qsch_editor._find_pin_position((px, py), orientation, pins[1])
        except Exception as exc:
            log(f'  WARNING: could not derive pin positions for {ref}: {exc}')
            continue
        net1 = None
        net2 = None
        try:
            net1 = qsch_editor._find_net_at_position(*p1)
        except Exception:
            pass
        try:
            net2 = qsch_editor._find_net_at_position(*p2)
        except Exception:
            pass
        wire_net = net1 or net2 or _next_wire_name(qsch_editor)
        wires_to_add.append((int(p1[0]), int(p1[1]), int(p2[0]), int(p2[1]), wire_net))
        to_remove.append(refdes)
        replaced += 1
        log(f'  Replaced 0-ohm resistor {ref} with wire {wire_net}')
    for refdes in to_remove:
        comp = qsch_editor.components.get(refdes)
        if comp is None:
            continue
        tag = _attr(comp, 'tag', 'qsch_tag', default=None)
        if tag is not None:
            removed = _remove_tag_from_tree(qsch_editor.schematic, tag)
            if not removed:
                log(f'  WARNING: could not remove {refdes} from schematic tree cleanly.')
        qsch_editor.components.pop(refdes, None)
    for x1, y1, x2, y2, net in wires_to_add:
        wire_tag, _ = QschTag.parse(f'«wire ({x1},{y1}) ({x2},{y2}) "{net}"»')
        qsch_editor.schematic.items.append(wire_tag)
        try:
            qsch_editor.wires.append(Line(Point(x1, y1), Point(x2, y2), net=net))
        except Exception:
            pass
        qsch_editor.canvas_updated = True
    return replaced


@dataclass
class ProcessingResult:
    fixed_count: int
    injected_count: int
    skipped_models: List[str]
    resolved_paths: Dict[str, str]
    reclassified_count: int = 0
    replaced_zero_ohm_count: int = 0
    device_models_injected: int = 0
    device_models_skipped: List[str] = field(default_factory=list)


def fix_misclassified_comments(qsch_editor, log: Callable[[str], None] = print) -> int:
    """Reclassify plain text annotations as comments."""
    from spicelib.editor.qsch_editor import (
        QSCH_TEXT_COMMENT,
        QSCH_TEXT_INSTR_QUALIFIER,
        QSCH_TEXT_STR_ATTR,
    )
    fixed = 0
    text_tags = qsch_editor.schematic.get_items("text")
    for tag in text_tags:
        try:
            if tag.get_attr(QSCH_TEXT_COMMENT) == 1:
                continue
            content = tag.get_attr(QSCH_TEXT_STR_ATTR)
        except Exception:
            continue
        if not isinstance(content, str):
            continue
        stripped = content
        if stripped.startswith(QSCH_TEXT_INSTR_QUALIFIER):
            stripped = stripped[len(QSCH_TEXT_INSTR_QUALIFIER):]
        stripped = stripped.strip()
        if stripped and not stripped.startswith('.'):
            tag.set_attr(QSCH_TEXT_COMMENT, 1)
            fixed += 1
            preview = stripped[:80] + ('...' if len(stripped) > 80 else '')
            log(f'  Reclassified as comment: "{preview}"')
    if fixed:
        qsch_editor.canvas_updated = True
    return fixed


def process_models(
    asc_file: str,
    qsch_file: str,
    search_roots: List[str],
    choose_model_path: Callable[[str, Optional[str]], Optional[str]],
    log: Callable[[str], None] = print,
    fix_annotations: bool = True,
    replace_zero_ohm: bool = True,
    choose_device_model: Optional[Callable[[str, Optional[str], List[tuple]], Optional[dict]]] = None,
) -> ProcessingResult:
    _patch_spicelib_qsch_colon_parsing(log=log)
    asc_map = parse_asc_ref_to_symbol(asc_file)
    x_refs = parse_qsch_x_type_refs(qsch_file)
    missing_by_model: Dict[str, List[str]] = defaultdict(list)
    for ref in x_refs:
        symtype = asc_map.get(ref)
        if symtype is None:
            continue
        bare = bare_model_name(symtype)
        if bare.lower() in BUILTIN_PRIMITIVES:
            continue
        missing_by_model[bare].append(ref)

    if not missing_by_model:
        log("No unresolved subcircuit components found via ASC map.")

    log("=" * 60)
    log("COMPONENTS NEEDING VALUE FIX + MODEL IMPORT")
    log("=" * 60)
    for model, refs in missing_by_model.items():
        log(f"  {model}  ({len(refs)} component(s): {', '.join(sorted(refs))})")
    log("")
    resolved_paths: Dict[str, str] = {}
    for model in sorted(missing_by_model.keys()):
        refs = missing_by_model[model]
        log(f"--- {model} (used by {len(refs)} component(s)) ---")
        found = search_for_model(model, search_roots)
        chosen = choose_model_path(model, found)
        if chosen:
            chosen = _clean_path(chosen)
            resolved_paths[model] = chosen
            log(f"  Selected: {chosen}")
        else:
            log(
                f"  SKIPPED -- {model} value will still be fixed, "
                f"but no .lib will be imported for it."
            )
    from spicelib.editor.qsch_editor import QschEditor
    qsch_editor = QschEditor(qsch_file)
    fixed_count = 0
    for model, refs in missing_by_model.items():
        for ref in refs:
            try:
                qsch_editor.set_component_value(ref, model)
                fixed_count += 1
            except Exception as e:
                log(f"  WARNING: could not set value for {ref}: {e}")
    injected_count = 0
    for model, path in resolved_paths.items():
        path = _clean_path(path)
        log(f'Injecting: .lib "{path}"')
        _inject_lib_instruction(qsch_editor, path, kind="lib", log=log)
        injected_count += 1

    device_model_refs = parse_qsch_primitive_device_refs(qsch_editor, log=log)
    existing_models = _existing_model_definitions(qsch_editor)
    unresolved_device_models = {
        name: refs for name, refs in device_model_refs.items()
        if name not in existing_models and name not in resolved_paths
    }
    device_models_injected = 0
    device_models_skipped: List[str] = []
    log("")
    log("=" * 60)
    log("UNRESOLVED COMPONENT MODELS (Diodes / BJTs / MOSFETs / Subcircuits)")
    log("=" * 60)
    if not unresolved_device_models:
        log("  None found -- every component model is already satisfied.")
    else:
        for model in sorted(unresolved_device_models.keys()):
            refs = unresolved_device_models[model]
            ref_desc = ", ".join(f"{r} ({t})" for r, t in refs)
            log(f"  {model}  ({len(refs)} component(s): {ref_desc})")
        log("")
        if choose_device_model is None:
            log("  No device-model chooser was provided; skipping this step.")
            device_models_skipped = sorted(unresolved_device_models.keys())
        else:
            for model in sorted(unresolved_device_models.keys()):
                refs = unresolved_device_models[model]
                log(f"--- {model} (used by {len(refs)} component(s)) ---")
                found = search_for_model(model, search_roots)
                choice = choose_device_model(model, found, refs)
                if not choice:
                    log(f"  SKIPPED -- no definition assigned for {model}.")
                    device_models_skipped.append(model)
                    continue
                text = choice.get("inline") if isinstance(choice, dict) else choice
                path_file = choice.get("path") if isinstance(choice, dict) else None
                
                if text:
                    n = validate_and_inject_device_model(
                        qsch_editor, model, text, refs, log=log
                    )
                    if n:
                        device_models_injected += 1
                    else:
                        device_models_skipped.append(model)
                elif path_file:
                    _inject_lib_instruction(qsch_editor, path_file, kind="lib", log=log)
                    device_models_injected += 1
                else:
                    device_models_skipped.append(model)

    reclassified_count = 0
    if fix_annotations:
        log("")
        log("Checking for plain text annotations that should be comments...")
        try:
            reclassified_count = fix_misclassified_comments(qsch_editor, log=log)
        except Exception as e:
            log(f"  WARNING: annotation cleanup failed: {e}")
            reclassified_count = 0
        if reclassified_count == 0:
            log("  None found -- nothing to fix.")
    replaced_zero_ohm_count = 0
    if replace_zero_ohm:
        log("")
        log("Replacing literal 0-ohm resistors with wires...")
        try:
            replaced_zero_ohm_count = replace_zero_ohm_resistors_with_wires(qsch_editor, log=log)
        except Exception as e:
            log(f"  WARNING: zero-ohm replacement failed: {e}")
            replaced_zero_ohm_count = 0
        if replaced_zero_ohm_count == 0:
            log("  None found -- nothing to replace.")
    log("")
    log("Normalizing .lib/.include instructions...")
    normalized_count = _normalize_lib_tags(qsch_editor, log=log)
    if normalized_count == 0:
        log("  None needed normalizing.")
    qsch_editor.save_netlist(qsch_file)
    log("")
    log(
        f"Done. {fixed_count} component value(s) corrected, "
        f"{injected_count} .lib instruction(s) injected, "
        f"{device_models_injected} device model/subcircuit definition(s) injected, "
        f"{reclassified_count} annotation(s) reclassified as comments, "
        f"{replaced_zero_ohm_count} zero-ohm resistor(s) replaced with wires "
        f"in {qsch_file}"
    )
    skipped = sorted(set(missing_by_model.keys()) - set(resolved_paths.keys()))
    if skipped:
        log(f"Still no .lib imported for: {', '.join(skipped)}")
    if device_models_skipped:
        log(f"Still no model defined for: {', '.join(sorted(set(device_models_skipped)))}")
    return ProcessingResult(
        fixed_count=fixed_count,
        injected_count=injected_count,
        skipped_models=skipped,
        resolved_paths=resolved_paths,
        reclassified_count=reclassified_count,
        replaced_zero_ohm_count=replaced_zero_ohm_count,
        device_models_injected=device_models_injected,
        device_models_skipped=sorted(set(device_models_skipped)),
    )


def choose_model_path_cli(model: str, found: Optional[str]) -> Optional[str]:
    if found:
        print(f"  Auto-found: {found}")
        answer = input("  Use this file? [Y/n/other path]: ").strip()
        if answer == "" or answer.lower() == "y":
            return found
        if answer.lower() != "n":
            return answer
    manual = input(
        f"  Enter path to .lib/.cir file containing '{model}' (or leave blank to skip): "
    ).strip()
    return manual or None


def choose_model_path_gui(model: str, found: Optional[str], root) -> Optional[str]:
    from tkinter import filedialog, messagebox
    if found:
        use_found = messagebox.askyesnocancel(
            "Model found",
            f"Auto-found a file for {model}:\n\n{found}\n\n"
            f"Yes = use this file\nNo = choose another file\nCancel = skip",
            parent=root,
        )
        if use_found is True:
            return found
        if use_found is None:
            return None
    initialdir = os.path.dirname(found) if found else os.path.expanduser("~")
    chosen = filedialog.askopenfilename(
        parent=root,
        title=f"Select .cir/.lib file for {model}",
        initialdir=initialdir,
        filetypes=[
            ("SPICE library files", "*.lib *.cir *.sub *.txt *.mod"),
            ("All files", "*.*"),
        ],
    )
    return chosen or None


def choose_device_model_cli(
    model: str, found: Optional[str], refs: List[tuple]
) -> Optional[dict]:
    """CLI chooser for discrete devices and subcircuits."""
    ref_desc = ", ".join(f"{r} ({t})" for r, t in refs)
    print(f"  Used by: {ref_desc}")
    if found:
        print(f"  Auto-found: {found}")
        extracted = _extract_model_definition(found, model, log=print)
        if extracted:
            print(f"  Extracted this definition from it:")
            print("    " + extracted.replace("\n", "\n    "))
            answer = input("  Inject this definition directly? [Y/n]: ").strip().lower()
            if answer in ("", "y"):
                return {"inline": extracted}
        else:
            answer = input(f"  Include full library file reference '{found}'? [Y/n]: ").strip().lower()
            if answer in ("", "y"):
                return {"path": found}

    print(
        f"  Type or paste the .model / .subckt line(s) for '{model}' directly.\n"
        f"  (leave blank to skip)"
    )
    lines = []
    while True:
        line = input("    ")
        if not line.strip():
            break
        lines.append(line)
    text = "\n".join(lines).strip()
    return {"inline": text} if text else None


def run_cli(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("asc_file")
    parser.add_argument("qsch_file")
    parser.add_argument("--auto-search", action="append", default=[])
    parser.add_argument("--yes-to-auto", action="store_true")
    parser.add_argument("--no-annotation-fix", action="store_true",
                        help="Do not reclassify plain text annotations as comments.")
    parser.add_argument("--no-zero-ohm-replacement", action="store_true",
                        help="Do not replace 0-ohm resistors with wires.")
    parser.add_argument("--no-device-model-fix", action="store_true",
                        help="Do not prompt for missing component model definitions.")
    args = parser.parse_args(argv)
    search_roots = DEFAULT_SEARCH_ROOTS + args.auto_search
    def chooser(model: str, found: Optional[str]) -> Optional[str]:
        if found:
            print(f"  Auto-found: {found}")
            if args.yes_to_auto:
                return found
        return choose_model_path_cli(model, found)
    device_chooser = None if args.no_device_model_fix else choose_device_model_cli
    process_models(
        asc_file=args.asc_file,
        qsch_file=args.qsch_file,
        search_roots=search_roots,
        choose_model_path=chooser,
        log=print,
        fix_annotations=not args.no_annotation_fix,
        replace_zero_ohm=not args.no_zero_ohm_replacement,
        choose_device_model=device_chooser,
    )
    return 0


def choose_device_model_gui(
    model: str, found: Optional[str], refs: List[tuple], root
) -> Optional[dict]:
    """GUI dialog for resolving unresolved models and subcircuits."""
    import tkinter as tk
    from tkinter import filedialog, ttk

    dialog = tk.Toplevel(root)
    dialog.title(f"Resolve model: {model}")
    dialog.geometry("620x520")
    dialog.transient(root)
    dialog.grab_set()

    result: dict = {}

    frame = ttk.Frame(dialog, padding=12)
    frame.pack(fill="both", expand=True)

    ref_desc = ", ".join(f"{r} ({t})" for r, t in refs)
    ttk.Label(
        frame,
        text=f'No definition found for "{model}"',
        font=("", 11, "bold"),
    ).pack(anchor="w")
    ttk.Label(frame, text=f"Used by: {ref_desc}", wraplength=580).pack(anchor="w", pady=(2, 12))

    file_box = ttk.LabelFrame(
        frame,
        text="Point to a .lib/.cir/.mod file",
        padding=8,
    )
    file_box.pack(fill="x", pady=(0, 10))
    file_var = tk.StringVar(value=found or "")
    file_entry = ttk.Entry(file_box, textvariable=file_var)
    file_entry.pack(fill="x", pady=(4, 4), side="left", expand=True)

    def _try_extract(path: str) -> None:
        path = path.strip()
        if not path or not os.path.isfile(path):
            return
        notes: List[str] = []
        extracted = _extract_model_definition(path, model, log=notes.append)
        text_widget.delete("1.0", "end")
        if extracted:
            text_widget.insert("1.0", extracted)
            status_label.config(text=f'Extracted definition from {os.path.basename(path)}')
        else:
            status_label.config(
                text=f'No explicit definition for {model} found in {os.path.basename(path)} -- paste manually below.'
            )

    def browse_file() -> None:
        chosen = filedialog.askopenfilename(
            parent=dialog,
            title=f"Select file defining '{model}'",
            filetypes=[("SPICE library files", "*.lib *.cir *.sub *.mod *.txt"), ("All files", "*.*")],
        )
        if chosen:
            file_var.set(chosen)
            _try_extract(chosen)

    ttk.Button(file_box, text="Browse...", command=browse_file).pack(side="left", padx=(4, 0))
    ttk.Button(file_box, text="Try extract", command=lambda: _try_extract(file_var.get())).pack(side="left", padx=(4, 0))

    status_label = ttk.Label(frame, text="", foreground="#555555", wraplength=580)
    status_label.pack(anchor="w", pady=(0, 4))

    inline_box = ttk.LabelFrame(
        frame, text="Inline SPICE definition (.model / .subckt)", padding=8
    )
    inline_box.pack(fill="both", expand=True, pady=(0, 10))
    text_widget = tk.Text(inline_box, height=6, wrap="word")
    text_widget.pack(fill="both", expand=True)

    if found:
        _try_extract(found)

    def use_inline() -> None:
        content = text_widget.get("1.0", "end").strip()
        if content:
            result["inline"] = content
        elif file_var.get().strip():
            result["path"] = file_var.get().strip()
        dialog.destroy()

    def skip() -> None:
        dialog.destroy()

    button_row = ttk.Frame(frame)
    button_row.pack(fill="x", pady=(4, 0))
    ttk.Button(button_row, text="Confirm", command=use_inline).pack(side="left")
    ttk.Button(button_row, text="Skip", command=skip).pack(side="right")

    dialog.wait_window()
    return result or None


def run_gui() -> int:
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk
    root = tk.Tk()
    root.title("Fix and Import Models")
    root.geometry("840x580")
    asc_var = tk.StringVar()
    qsch_var = tk.StringVar()
    auto_search_var = tk.BooleanVar(value=True)
    fix_annotations_var = tk.BooleanVar(value=True)
    replace_zero_ohm_var = tk.BooleanVar(value=True)
    device_model_fix_var = tk.BooleanVar(value=True)
    main = ttk.Frame(root, padding=12)
    main.pack(fill="both", expand=True)
    
    def browse_asc() -> None:
        path = filedialog.askopenfilename(
            parent=root,
            title="Select original .asc file",
            filetypes=[("LTspice schematic", "*.asc"), ("All files", "*.*")],
        )
        if path:
            asc_var.set(path)

    def browse_qsch() -> None:
        path = filedialog.askopenfilename(
            parent=root,
            title="Select converted .qsch file",
            filetypes=[("Qsch file", "*.qsch"), ("All files", "*.*")],
        )
        if path:
            qsch_var.set(path)

    ttk.Label(main, text="Original .asc file").grid(row=0, column=0, sticky="w")
    ttk.Entry(main, textvariable=asc_var).grid(row=1, column=0, sticky="ew", padx=(0, 8))
    ttk.Button(main, text="Browse...", command=browse_asc).grid(row=1, column=1, sticky="ew")
    ttk.Label(main, text="Converted .qsch file").grid(row=2, column=0, sticky="w", pady=(10, 0))
    ttk.Entry(main, textvariable=qsch_var).grid(row=3, column=0, sticky="ew", padx=(0, 8))
    ttk.Button(main, text="Browse...", command=browse_qsch).grid(row=3, column=1, sticky="ew")
    ttk.Checkbutton(
        main,
        text="Auto-search LTspice library folders first",
        variable=auto_search_var,
    ).grid(row=4, column=0, columnspan=2, sticky="w", pady=(10, 4))
    ttk.Checkbutton(
        main,
        text="Fix plain text annotations / hidden notes as comments (recommended)",
        variable=fix_annotations_var,
    ).grid(row=5, column=0, columnspan=2, sticky="w", pady=(0, 4))
    ttk.Checkbutton(
        main,
        text="Replace 0-ohm resistors with wires (recommended)",
        variable=replace_zero_ohm_var,
    ).grid(row=6, column=0, columnspan=2, sticky="w", pady=(0, 4))
    ttk.Checkbutton(
        main,
        text="Detect missing component model/subcircuit definitions (recommended)",
        variable=device_model_fix_var,
    ).grid(row=7, column=0, columnspan=2, sticky="w", pady=(0, 8))
    
    log_frame = ttk.LabelFrame(main, text="Log", padding=8)
    log_frame.grid(row=9, column=0, columnspan=2, sticky="nsew", pady=(8, 0))
    text = tk.Text(log_frame, wrap="word", height=18)
    text.pack(side="left", fill="both", expand=True)
    scrollbar = ttk.Scrollbar(log_frame, orient="vertical", command=text.yview)
    scrollbar.pack(side="right", fill="y")
    text.configure(yscrollcommand=scrollbar.set)

    def log(msg: str = "") -> None:
        text.insert("end", msg + "\n")
        text.see("end")
        root.update_idletasks()

    def run() -> None:
        asc_file = asc_var.get().strip()
        qsch_file = qsch_var.get().strip()
        if not asc_file or not os.path.isfile(asc_file):
            messagebox.showerror("Missing file", "Please select a valid .asc file.", parent=root)
            return
        if not qsch_file or not os.path.isfile(qsch_file):
            messagebox.showerror("Missing file", "Please select a valid .qsch file.", parent=root)
            return
        text.delete("1.0", "end")
        log("Starting...\n")
        search_roots = DEFAULT_SEARCH_ROOTS if auto_search_var.get() else []
        def chooser(model: str, found: Optional[str]) -> Optional[str]:
            return choose_model_path_gui(model, found, root)
        def device_chooser(model: str, found: Optional[str], refs: List[tuple]) -> Optional[dict]:
            return choose_device_model_gui(model, found, refs, root)
        try:
            process_models(
                asc_file=asc_file,
                qsch_file=qsch_file,
                search_roots=search_roots,
                choose_model_path=chooser,
                log=log,
                fix_annotations=fix_annotations_var.get(),
                replace_zero_ohm=replace_zero_ohm_var.get(),
                choose_device_model=device_chooser if device_model_fix_var.get() else None,
            )
            messagebox.showinfo("Done", "Processing finished.", parent=root)
        except Exception as exc:
            log(f"ERROR: {exc}")
            messagebox.showerror("Error", str(exc), parent=root)

    button_row = ttk.Frame(main)
    button_row.grid(row=8, column=0, columnspan=2, sticky="ew")
    ttk.Button(button_row, text="Run", command=run).pack(side="left")
    ttk.Button(button_row, text="Quit", command=root.destroy).pack(side="left", padx=(8, 0))
    main.columnconfigure(0, weight=1)
    main.rowconfigure(9, weight=1)
    root.mainloop()
    return 0


def main() -> int:
    if len(sys.argv) == 1:
        return run_gui()
    if any(arg in ("--gui", "-g") for arg in sys.argv[1:]):
        filtered = [a for a in sys.argv[1:] if a not in ("--gui", "-g")]
        if filtered:
            return run_cli(filtered)
        return run_gui()
    return run_cli(sys.argv[1:])


if __name__ == "__main__":
    raise SystemExit(main())
