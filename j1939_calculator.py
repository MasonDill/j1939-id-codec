#!/usr/bin/env python3
"""Convert between SAE J1939 identifier fields and a 29-bit CAN ID.

    j1939_calculator.py decode 0x18EA00F9
    j1939_calculator.py encode --pgn 0xF004 --priority 3 --source 0x00
    j1939_calculator.py encode --pgn 0xEA00 --dest 0x21 --source 0xF9
    j1939_calculator.py encode --pf 0xEA --ps 0x00 --source 0xF9
    j1939_calculator.py                                  (interactive)

Identifier layout, per SAE J1939-21:

    bits 28..26   Priority          0 highest, 6 the usual default
    bit  25       EDP               extended data page
    bit  24       DP                data page
    bits 23..16   PF                PDU format
    bits 15..8    PS                PDU specific
    bits  7..0    SA                source address

PF decides what PS means. Below 240 the message is PDU1, addressed to one
node, and PS holds the destination address, which is *not* part of the PGN --
so an addressed message keeps the same PGN whoever it is sent to. At 240 and
above it is PDU2, a broadcast, and PS is a group extension that forms part of
the PGN.
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from typing import Optional, Tuple

MODE_J2U = "j2u"   # kept as an alias for "encode"
MODE_U2J = "u2j"   # kept as an alias for "decode"

MAX_CAN_ID = 0x1FFFFFFF          # 29 bits
MAX_PGN = 0x3FFFF                # EDP + DP + PF + PS
PDU2_THRESHOLD = 240             # PF at or above this is a broadcast

DEFAULT_PRIORITY = 6
BROADCAST_ADDRESS = 0xFF
NULL_ADDRESS = 0xFE

#: A handful of common PGNs, so decoded output is readable at a glance.
#: Not exhaustive; J1939-71 defines several hundred.
KNOWN_PGNS = {
    0x00000: "ACK - Acknowledgement",
    0x0EA00: "Request",
    0x0EB00: "TP.DT - Transport Protocol, Data Transfer",
    0x0EC00: "TP.CM - Transport Protocol, Connection Management",
    0x0EE00: "AC - Address Claimed",
    0x0F004: "EEC1 - Electronic Engine Controller 1",
    0x0F003: "EEC2 - Electronic Engine Controller 2",
    0x0FE6C: "TCO1 - Tachograph",
    0x0FECA: "DM1 - Active Diagnostic Trouble Codes",
    0x0FECB: "DM2 - Previously Active Diagnostic Trouble Codes",
    0x0FEE0: "VD - Vehicle Distance",
    0x0FEE5: "HOURS - Engine Hours, Revolutions",
    0x0FEE9: "LFC1 - Fuel Consumption",
    0x0FEEE: "ET1 - Engine Temperature 1",
    0x0FEF1: "CCVS1 - Cruise Control / Vehicle Speed",
    0x0FEF2: "LFE1 - Fuel Economy",
    0x0FEF5: "AMB - Ambient Conditions",
    0x0FEF6: "IC1 - Intake / Exhaust Conditions 1",
}


def _check(name: str, value: int, bits: int) -> int:
    """Reject a field that does not fit, rather than silently truncating it."""
    if not isinstance(value, int) or isinstance(value, bool):
        raise TypeError(f"{name} must be an integer, got {type(value).__name__}")
    limit = 1 << bits
    if not 0 <= value < limit:
        raise ValueError(
            f"{name} must be {bits} bits (0..{limit - 1}), got {value} (0x{value:X})"
        )
    return value


@dataclass(frozen=True)
class J1939Id:
    """A 29-bit J1939 identifier, taken apart."""

    priority: int = DEFAULT_PRIORITY
    edp: int = 0
    dp: int = 0
    pf: int = 0
    ps: int = 0
    sa: int = 0

    def __post_init__(self) -> None:
        _check("priority", self.priority, 3)
        _check("EDP", self.edp, 1)
        _check("DP", self.dp, 1)
        _check("PF", self.pf, 8)
        _check("PS", self.ps, 8)
        _check("SA", self.sa, 8)

    # -- derived -------------------------------------------------------

    @property
    def can_id(self) -> int:
        """The assembled 29-bit identifier."""
        return (
            (self.priority << 26)
            | (self.edp << 25)
            | (self.dp << 24)
            | (self.pf << 16)
            | (self.ps << 8)
            | self.sa
        )

    @property
    def is_pdu1(self) -> bool:
        """True when the message is addressed to a specific node."""
        return self.pf < PDU2_THRESHOLD

    @property
    def destination_address(self) -> Optional[int]:
        """The destination, or None for a broadcast."""
        return self.ps if self.is_pdu1 else None

    @property
    def pgn(self) -> int:
        """Parameter Group Number.

        For PDU1 the PS byte is a destination address, not part of the group,
        so it is excluded. That is why the same PGN appears with different
        identifiers depending on who a message was sent to.
        """
        base = (self.edp << 17) | (self.dp << 16) | (self.pf << 8)
        return base if self.is_pdu1 else base | self.ps

    @property
    def name(self) -> str:
        """A human-readable name for the PGN, or an empty string."""
        return KNOWN_PGNS.get(self.pgn, "")

    # -- constructors --------------------------------------------------

    @classmethod
    def from_can_id(cls, can_id: int) -> "J1939Id":
        """Take a raw 29-bit identifier apart."""
        _check("CAN ID", can_id, 29)
        return cls(
            priority=(can_id >> 26) & 0x07,
            edp=(can_id >> 25) & 0x01,
            dp=(can_id >> 24) & 0x01,
            pf=(can_id >> 16) & 0xFF,
            ps=(can_id >> 8) & 0xFF,
            sa=can_id & 0xFF,
        )

    @classmethod
    def from_pgn(cls, pgn: int, source: int, priority: int = DEFAULT_PRIORITY,
                 destination: Optional[int] = None) -> "J1939Id":
        """Build an identifier from a PGN.

        @param destination  required only for a PDU1 (addressed) PGN, where it
                            supplies the PS byte. Defaults to the global
                            broadcast address if omitted.
        @raises ValueError  if a destination is given for a PDU2 PGN, or if a
                            PDU1 PGN carries a non-zero low byte.
        """
        _check("PGN", pgn, 18)

        edp = (pgn >> 17) & 0x01
        dp = (pgn >> 16) & 0x01
        pf = (pgn >> 8) & 0xFF
        ps = pgn & 0xFF

        if pf < PDU2_THRESHOLD:
            if ps:
                raise ValueError(
                    f"PGN 0x{pgn:04X} is PDU1 (PF 0x{pf:02X} < 240), so its low "
                    "byte must be zero; give the destination separately"
                )
            ps = BROADCAST_ADDRESS if destination is None else _check(
                "destination", destination, 8)
        elif destination is not None:
            raise ValueError(
                f"PGN 0x{pgn:04X} is PDU2 (PF 0x{pf:02X} >= 240), a broadcast, "
                "so it has no destination address"
            )

        return cls(priority=priority, edp=edp, dp=dp, pf=pf, ps=ps, sa=source)

    # -- output --------------------------------------------------------

    def describe(self) -> str:
        lines = [
            f"CAN ID              0x{self.can_id:08X}  ({self.can_id})",
            f"  Priority          {self.priority}",
            f"  EDP               {self.edp}",
            f"  DP                {self.dp}",
            f"  PF  (PDU format)  0x{self.pf:02X}  ({self.pf})",
            f"  PS  (PDU spec.)   0x{self.ps:02X}  ({self.ps})"
            + ("   destination address" if self.is_pdu1 else "   group extension"),
            f"  SA  (source)      0x{self.sa:02X}  ({self.sa})",
            "",
            f"PGN                 0x{self.pgn:05X}  ({self.pgn})"
            + (f"   {self.name}" if self.name else ""),
            f"Type                {'PDU1, addressed' if self.is_pdu1 else 'PDU2, broadcast'}",
        ]
        if self.is_pdu1:
            dest = self.destination_address
            note = ""
            if dest == BROADCAST_ADDRESS:
                note = "   (global broadcast)"
            elif dest == NULL_ADDRESS:
                note = "   (null address)"
            lines.append(f"Destination         0x{dest:02X}  ({dest}){note}")
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Backwards-compatible helpers
# ---------------------------------------------------------------------------

def encode_j1939_to_uint32(priority: int, pdu_f: int, pdu_s: int, source: int,
                           edp: int = 0, dp: int = 0) -> int:
    """Encode J1939 fields into a 29-bit CAN ID.

    Unlike the previous version this rejects out-of-range fields instead of
    masking them, and carries the EDP and DP bits, without which the result
    could not represent a page-1 PGN and a decode/encode round trip lost them.
    """
    return J1939Id(priority=priority, edp=edp, dp=dp,
                   pf=pdu_f, ps=pdu_s, sa=source).can_id


def decode_uint32_to_j1939(can_id: int) -> Tuple[int, int, int, int, int, int]:
    """Decode a 29-bit CAN ID into (priority, edp, dp, pf, ps, sa).

    The previous version returned four values and dropped EDP and DP, so
    feeding its output back into the encoder did not reproduce the input.
    """
    j = J1939Id.from_can_id(can_id)
    return j.priority, j.edp, j.dp, j.pf, j.ps, j.sa


# ---------------------------------------------------------------------------
# Command line
# ---------------------------------------------------------------------------

def parse_int(text: str) -> int:
    """Accept decimal, 0x hex or 0b binary."""
    try:
        return int(text, 0)
    except ValueError:
        raise argparse.ArgumentTypeError(
            f"{text!r} is not a number (try 0x18EA00F9 or 418316025)"
        )


def prompt_int(label: str, low: int, high: int, default: Optional[int] = None) -> int:
    """Read a number from the terminal, re-asking until it is in range."""
    suffix = f" [{default}]" if default is not None else ""
    while True:
        raw = input(f"{label} ({low}-{high}){suffix}: ").strip()
        if not raw and default is not None:
            return default
        try:
            value = int(raw, 0)
        except ValueError:
            print(f"  {raw!r} is not a number; hex like 0xEA is fine")
            continue
        if not low <= value <= high:
            print(f"  must be between {low} and {high}")
            continue
        return value


def interactive() -> int:
    """The original prompt-driven flow, kept for when no arguments are given."""
    print("J1939 identifier calculator")
    print("  1) fields or PGN -> CAN ID")
    print("  2) CAN ID -> fields")
    choice = input("Choose (1/2): ").strip()

    if choice == "2":
        can_id = prompt_int("CAN ID", 0, MAX_CAN_ID)
        print()
        print(J1939Id.from_can_id(can_id).describe())
        return 0

    if choice != "1":
        print("error: choose 1 or 2", file=sys.stderr)
        return 2

    by_pgn = input("Enter a PGN rather than PF/PS? (y/N): ").strip().lower()

    priority = prompt_int("Priority", 0, 7, DEFAULT_PRIORITY)
    source = prompt_int("Source address", 0, 255)

    if by_pgn.startswith("y"):
        pgn = prompt_int("PGN", 0, MAX_PGN)
        destination = None
        if ((pgn >> 8) & 0xFF) < PDU2_THRESHOLD:
            destination = prompt_int("Destination address", 0, 255,
                                     BROADCAST_ADDRESS)
        identifier = J1939Id.from_pgn(pgn, source, priority, destination)
    else:
        edp = prompt_int("EDP", 0, 1, 0)
        dp = prompt_int("DP", 0, 1, 0)
        pf = prompt_int("PDU Format (PF)", 0, 255)
        ps = prompt_int("PDU Specific (PS)", 0, 255)
        identifier = J1939Id(priority=priority, edp=edp, dp=dp,
                             pf=pf, ps=ps, sa=source)

    print()
    print(identifier.describe())
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Encode and decode SAE J1939 CAN identifiers.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    sub = parser.add_subparsers(dest="mode")

    dec = sub.add_parser("decode", aliases=[MODE_U2J],
                         help="29-bit CAN ID -> J1939 fields")
    dec.add_argument("can_id", type=parse_int, help="e.g. 0x18EA00F9")

    enc = sub.add_parser("encode", aliases=[MODE_J2U],
                         help="J1939 fields or a PGN -> 29-bit CAN ID")
    enc.add_argument("--priority", type=parse_int, default=DEFAULT_PRIORITY,
                     help=f"0..7 (default: {DEFAULT_PRIORITY})")
    enc.add_argument("--source", "--sa", type=parse_int, required=True,
                     help="source address, 0..255")
    enc.add_argument("--pgn", type=parse_int, help="parameter group number")
    enc.add_argument("--dest", "--da", type=parse_int,
                     help="destination address, for a PDU1 (addressed) PGN")
    enc.add_argument("--pf", type=parse_int, help="PDU format, 0..255")
    enc.add_argument("--ps", type=parse_int, help="PDU specific, 0..255")
    enc.add_argument("--edp", type=parse_int, default=0, help="extended data page")
    enc.add_argument("--dp", type=parse_int, default=0, help="data page")
    enc.add_argument("-q", "--quiet", action="store_true",
                     help="print only the hex identifier")

    return parser


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv

    if not argv:
        try:
            return interactive()
        except (KeyboardInterrupt, EOFError):
            print()
            return 130
        except ValueError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1

    args = build_parser().parse_args(argv)

    try:
        if args.mode in ("decode", MODE_U2J):
            print(J1939Id.from_can_id(args.can_id).describe())
            return 0

        if args.pgn is not None:
            if args.pf is not None or args.ps is not None:
                print("error: give either --pgn or --pf/--ps, not both",
                      file=sys.stderr)
                return 2
            identifier = J1939Id.from_pgn(args.pgn, args.source,
                                          args.priority, args.dest)
        else:
            if args.pf is None:
                print("error: encode needs either --pgn or --pf", file=sys.stderr)
                return 2
            if args.dest is not None:
                print("error: --dest applies to --pgn; with --pf set PS directly",
                      file=sys.stderr)
                return 2
            identifier = J1939Id(priority=args.priority, edp=args.edp, dp=args.dp,
                                 pf=args.pf, ps=args.ps or 0, sa=args.source)

        if args.quiet:
            print(f"0x{identifier.can_id:08X}")
        else:
            print(identifier.describe())
        return 0

    except (ValueError, TypeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
