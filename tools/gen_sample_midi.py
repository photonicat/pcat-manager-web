#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Generate the bundled sample MIDI files for the /buzzer page.

Writes app/static/midi/twinkle_twinkle.mid and app/static/midi/ode_to_joy.mid.
Both melodies are public domain. Format-0 single-track files, 480 ticks per
quarter note, 120 bpm, so they exercise tempo meta + note on/off parsing in the
page's JS MIDI reader without needing any third-party MIDI library here.
"""

import pathlib
import struct

TPQ = 480  # ticks per quarter note


def varlen(value):
    out = bytearray([value & 0x7F])
    value >>= 7
    while value:
        out.insert(0, 0x80 | (value & 0x7F))
        value >>= 7
    return bytes(out)


def midi_file(notes, us_per_quarter=500000):
    """notes: list of (midi_note, beats). Rests use midi_note None."""
    track = bytearray()
    track += varlen(0) + b"\xff\x51\x03" + struct.pack(">I", us_per_quarter)[1:]
    pending_delta = 0
    for note, beats in notes:
        ticks = int(round(beats * TPQ))
        if note is None:
            pending_delta += ticks
            continue
        track += varlen(pending_delta) + bytes([0x90, note, 100])
        track += varlen(ticks) + bytes([0x80, note, 64])
        pending_delta = 0
    track += varlen(0) + b"\xff\x2f\x00"

    header = b"MThd" + struct.pack(">IHHH", 6, 0, 1, TPQ)
    return header + b"MTrk" + struct.pack(">I", len(track)) + bytes(track)


C4, D4, E4, F4, G4, A4 = 60, 62, 64, 65, 67, 69

TWINKLE = (
    [(C4, 1), (C4, 1), (G4, 1), (G4, 1), (A4, 1), (A4, 1), (G4, 2)]
    + [(F4, 1), (F4, 1), (E4, 1), (E4, 1), (D4, 1), (D4, 1), (C4, 2)]
    + [(G4, 1), (G4, 1), (F4, 1), (F4, 1), (E4, 1), (E4, 1), (D4, 2)] * 2
    + [(C4, 1), (C4, 1), (G4, 1), (G4, 1), (A4, 1), (A4, 1), (G4, 2)]
    + [(F4, 1), (F4, 1), (E4, 1), (E4, 1), (D4, 1), (D4, 1), (C4, 2)]
)

ODE_TO_JOY = (
    [(E4, 1), (E4, 1), (F4, 1), (G4, 1), (G4, 1), (F4, 1), (E4, 1), (D4, 1),
     (C4, 1), (C4, 1), (D4, 1), (E4, 1), (E4, 1.5), (D4, 0.5), (D4, 2)]
    + [(E4, 1), (E4, 1), (F4, 1), (G4, 1), (G4, 1), (F4, 1), (E4, 1), (D4, 1),
       (C4, 1), (C4, 1), (D4, 1), (E4, 1), (D4, 1.5), (C4, 0.5), (C4, 2)]
)


def main():
    out_dir = pathlib.Path(__file__).resolve().parent.parent / "app" / "static" / "midi"
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "twinkle_twinkle.mid").write_bytes(midi_file(TWINKLE))
    (out_dir / "ode_to_joy.mid").write_bytes(midi_file(ODE_TO_JOY))
    print("Wrote", out_dir / "twinkle_twinkle.mid")
    print("Wrote", out_dir / "ode_to_joy.mid")


if __name__ == "__main__":
    main()
