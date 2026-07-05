#!/usr/bin/env python3
r"""Place DRV5055 Hall sensors from a KLE-style keyboard-layout.json.

Run with KiCad's bundled Python so the pcbnew module is available, for example:

  C:\Users\stavros\AppData\Local\Programs\KiCad\10.0\bin\python.exe \
      scripts\place_hall_sensors.py --apply

The script is dry-run by default. Add --apply to write the board. By default,
it expects Hall sensors named K_SW1, K_SW2, ... and matching capacitors named
K_C1, K_C2, ...

Coordinate convention: origin is near the PCB's top-left, X grows rightward,
and Y grows downward, matching KiCad PCB coordinates.
"""

from __future__ import annotations

import argparse
import csv
import json
import shutil
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable

try:
    import pcbnew
except ImportError as exc:  # pragma: no cover - depends on KiCad install
    raise SystemExit(
        "Could not import pcbnew. Run this with KiCad's bundled Python, e.g.\n"
        r"  C:\Users\stavros\AppData\Local\Programs\KiCad\10.0\bin\python.exe "
        r"scripts\place_hall_sensors.py"
    ) from exc


DEFAULT_BOARD = "stav-keeb-65.kicad_pcb"
DEFAULT_LAYOUT = "keyboard-layout.json"


@dataclass(frozen=True)
class KeyPlacement:
    index: int
    label: str
    x_units: float
    y_units: float
    width: float
    height: float


def mm(value: float) -> int:
    return pcbnew.FromMM(value)


def parse_kle_layout(layout_path: Path) -> list[KeyPlacement]:
    """Parse the subset of KLE JSON used by keyboard-layout-editor."""
    with layout_path.open("r", encoding="utf-8") as handle:
        rows = json.load(handle)

    placements: list[KeyPlacement] = []
    key_index = 1
    y = 0.0

    for row in rows:
        x = 0.0
        width = 1.0
        height = 1.0

        for item in row:
            if isinstance(item, dict):
                x += float(item.get("x", 0.0))
                y += float(item.get("y", 0.0))
                width = float(item.get("w", width))
                height = float(item.get("h", height))
                continue

            if not isinstance(item, str):
                raise ValueError(f"Unsupported KLE item {item!r}")

            placements.append(
                KeyPlacement(
                    index=key_index,
                    label=item,
                    x_units=x + width / 2.0,
                    y_units=y + height / 2.0,
                    width=width,
                    height=height,
                )
            )
            key_index += 1
            x += width
            width = 1.0
            height = 1.0

        y += 1.0

    return placements


def ref(fp: object) -> str:
    return fp.GetReference()


def value(fp: object) -> str:
    return fp.GetValue()


def sorted_footprints(board: object) -> list[object]:
    return sorted(board.GetFootprints(), key=lambda fp: ref(fp))


def find_fp(board: object, reference: str) -> object | None:
    fp = board.FindFootprintByReference(reference)
    if fp is None:
        return None
    return fp


def ref_number(reference: str, prefix: str) -> int:
    if not reference.startswith(prefix):
        raise ValueError(f"{reference!r} does not start with {prefix!r}")
    return int(reference[len(prefix) :])


def pad_net_names(fp: object) -> set[str]:
    return {pad.GetNetname() for pad in fp.Pads() if pad.GetNetname()}


def sensor_vcc_nets(fp: object) -> list[str]:
    nets: list[str] = []
    for pad in fp.Pads():
        net = pad.GetNetname()
        if not net:
            continue
        pad_num = str(pad.GetNumber())
        if pad_num == "1" or net.endswith("-VCC)") or "VCC" in net:
            nets.append(net)
    return nets


def read_cap_map(path: Path) -> dict[str, str]:
    """Read CSV rows as switch_ref,cap_ref."""
    result: dict[str, str] = {}
    with path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.reader(handle)
        for row in reader:
            if not row or row[0].strip().startswith("#"):
                continue
            if len(row) < 2:
                raise ValueError(f"Bad cap map row in {path}: {row!r}")
            result[row[0].strip()] = row[1].strip()
    return result


@dataclass(frozen=True)
class CapInference:
    matches: dict[str, str]
    ambiguous: dict[str, list[str]]
    missing: list[str]


def infer_cap_map(
    board: object, switch_refs: Iterable[str], cap_prefix: str
) -> CapInference:
    """Infer switch -> capacitor from the capacitor attached to the sensor VCC net."""
    net_to_caps: dict[str, list[str]] = {}
    for fp in sorted_footprints(board):
        reference = ref(fp)
        if not reference.startswith(cap_prefix):
            continue
        if value(fp).strip().lower() not in {"100nf", "0.1uf", "100n"}:
            continue
        for net in pad_net_names(fp):
            net_to_caps.setdefault(net, []).append(reference)

    result: dict[str, str] = {}
    ambiguous: dict[str, list[str]] = {}
    missing: list[str] = []
    for switch_ref in switch_refs:
        switch = find_fp(board, switch_ref)
        if switch is None:
            continue
        candidates: list[str] = []
        for net in sensor_vcc_nets(switch):
            candidates.extend(net_to_caps.get(net, []))
        candidates = sorted(set(candidates))
        if len(candidates) == 1:
            result[switch_ref] = candidates[0]
        elif len(candidates) > 1:
            ambiguous[switch_ref] = candidates
        else:
            missing.append(switch_ref)
    return CapInference(result, ambiguous, missing)


def fallback_cap_ref(switch_index: int, prefix: str, offset: int) -> str:
    return f"{prefix}{switch_index + offset}"


def layer_id(side: str) -> int:
    if side == "front":
        return pcbnew.F_Cu
    if side == "back":
        return pcbnew.B_Cu
    raise ValueError(f"Unsupported side {side!r}")


def put_on_side(fp: object, side: str) -> None:
    target = layer_id(side)
    if fp.GetLayer() != target:
        fp.Flip(fp.GetPosition(), False)
    if fp.GetLayer() != target:
        fp.SetLayer(target)


def set_position(fp: object, x_mm: float, y_mm: float) -> None:
    fp.SetPosition(pcbnew.VECTOR2I(mm(x_mm), mm(y_mm)))


def set_rotation(fp: object, degrees: float | None) -> None:
    if degrees is None:
        return
    fp.SetOrientationDegrees(degrees)


def placement_xy(args: argparse.Namespace, key: KeyPlacement) -> tuple[float, float]:
    # origin_x/y is the center of the first 1u key, so subtract the first
    # key's nominal KLE center at 0.5u/0.5u. KiCad PCB coordinates grow
    # rightward on X and downward on Y, same as KLE row progression.
    x_mm = args.origin_x + (key.x_units - 0.5) * args.unit
    y_mm = args.origin_y + (key.y_units - 0.5) * args.unit
    return x_mm, y_mm


def backup_board(board_path: Path) -> Path:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup_path = board_path.with_suffix(board_path.suffix + f".bak-{stamp}")
    shutil.copy2(board_path, backup_path)
    return backup_path


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Place DRV5055 Hall sensors at key centers from keyboard-layout.json."
    )
    parser.add_argument("--board", default=DEFAULT_BOARD, help="KiCad PCB file")
    parser.add_argument("--layout", default=DEFAULT_LAYOUT, help="KLE-style JSON file")
    parser.add_argument("--output", help="Write to this board instead of overwriting --board")
    parser.add_argument("--apply", action="store_true", help="Write changes")
    parser.add_argument("--no-backup", action="store_true", help="Do not create backup when overwriting --board")
    parser.add_argument("--unit", type=float, default=19.05, help="Key unit in mm")
    parser.add_argument("--origin-x", type=float, default=38.1, help="X center of first key, mm")
    parser.add_argument("--origin-y", type=float, default=38.1, help="Y center of first key, mm")
    parser.add_argument("--first-index", type=int, default=1, help="First switch number")
    parser.add_argument("--switch-prefix", default="K_SW", help="Switch/Hall sensor reference prefix")
    parser.add_argument("--switch-side", choices=["front", "back"], default="front")
    parser.add_argument("--sensor-dx", type=float, default=0.0, help="Sensor X offset from key center, mm")
    parser.add_argument("--sensor-dy", type=float, default=0.0, help="Sensor Y offset from key center, mm")
    parser.add_argument("--sensor-rotation", type=float, help="Set sensor rotation in degrees")
    parser.add_argument("--cap-side", choices=["front", "back"], default="back")
    parser.add_argument("--cap-dx", type=float, default=0.0, help="Capacitor X offset from key center, mm")
    parser.add_argument("--cap-dy", type=float, default=0.0, help="Capacitor Y offset from key center, mm")
    parser.add_argument("--cap-rotation", type=float, help="Set capacitor rotation in degrees")
    parser.add_argument("--cap-map", help="Optional CSV with rows: K_SW1,K_C1")
    parser.add_argument(
        "--cap-match",
        choices=["number", "net", "auto"],
        default="number",
        help="How to pair sensors and capacitors: same number, VCC net, or net then number",
    )
    parser.add_argument("--cap-prefix", default="K_C", help="Capacitor reference prefix")
    parser.add_argument("--cap-index-offset", type=int, default=0, help="Cap ref offset for number matching")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)

    board_path = Path(args.board)
    layout_path = Path(args.layout)
    output_path = Path(args.output) if args.output else board_path

    placements = parse_kle_layout(layout_path)
    board = pcbnew.LoadBoard(str(board_path))

    switch_refs = [
        f"{args.switch_prefix}{args.first_index + key.index - 1}" for key in placements
    ]
    cap_inference = infer_cap_map(board, switch_refs, args.cap_prefix)
    explicit_caps = read_cap_map(Path(args.cap_map)) if args.cap_map else {}

    missing: list[str] = []
    unresolved_caps: list[str] = []
    report: list[str] = []

    for key in placements:
        number = args.first_index + key.index - 1
        switch_ref = f"{args.switch_prefix}{number}"
        switch = find_fp(board, switch_ref)
        if switch is None:
            missing.append(switch_ref)
            continue

        key_x, key_y = placement_xy(args, key)
        sensor_x = key_x + args.sensor_dx
        sensor_y = key_y + args.sensor_dy

        cap_ref = explicit_caps.get(switch_ref)
        if cap_ref is None and args.cap_match in {"net", "auto"}:
            cap_ref = cap_inference.matches.get(switch_ref)
        if cap_ref is None and args.cap_match in {"number", "auto"}:
            cap_ref = fallback_cap_ref(number, args.cap_prefix, args.cap_index_offset)
        if cap_ref is None:
            unresolved_caps.append(switch_ref)
        cap = find_fp(board, cap_ref) if cap_ref else None

        put_on_side(switch, args.switch_side)
        set_position(switch, sensor_x, sensor_y)
        set_rotation(switch, args.sensor_rotation)

        cap_note = "no cap found"
        if cap is not None:
            cap_x = key_x + args.cap_dx
            cap_y = key_y + args.cap_dy
            put_on_side(cap, args.cap_side)
            set_position(cap, cap_x, cap_y)
            set_rotation(cap, args.cap_rotation)
            cap_note = f"{cap_ref}@{args.cap_side} ({cap_x:.3f}, {cap_y:.3f})"
        elif cap_ref:
            missing.append(cap_ref)

        label = key.label.replace("\n", "/") or "Space"
        report.append(
            f"{switch_ref:>4} {label:<12} -> {args.switch_side} "
            f"({sensor_x:.3f}, {sensor_y:.3f}); {cap_note}"
        )

    print(f"Loaded {len(placements)} keys from {layout_path}")
    print(f"Board: {board_path}")
    print(f"Mode: {'apply' if args.apply else 'dry-run'}")
    for line in report:
        print(line)

    if missing:
        print("\nMissing references:")
        for reference in sorted(set(missing)):
            print(f"  {reference}")

    if unresolved_caps:
        print("\nUnresolved capacitors:")
        for switch_ref in sorted(set(unresolved_caps), key=lambda item: ref_number(item, args.switch_prefix)):
            if switch_ref in cap_inference.ambiguous:
                candidates = ", ".join(cap_inference.ambiguous[switch_ref])
                print(f"  {switch_ref}: multiple capacitors share its VCC net: {candidates}")
            elif switch_ref in cap_inference.missing:
                print(f"  {switch_ref}: no 100nF capacitor found on its VCC net")
            else:
                print(f"  {switch_ref}: no capacitor mapping found")
        print("  Provide --cap-map, use --cap-match number, or fix the per-sensor VCC nets.")

    if not args.apply:
        print("\nDry-run only. Re-run with --apply to write the board.")
        return 0

    if unresolved_caps:
        raise SystemExit("Refusing to apply with unresolved capacitor mappings.")

    if output_path == board_path and not args.no_backup:
        backup = backup_board(board_path)
        print(f"\nBackup written: {backup}")

    pcbnew.SaveBoard(str(output_path), board)
    print(f"Board written: {output_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
