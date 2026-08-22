# j1939-id-codec

Convert between SAE J1939 identifier fields and a 29-bit CAN ID, in either
direction. One file, no dependencies.

```
$ ./j1939_calculator.py decode 0x18EA00F9
CAN ID              0x18EA00F9  (418316025)
  Priority          6
  EDP               0
  DP                0
  PF  (PDU format)  0xEA  (234)
  PS  (PDU spec.)   0x00  (0)   destination address
  SA  (source)      0xF9  (249)

PGN                 0x0EA00  (60928)   Request
Type                PDU1, addressed
Destination         0x00  (0)
```

```
$ ./j1939_calculator.py encode --pgn 0xF004 --priority 3 --source 0x00 -q
0x0CF00400
```

Run it with no arguments for the interactive prompts.

## Identifier layout

Per SAE J1939-21:

| Bits | Field | |
|---|---|---|
| 28..26 | Priority | 0 highest, 6 the usual default |
| 25 | EDP | extended data page |
| 24 | DP | data page |
| 23..16 | PF | PDU format |
| 15..8 | PS | PDU specific |
| 7..0 | SA | source address |

**PF decides what PS means.** Below 240 the message is PDU1, addressed to one
node, and PS is the destination address — which is *not* part of the PGN, so an
addressed message keeps the same PGN whoever it is sent to. At 240 and above it
is PDU2, a broadcast, and PS is a group extension that does form part of the
PGN.

That asymmetry is the thing this tool exists to get right, and it is the reason
`--dest` is separate from `--pgn`: you cannot put a destination inside a PGN.

## Usage

```
j1939_calculator.py decode <can-id>

j1939_calculator.py encode --source SA [--priority P]
                           ( --pgn PGN [--dest DA] | --pf PF [--ps PS] )
                           [--edp E] [--dp D] [-q]
```

Numbers may be decimal, `0x` hex or `0b` binary. `-q` prints just the hex
identifier, for piping into something else. The old `j2u` and `u2j` mode names
still work as aliases for `encode` and `decode`.

As a library:

```python
from j1939_calculator import J1939Id

j = J1939Id.from_can_id(0x18EA00F9)
j.pgn                    # 0x0EA00
j.is_pdu1                # True
j.destination_address    # 0x00

J1939Id.from_pgn(0xF004, source=0x00, priority=3).can_id   # 0x0CF00400
```

## What changed

**EDP and DP were dropped.** The encoder never set bits 25 and 24 and the
decoder never read them, so page-1 PGNs could not be represented at all and a
decode followed by an encode did not give back what you started with —
`0x1DFFFF00` came back as `0x1CFFFF00`. Both bits are now carried, and a sweep
of the identifier space confirms the round trip is lossless.

**There was no PGN.** The tool reported PF, PS and SA and left you to do the
PDU1/PDU2 arithmetic by hand, which is the part that is easy to get wrong. It
now reports the PGN, the type, and the destination address where one exists,
and will build an identifier from a PGN directly.

**Out-of-range values were masked, not rejected.** `encode_j1939_to_uint32(8, …)`
silently produced priority 0, and a 9-bit PF was truncated. Fields are now
validated, with a message naming the field and its range.

**It could only be used interactively.** Everything went through `input()`
prompts, so it could not be scripted or piped. There is a non-interactive CLI
now; the prompts remain when you run it bare.

The `encode_j1939_to_uint32` and `decode_uint32_to_j1939` helpers are still
there. `decode` now returns six fields rather than four, since dropping EDP and
DP was the bug.

## Tests

```
python test_j1939_calculator.py        # or: pytest -q
```

276 checks. The identifiers and PGNs are worked examples from J1939-21 and from
real buses — EEC1 at `0x0CF00400`, the Request PGN at `0x18EA00F9`, TP.CM at
`0x1CECFF00` — not values recorded from this implementation.

They have teeth:

| Mutation | Checks failed |
|---|---|
| drop EDP/DP from the identifier (the original bug) | 8 |
| PDU1 PGN wrongly includes the destination | 14 |
| PDU1/PDU2 boundary off by one | crashes |
| mask out-of-range fields instead of rejecting (the original behaviour) | 19 |
| priority shifted to the wrong bits | 75 |
| report a destination for broadcasts too | 4 |
