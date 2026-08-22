#!/usr/bin/env python3
"""Tests for the J1939 identifier calculator.

Run with ``python test_j1939_calculator.py`` or under pytest. No dependencies.

The identifier and PGN cases are worked examples from SAE J1939-21 and from
identifiers seen on real buses, not values recorded from this implementation.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from j1939_calculator import (
    BROADCAST_ADDRESS,
    DEFAULT_PRIORITY,
    J1939Id,
    decode_uint32_to_j1939,
    encode_j1939_to_uint32,
    main,
    parse_int,
)

SCRIPT = Path(__file__).resolve().parent / "j1939_calculator.py"

_failures = []
_checks = 0


def check(condition, message):
    global _checks
    _checks += 1
    if not condition:
        _failures.append(message)
        print(f"    FAIL {message}")


def check_eq(got, want, message):
    check(got == want, f"{message}: got {got!r}, want {want!r}")


def check_raises(exc_type, fn, message):
    global _checks
    _checks += 1
    try:
        fn()
    except exc_type:
        return
    except Exception as exc:
        _failures.append(f"{message}: raised {type(exc).__name__}, want {exc_type.__name__}")
        print(f"    FAIL {message}: raised {type(exc).__name__}")
        return
    _failures.append(f"{message}: nothing raised")
    print(f"    FAIL {message}: nothing raised")


# ---------------------------------------------------------------------------
# Known identifiers
# ---------------------------------------------------------------------------

#: (CAN ID, priority, edp, dp, pf, ps, sa, pgn, is_pdu1)
KNOWN = [
    # EEC1 engine speed, broadcast, priority 3, from the engine ECU.
    (0x0CF00400, 3, 0, 0, 0xF0, 0x04, 0x00, 0x0F004, False),
    # Request PGN, addressed to node 0x00, from node 0xF9.
    (0x18EA00F9, 6, 0, 0, 0xEA, 0x00, 0xF9, 0x0EA00, True),
    # Transport protocol connection management, global broadcast.
    (0x1CECFF00, 7, 0, 0, 0xEC, 0xFF, 0x00, 0x0EC00, True),
    # Engine hours, broadcast.
    (0x18FEE500, 6, 0, 0, 0xFE, 0xE5, 0x00, 0x0FEE5, False),
    # Address claimed, global.
    (0x18EEFF00, 6, 0, 0, 0xEE, 0xFF, 0x00, 0x0EE00, True),
    # Data page set: PGN moves into page 1.
    (0x0DF00400, 3, 0, 1, 0xF0, 0x04, 0x00, 0x1F004, False),
    # Both page bits set.
    (0x0FF00400, 3, 1, 1, 0xF0, 0x04, 0x00, 0x3F004, False),
    # Extremes.
    (0x00000000, 0, 0, 0, 0x00, 0x00, 0x00, 0x00000, True),
    (0x1FFFFFFF, 7, 1, 1, 0xFF, 0xFF, 0xFF, 0x3FFFF, False),
]


def test_decode_known_identifiers():
    print("  decoding known identifiers")
    for can_id, pri, edp, dp, pf, ps, sa, pgn, pdu1 in KNOWN:
        j = J1939Id.from_can_id(can_id)
        label = f"0x{can_id:08X}"
        check_eq(j.priority, pri, f"{label} priority")
        check_eq(j.edp, edp, f"{label} EDP")
        check_eq(j.dp, dp, f"{label} DP")
        check_eq(j.pf, pf, f"{label} PF")
        check_eq(j.ps, ps, f"{label} PS")
        check_eq(j.sa, sa, f"{label} SA")
        check_eq(j.pgn, pgn, f"{label} PGN")
        check_eq(j.is_pdu1, pdu1, f"{label} PDU1")


def test_encode_known_identifiers():
    print("  encoding back to the same identifiers")
    for can_id, pri, edp, dp, pf, ps, sa, _pgn, _pdu1 in KNOWN:
        got = J1939Id(priority=pri, edp=edp, dp=dp, pf=pf, ps=ps, sa=sa).can_id
        check_eq(got, can_id, f"encoding 0x{can_id:08X}")


def test_round_trip_is_lossless():
    """The old codec dropped EDP and DP, so this did not hold."""
    print("  decode then encode reproduces the input exactly")
    # Sweep every combination of the page bits against a spread of other fields.
    for edp in (0, 1):
        for dp in (0, 1):
            for pri in (0, 3, 7):
                for pf in (0x00, 0xEA, 0xEF, 0xF0, 0xFE, 0xFF):
                    original = J1939Id(priority=pri, edp=edp, dp=dp,
                                       pf=pf, ps=0x5A, sa=0xA5).can_id
                    check_eq(J1939Id.from_can_id(original).can_id, original,
                             f"round trip 0x{original:08X}")


def test_legacy_helpers_round_trip():
    """The module-level helpers must round-trip too, page bits included."""
    print("  the encode/decode helpers round-trip")
    for can_id, *_rest in KNOWN:
        fields = decode_uint32_to_j1939(can_id)
        check_eq(len(fields), 6, "decode returns six fields including EDP and DP")
        pri, edp, dp, pf, ps, sa = fields
        check_eq(encode_j1939_to_uint32(pri, pf, ps, sa, edp, dp), can_id,
                 f"helper round trip 0x{can_id:08X}")


# ---------------------------------------------------------------------------
# PGN arithmetic
# ---------------------------------------------------------------------------

def test_pdu1_pgn_excludes_the_destination():
    """An addressed message keeps its PGN whoever it is sent to."""
    print("  a PDU1 PGN does not depend on the destination")
    base = None
    for dest in (0x00, 0x21, 0x80, BROADCAST_ADDRESS):
        j = J1939Id(priority=6, pf=0xEA, ps=dest, sa=0xF9)
        check_eq(j.pgn, 0x0EA00, f"Request PGN with destination 0x{dest:02X}")
        check_eq(j.destination_address, dest, f"destination 0x{dest:02X}")
        if base is None:
            base = j.pgn
        check_eq(j.pgn, base, "the PGN must not move with the destination")


def test_pdu2_pgn_includes_the_group_extension():
    print("  a PDU2 PGN includes the PS byte")
    for ps in (0x00, 0x04, 0xE5, 0xFF):
        j = J1939Id(priority=3, pf=0xF0, ps=ps, sa=0x00)
        check_eq(j.pgn, 0xF000 | ps, f"PGN with group extension 0x{ps:02X}")
        check_eq(j.destination_address, None, "a broadcast has no destination")
        check(not j.is_pdu1, "PF 0xF0 is PDU2")


def test_pdu1_pdu2_boundary():
    """The split is at PF 240 exactly."""
    print("  the PDU1/PDU2 boundary sits at PF 240")
    check(J1939Id(pf=239).is_pdu1, "PF 239 is PDU1")
    check(not J1939Id(pf=240).is_pdu1, "PF 240 is PDU2")
    check_eq(J1939Id(pf=239, ps=0xAB).pgn, 0xEF00, "PF 239 excludes PS")
    check_eq(J1939Id(pf=240, ps=0xAB).pgn, 0xF0AB, "PF 240 includes PS")


def test_from_pgn():
    print("  building an identifier from a PGN")

    # PDU2: no destination, PS comes from the PGN.
    j = J1939Id.from_pgn(0xF004, source=0x00, priority=3)
    check_eq(j.can_id, 0x0CF00400, "EEC1 from PGN")
    check_eq(j.pgn, 0xF004, "PGN survives the round trip")

    # PDU1: destination supplied separately.
    j = J1939Id.from_pgn(0xEA00, source=0xF9, priority=6, destination=0x00)
    check_eq(j.can_id, 0x18EA00F9, "Request from PGN with a destination")

    # PDU1 with no destination defaults to the global address.
    j = J1939Id.from_pgn(0xEA00, source=0xF9)
    check_eq(j.ps, BROADCAST_ADDRESS, "PDU1 defaults to the broadcast address")
    check_eq(j.priority, DEFAULT_PRIORITY, "default priority is 6")

    # Page bits survive.
    j = J1939Id.from_pgn(0x1F004, source=0x00, priority=3)
    check_eq(j.dp, 1, "PGN bit 16 becomes DP")
    check_eq(j.can_id, 0x0DF00400, "page 1 identifier")

    j = J1939Id.from_pgn(0x3F004, source=0x00, priority=3)
    check_eq((j.edp, j.dp), (1, 1), "PGN bits 17 and 16 become EDP and DP")

    # Every PGN in the table must round-trip.
    from j1939_calculator import KNOWN_PGNS
    for pgn in KNOWN_PGNS:
        pf = (pgn >> 8) & 0xFF
        dest = 0x21 if pf < 240 else None
        rebuilt = J1939Id.from_pgn(pgn, source=0x17, destination=dest)
        check_eq(rebuilt.pgn, pgn, f"PGN 0x{pgn:05X} round trip")


def test_from_pgn_rejects_contradictions():
    print("  from_pgn rejects contradictory arguments")
    check_raises(ValueError,
                 lambda: J1939Id.from_pgn(0xEA21, source=0x00),
                 "a PDU1 PGN with a non-zero low byte")
    check_raises(ValueError,
                 lambda: J1939Id.from_pgn(0xF004, source=0x00, destination=0x21),
                 "a destination on a PDU2 broadcast")


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def test_out_of_range_is_rejected():
    """The old encoder masked silently, so priority 8 became priority 0."""
    print("  out-of-range fields are rejected, not truncated")
    check_raises(ValueError, lambda: J1939Id(priority=8), "priority 8")
    check_raises(ValueError, lambda: J1939Id(priority=-1), "negative priority")
    check_raises(ValueError, lambda: J1939Id(pf=256), "PF 256")
    check_raises(ValueError, lambda: J1939Id(ps=256), "PS 256")
    check_raises(ValueError, lambda: J1939Id(sa=256), "SA 256")
    check_raises(ValueError, lambda: J1939Id(edp=2), "EDP 2")
    check_raises(ValueError, lambda: J1939Id(dp=2), "DP 2")
    check_raises(ValueError, lambda: J1939Id.from_can_id(1 << 29), "a 30-bit ID")
    check_raises(ValueError, lambda: J1939Id.from_can_id(-1), "a negative ID")
    check_raises(ValueError, lambda: J1939Id.from_pgn(1 << 18, source=0), "an oversized PGN")

    check_raises(ValueError, lambda: encode_j1939_to_uint32(8, 0xF0, 0x04, 0x00),
                 "the helper should reject priority 8")
    check_raises(ValueError, lambda: encode_j1939_to_uint32(3, 0x1FF, 0x04, 0x00),
                 "the helper should reject a 9-bit PF")

    # A bool is an int in Python, but not a sensible field value.
    check_raises(TypeError, lambda: J1939Id(priority=True), "a boolean priority")


def test_every_id_decodes():
    """No 29-bit value should be rejected or mis-round-trip."""
    print("  a sweep of the identifier space round-trips")
    step = 0x1FFFFFFF // 997
    bad = 0
    for can_id in range(0, 0x20000000, step):
        if J1939Id.from_can_id(can_id).can_id != can_id:
            bad += 1
    check_eq(bad, 0, "identifiers that failed to round-trip")


# ---------------------------------------------------------------------------
# Command line
# ---------------------------------------------------------------------------

def run(*args):
    return subprocess.run([sys.executable, str(SCRIPT), *args],
                          capture_output=True, text=True)


def test_cli_decode():
    print("  CLI decode")
    result = run("decode", "0x18EA00F9")
    check_eq(result.returncode, 0, f"decode exited {result.returncode}")
    out = result.stdout
    for expected in ("0x18EA00F9", "0x0EA00", "Request", "PDU1", "0xF9"):
        check(expected in out, f"decode output should mention {expected}")

    # The legacy mode name still works.
    check_eq(run("u2j", "0x18EA00F9").stdout, out, "u2j is an alias for decode")


def test_cli_encode():
    print("  CLI encode")
    result = run("encode", "--pgn", "0xF004", "--priority", "3", "--source", "0")
    check_eq(result.returncode, 0, f"encode exited {result.returncode}")
    check("0x0CF00400" in result.stdout, "encode should produce 0x0CF00400")

    result = run("encode", "--pgn", "0xF004", "--priority", "3",
                 "--source", "0", "-q")
    check_eq(result.stdout.strip(), "0x0CF00400", "quiet mode prints only the ID")

    result = run("encode", "--pf", "0xEA", "--ps", "0x00",
                 "--source", "0xF9", "--priority", "6", "-q")
    check_eq(result.stdout.strip(), "0x18EA00F9", "encoding from PF/PS")

    result = run("encode", "--pgn", "0xEA00", "--dest", "0x21",
                 "--source", "0xF9", "-q")
    check_eq(result.stdout.strip(), "0x18EA21F9", "encoding a PDU1 with a destination")

    result = run("j2u", "--pgn", "0xF004", "--source", "0", "--priority", "3", "-q")
    check_eq(result.stdout.strip(), "0x0CF00400", "j2u is an alias for encode")


def test_cli_errors():
    print("  CLI error handling")
    cases = [
        (("decode", "0x20000000"), "a 30-bit identifier"),
        (("decode", "banana"), "a non-numeric identifier"),
        (("encode", "--source", "0"), "encode with neither --pgn nor --pf"),
        (("encode", "--pgn", "0xF004", "--pf", "0xF0", "--source", "0"),
         "both --pgn and --pf"),
        (("encode", "--pgn", "0xF004", "--dest", "0x21", "--source", "0"),
         "a destination on a broadcast PGN"),
        (("encode", "--pf", "0xEA", "--dest", "0x21", "--source", "0"),
         "--dest alongside --pf"),
        (("encode", "--pgn", "0xF004", "--priority", "9", "--source", "0"),
         "priority 9"),
    ]
    for args, label in cases:
        result = run(*args)
        check(result.returncode != 0, f"{label} should fail")
        check(bool(result.stderr.strip()), f"{label} should explain itself on stderr")


def test_parse_int():
    print("  numeric argument parsing")
    check_eq(parse_int("0x18EA00F9"), 0x18EA00F9, "hex")
    check_eq(parse_int("418316025"), 418316025, "decimal")
    check_eq(parse_int("0b1010"), 10, "binary")
    check_raises(Exception, lambda: parse_int("nope"), "a non-number")


def test_main_is_importable():
    """main() should be callable in-process, for scripting."""
    print("  main() is usable as a function")

    import contextlib
    import io

    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        ok = main(["encode", "--pgn", "0xF004", "--source", "0",
                   "--priority", "3", "-q"])
        bad = main(["decode", "0x20000000"])

    check_eq(ok, 0, "main returns 0 on success")
    check_eq(out.getvalue().strip(), "0x0CF00400", "main writes the result to stdout")
    check_eq(bad, 1, "main returns non-zero on bad input")
    check(err.getvalue().strip(), "main writes the error to stderr")


def main_tests():
    print("j1939-id-codec tests\n")
    for fn in [
        test_decode_known_identifiers,
        test_encode_known_identifiers,
        test_round_trip_is_lossless,
        test_legacy_helpers_round_trip,
        test_pdu1_pgn_excludes_the_destination,
        test_pdu2_pgn_includes_the_group_extension,
        test_pdu1_pdu2_boundary,
        test_from_pgn,
        test_from_pgn_rejects_contradictions,
        test_out_of_range_is_rejected,
        test_every_id_decodes,
        test_cli_decode,
        test_cli_encode,
        test_cli_errors,
        test_parse_int,
        test_main_is_importable,
    ]:
        fn()
    print(f"\n{_checks} checks, {len(_failures)} failures")
    return 1 if _failures else 0


if __name__ == "__main__":
    sys.exit(main_tests())
