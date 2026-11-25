"""Pylon RS485 frames for this BlueNova rack, and the port they travel on.

One layout. parse keeps INFO as the ASCII bytes from the wire. CID2 0x42 and
0x44 are hex and are decoded from their count bytes. CID2 0x51 stays raw text.
"""

import os
import select
import termios
import time

BAUD_CONST = {
    9600: termios.B9600,
    19200: termios.B19200,
    115200: termios.B115200,
}

LEVEL = {
    0x00: "normal",
    0x01: "below lower limit",
    0x02: "above upper limit",
    0xF0: "other fault",
}


def level_name(value):
    return LEVEL.get(value, f"0x{value:02X}")


def signed16(value):
    return value - 0x10000 if value & 0x8000 else value


def _length(info):
    """LENGTH field: high nibble is the checksum of the 12-bit ASCII length."""
    length = len(info)
    if length == 0:
        return 0
    total = (length & 0xF) + ((length >> 4) & 0xF) + ((length >> 8) & 0xF)
    check = (0x10 - (total % 16)) & 0xF
    return (check << 12) | length


def frame(version, address, cid2, info=b"", cid1=0x46):
    body = f"{version:02X}{address:02X}{cid1:02X}{cid2:02X}{_length(info):04X}".encode()
    body += info
    checksum = (-sum(body)) & 0xFFFF
    return b"~" + body + f"{checksum:04X}".encode() + b"\r"


def parse(response):
    """Return checksum-valid frames. INFO is the ASCII bytes that were on the wire."""
    frames = []
    start = 0
    while True:
        begin = response.find(b"~", start)
        if begin < 0:
            return frames
        end = response.find(b"\r", begin)
        if end < 0:
            return frames
        raw = response[begin : end + 1]
        start = end + 1
        body = raw[1:-5]
        try:
            checksum = int(raw[-5:-1], 16)
        except ValueError:
            continue
        if ((-sum(body)) & 0xFFFF) != checksum or len(body) < 12:
            continue
        try:
            version = int(body[0:2], 16)
            address = int(body[2:4], 16)
            cid1 = int(body[4:6], 16)
            rtn = int(body[6:8], 16)
            length = int(body[8:12], 16) & 0x0FFF
            info = body[12:]
        except ValueError:
            continue
        if len(info) != length:
            continue
        frames.append(
            {
                "kind": "pylon",
                "version": version,
                "address": address,
                "cid1": cid1,
                "rtn": rtn,
                "info": bytes(info),
                "raw": raw,
            }
        )
    return frames


def _info_bytes(info):
    """0x42 and 0x44 carry ASCII hex. Identity text is not passed through here."""
    try:
        return bytes.fromhex(info.decode("ascii"))
    except (UnicodeDecodeError, ValueError):
        return None


def decode_analog(info):
    """CID2 0x42. Address echo, cell count, then temperatures and the pack words.

    The byte after remaining capacity is one fixed byte. It is not a length.
    """
    info = _info_bytes(info)
    if info is None or len(info) < 4:
        return None
    cell_count = info[1]
    cursor = 2
    if cell_count < 1 or cursor + cell_count * 2 >= len(info):
        return None
    cells = []
    for _ in range(cell_count):
        cells.append((info[cursor] << 8) | info[cursor + 1])
        cursor += 2
    temp_count = info[cursor]
    cursor += 1
    # temperatures, current, voltage, remain, the fixed byte, full, cycles, SOC, SOH
    tail = temp_count * 2 + 13
    if cursor + tail > len(info):
        return None
    temps = []
    for _ in range(temp_count):
        raw = (info[cursor] << 8) | info[cursor + 1]
        cursor += 2
        temps.append(round((signed16(raw) - 2730) / 10.0, 1))
    current = signed16((info[cursor] << 8) | info[cursor + 1]) / 100.0
    cursor += 2
    voltage_raw = (info[cursor] << 8) | info[cursor + 1]
    cursor += 2
    # Under 10000 the rack reports 10 mV steps (52.62 V is 0x148E).
    voltage = voltage_raw / 100.0 if voltage_raw < 10000 else voltage_raw / 1000.0
    remain = ((info[cursor] << 8) | info[cursor + 1]) / 100.0
    cursor += 2
    cursor += 1  # one fixed byte, not a user-defined length
    full = ((info[cursor] << 8) | info[cursor + 1]) / 100.0
    cursor += 2
    cycles = (info[cursor] << 8) | info[cursor + 1]
    cursor += 2
    soc = info[cursor]
    soh = info[cursor + 1]
    return {
        "current": current,
        "voltage": voltage,
        "soc": soc,
        "soh": soh,
        "remain_ah": remain,
        "full_ah": full,
        "cycles": cycles,
        "cells": [(index + 1, millivolts) for index, millivolts in enumerate(cells)],
        "temps": temps,
    }


def decode_alarm(info):
    """CID2 0x44. Address echo, cell count, one state byte per cell, then temperatures.

    The bytes after the temperature states are not charge-control flags.
    """
    info = _info_bytes(info)
    if info is None or len(info) < 4:
        return None
    cell_count = info[1]
    cursor = 2
    if cell_count < 1 or cursor + cell_count >= len(info):
        return None
    cells = list(info[cursor : cursor + cell_count])
    cursor += cell_count
    temp_count = info[cursor]
    cursor += 1
    if cursor + temp_count > len(info):
        return None
    temps = list(info[cursor : cursor + temp_count])
    return {"cells": cells, "temps": temps}


def decode_identity(info):
    """CID2 0x51. Twenty characters of model, four of project, then the maker."""
    text = info.decode("ascii", "replace") if isinstance(info, (bytes, bytearray)) else info
    if len(text) < 24:
        return None
    model = text[:20].strip()
    project = text[20:24].strip()
    maker = text[24:].strip()
    if not model:
        return None
    return model, project, maker


def set_rts(fd, on):
    import array
    import fcntl

    flags = array.array("i", [0])
    fcntl.ioctl(fd, termios.TIOCMGET, flags)
    if on:
        flags[0] |= termios.TIOCM_RTS
    else:
        flags[0] &= ~termios.TIOCM_RTS
    fcntl.ioctl(fd, termios.TIOCMSET, flags)


def open_port(port, baud):
    fd = os.open(port, os.O_RDWR | os.O_NOCTTY)
    attrs = termios.tcgetattr(fd)
    speed = BAUD_CONST[baud]
    attrs[0] = 0
    attrs[1] = 0
    attrs[2] = termios.CS8 | termios.CREAD | termios.CLOCAL | speed
    attrs[3] = 0
    attrs[4] = speed
    attrs[5] = speed
    attrs[6][termios.VMIN] = 0
    attrs[6][termios.VTIME] = 0
    termios.tcsetattr(fd, termios.TCSANOW, attrs)
    termios.tcflush(fd, termios.TCIOFLUSH)
    return fd


def _write_all(fd, payload):
    view = payload
    while view:
        sent = os.write(fd, view)
        view = view[sent:]
    termios.tcdrain(fd)


def _read_response(fd, request, overall, quiet):
    """Read until the reply's CR. A short silence is not the end of a Pylon frame.

    The request itself ends in CR, and some adapters echo it, so the CR that
    matters is the one after that echo.
    """
    data = bytearray()
    deadline = time.monotonic() + overall
    pylon = request[:1] == b"~"
    while time.monotonic() < deadline:
        wait = deadline - time.monotonic()
        ready, _, _ = select.select([fd], [], [], wait)
        if not ready:
            break
        chunk = os.read(fd, 4096)
        if not chunk:
            break
        data += chunk
        payload = bytes(data)
        if payload.startswith(request):
            payload = payload[len(request) :]
        if pylon and b"\r" in payload:
            break
        if not pylon and data:
            deadline = min(deadline, time.monotonic() + quiet)
    return bytes(data)


def transact(fd, payload, rts_mode, overall=1.0):
    termios.tcflush(fd, termios.TCIFLUSH)
    if rts_mode == "rts":
        set_rts(fd, True)
    elif rts_mode == "inv":
        set_rts(fd, False)
    _write_all(fd, payload)
    if rts_mode == "rts":
        set_rts(fd, False)
    elif rts_mode == "inv":
        set_rts(fd, True)
    quiet = 0.08 if payload[:1] != b"~" else 0.2
    return _read_response(fd, payload, overall, quiet)


def strip_echo(request, response):
    if response.startswith(request):
        return response[len(request) :]
    return response


def self_check():
    """Captured frames. 0x42 and 0x44 INFO is wire ASCII; decode hex-decodes it."""
    documented = bytes.fromhex(
        "01100CF40CEF0CF00CF60CF10CF00CF60CF20CED0CF70CF60CEC0CF20CF50CF00CF1"
        "070B9C0B9B0B9E0B9C0BCF0BCF0BB1FEA514B5251A04271000FC5F640000"
    )
    wire = documented.hex().upper().encode()
    measurements = {
        "current",
        "voltage",
        "soc",
        "soh",
        "remain_ah",
        "full_ah",
        "cycles",
        "cells",
        "temps",
    }
    got = decode_analog(wire)
    if (
        got is None
        or set(got) != measurements
        or got["voltage"] != 53.01
        or got["soc"] != 95
        or round(got["current"], 2) != -3.47
        or len(got["cells"]) != 16
    ):
        raise SystemExit(f"documented analog frame decoded wrong: {got}")
    shifted = bytes.fromhex(
        "01100d080d0a0d050d070d0a0d080d070d090d090d090d0a0d090d070d080d0a0d09"
        "070bac0bab0baf0bab0bdb0bdb0bb9005f14d920f3042710010554640000"
    )
    got = decode_analog(shifted.hex().upper().encode())
    if got is None or round(got["voltage"], 2) != 53.37 or got["soc"] != 84 or len(got["cells"]) != 16:
        raise SystemExit(f"live analog frame decoded wrong: {got}")
    alarm = ("0110" + "00" * 16 + "07" + "00" * 7 + "0000080006400000000000").encode()
    states = decode_alarm(alarm)
    if states != {"cells": [0] * 16, "temps": [0] * 7}:
        raise SystemExit(f"alarm frame decoded wrong: {states}")
    # Wire INFO is ASCII hex. A fromhex failure must not fall back to raw bytes.
    if decode_analog(documented) is not None or decode_alarm(bytes.fromhex(alarm.decode())) is not None:
        raise SystemExit("decoder walked raw bytes instead of wire ASCII")
    parsed = parse(frame(0x21, 1, 0x00, wire))
    if not parsed or parsed[0]["info"] != wire:
        raise SystemExit(f"hex INFO did not round-trip as wire ASCII: {parsed}")
    got = decode_analog(parsed[0]["info"])
    if (
        got is None
        or got["voltage"] != 53.01
        or got["soc"] != 95
        or round(got["current"], 2) != -3.47
        or len(got["cells"]) != 16
    ):
        raise SystemExit(f"hex INFO did not decode: {got}")
    identity = b"TB-48100-CAN" + b" " * 8 + b"BN07Topband"
    parsed = parse(frame(0x21, 1, 0x51, identity))
    if (
        not parsed
        or parsed[0]["info"] != identity
        or decode_identity(parsed[0]["info"]) != ("TB-48100-CAN", "BN07", "Topband")
    ):
        raise SystemExit(f"identity frame decoded wrong: {parsed}")
