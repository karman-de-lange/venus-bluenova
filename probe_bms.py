#!/usr/bin/env python3
"""Read-only probe for a BlueNova rack battery on a USB-RS485 adapter.

The rack does not talk until something asks. This sends only read requests
(Pace Modbus function 03, and Pylon/Pace ASCII analog and alarm reads) and
prints voltage, current, SOC, and whatever warning or protection bits are set.

Usage:
    python3 probe_bms.py
    python3 probe_bms.py --port /dev/ttyUSB0
"""

import argparse
import os
import select
import shlex
import sys
import termios
import time

PORT_DEFAULT = "/dev/ttyUSB0"
LOG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "probe-log.txt")

BAUD_CONST = {
    9600: termios.B9600,
    19200: termios.B19200,
    115200: termios.B115200,
}

# Pace Modbus register 9. Same positions are used for register 10, except
# where the protection list below overrides the name.
PACE_WARN_BITS = (
    (0, "cell overvoltage alarm"),
    (1, "cell undervoltage alarm"),
    (2, "pack overvoltage alarm"),
    (3, "pack undervoltage alarm"),
    (4, "charge overcurrent alarm"),
    (5, "discharge overcurrent alarm"),
    (8, "charge high temperature alarm"),
    (9, "discharge high temperature alarm"),
    (10, "charge low temperature alarm"),
    (11, "discharge low temperature alarm"),
    (12, "environment high temperature alarm"),
    (13, "environment low temperature alarm"),
    (14, "MOSFET high temperature alarm"),
    (15, "SOC low alarm"),
)
PACE_PROT_BITS = (
    (0, "cell overvoltage protection"),
    (1, "cell undervoltage protection"),
    (2, "pack overvoltage protection"),
    (3, "pack undervoltage protection"),
    (4, "charge overcurrent protection"),
    (5, "discharge overcurrent protection"),
    (6, "short circuit protection"),
    (7, "charger overvoltage protection"),
    (8, "charge high temperature protection"),
    (9, "discharge high temperature protection"),
    (10, "charge low temperature protection"),
    (11, "discharge low temperature protection"),
    (12, "MOSFET high temperature protection"),
    (13, "environment high temperature protection"),
    (14, "environment low temperature protection"),
)
PACE_STATUS_BITS = (
    (0, "charge MOSFET fault"),
    (1, "discharge MOSFET fault"),
    (2, "temperature sensor fault"),
    (4, "cell fault"),
    (5, "front-end sampling fault"),
    (8, "charging"),
    (9, "discharging"),
    (10, "charge MOSFET on"),
    (11, "discharge MOSFET on"),
    (12, "charge limiter on"),
    (14, "charger reversed"),
    (15, "heater on"),
)

PYLON_LEVEL = {
    0x00: "normal",
    0x01: "below lower limit",
    0x02: "above upper limit",
    0xF0: "other fault",
}
PYLON_RTN = {
    0x00: "ok",
    0x01: "version error",
    0x02: "checksum error",
    0x03: "length checksum error",
    0x04: "command not supported",
    0x05: "format error",
    0x06: "invalid data",
    0x90: "address error",
}


def bits(value, table):
    return [name for bit, name in table if value & (1 << bit)]


def hexdump(data):
    return data.hex(" ")


def modbus_crc(data):
    crc = 0xFFFF
    for byte in data:
        crc ^= byte
        for _ in range(8):
            if crc & 1:
                crc = (crc >> 1) ^ 0xA001
            else:
                crc >>= 1
    return crc


def modbus_read(slave, start, count, function=0x03):
    body = bytes(
        (
            slave,
            function,
            (start >> 8) & 0xFF,
            start & 0xFF,
            (count >> 8) & 0xFF,
            count & 0xFF,
        )
    )
    crc = modbus_crc(body)
    return body + bytes((crc & 0xFF, (crc >> 8) & 0xFF))


def pylon_length(info):
    """LENGTH field: high nibble is the checksum of the 12-bit ASCII length."""
    length = len(info)
    if length == 0:
        return 0
    total = (length & 0xF) + ((length >> 4) & 0xF) + ((length >> 8) & 0xF)
    check = (0x10 - (total % 16)) & 0xF
    return (check << 12) | length


def pylon_frame(version, address, cid2, info=b"", cid1=0x46):
    body = f"{version:02X}{address:02X}{cid1:02X}{cid2:02X}{pylon_length(info):04X}".encode()
    body += info
    checksum = (-sum(body)) & 0xFFFF
    return b"~" + body + f"{checksum:04X}".encode() + b"\r"


def self_check():
    # Pace ASCII alarm read, address 2, from the Pace protocol example.
    expected = bytes.fromhex("7E3235303234363434453030323032464432430D")
    got = pylon_frame(0x25, 0x02, 0x44, b"02")
    if got != expected:
        raise SystemExit(f"frame check failed: {got.hex()} != {expected.hex()}")
    # Modbus read holding register 0, one register, slave 1.
    if modbus_read(1, 0, 1) != bytes.fromhex("010300000001840A"):
        raise SystemExit("modbus crc check failed")


def ensure_access(port):
    try:
        fd = os.open(port, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
    except PermissionError:
        if os.environ.get("BLUENOVA_SG") == "1":
            raise SystemExit(
                f"Cannot open {port}. This login does not have the dialout group yet."
            )
        os.environ["BLUENOVA_SG"] = "1"
        script = os.path.abspath(__file__)
        command = " ".join(shlex.quote(part) for part in [sys.executable, script, *sys.argv[1:]])
        os.execvp("sg", ["sg", "dialout", "-c", command])
    except FileNotFoundError:
        raise SystemExit(f"{port} is not present. Plug in the USB-RS485 adapter.")
    else:
        os.close(fd)


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


def write_all(fd, payload):
    view = payload
    while view:
        sent = os.write(fd, view)
        view = view[sent:]
    termios.tcdrain(fd)


def read_response(fd, request, overall, quiet):
    """Read a reply. For a '~' request, wait until the reply's carriage return.

    A 160-byte frame at 9600 baud takes about 170 ms and arrives in USB
    packets, so a short silence is not the end of the frame. The request
    itself ends in CR, and some adapters echo it, so the CR that matters
    is the one after that echo.
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
    write_all(fd, payload)
    if rts_mode == "rts":
        set_rts(fd, False)
    elif rts_mode == "inv":
        set_rts(fd, True)
    # Modbus replies are short. Pylon analog replies are ~150 bytes at 9600.
    quiet = 0.08 if payload[:1] != b"~" else 0.2
    return read_response(fd, payload, overall, quiet)


def strip_echo(request, response):
    if response.startswith(request):
        return response[len(request) :]
    return response


def parse_modbus(request, response):
    response = strip_echo(request, response)
    slave = request[0]
    function = request[1]
    for index in range(max(0, len(response) - 4)):
        if response[index] != slave:
            continue
        seen = response[index + 1]
        if seen == function and index + 3 <= len(response):
            size = response[index + 2]
            end = index + 3 + size + 2
            if end <= len(response) and size % 2 == 0:
                frame = response[index:end]
                crc = modbus_crc(frame[:-2])
                if frame[-2] == (crc & 0xFF) and frame[-1] == ((crc >> 8) & 0xFF):
                    words = [
                        (frame[3 + i] << 8) | frame[4 + i] for i in range(0, size, 2)
                    ]
                    return {"kind": "modbus", "slave": slave, "words": words, "raw": frame}
        if seen == (function | 0x80) and index + 5 <= len(response):
            frame = response[index : index + 5]
            crc = modbus_crc(frame[:-2])
            if frame[-2] == (crc & 0xFF) and frame[-1] == ((crc >> 8) & 0xFF):
                return {
                    "kind": "modbus-exception",
                    "slave": slave,
                    "code": frame[2],
                    "raw": frame,
                }
    return None


def parse_pylon(response):
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
            info_ascii = body[12:]
            info = bytes.fromhex(info_ascii.decode()) if info_ascii else b""
        except ValueError:
            continue
        if len(info_ascii) != length:
            continue
        frames.append(
            {
                "kind": "pylon",
                "version": version,
                "address": address,
                "cid1": cid1,
                "rtn": rtn,
                "info": info,
                "raw": raw,
            }
        )
    return frames


def signed16(value):
    return value - 0x10000 if value & 0x8000 else value


def looks_like_pack(voltage, cells):
    live = [cell for cell in cells if 2000 <= cell <= 4500]
    return 30.0 <= voltage <= 70.0 and len(live) >= 8


def decode_pace(words):
    if len(words) < 13:
        return None
    current = signed16(words[0]) * 0.01
    voltage = words[1] * 0.01
    cells = list(words[15:31]) if len(words) >= 31 else []
    if not looks_like_pack(voltage, cells or [int(voltage / 16 * 1000)] * 8):
        return None
    temps = []
    for raw in words[31:37] if len(words) >= 37 else []:
        if raw in (0x0000, 0x7FFF, 0xFFFF):
            temps.append(None)
        else:
            temps.append(signed16(raw) * 0.1)
    live_cells = [(index + 1, millivolts) for index, millivolts in enumerate(cells) if millivolts]
    return {
        "protocol": "Pace Modbus",
        "current": current,
        "voltage": voltage,
        "soc": words[2],
        "soh": words[3],
        "remain_ah": words[4] * 0.01,
        "full_ah": words[5] * 0.01,
        "design_ah": words[6] * 0.01,
        "cycles": words[7],
        "warnings": bits(words[9], PACE_WARN_BITS),
        "warning_raw": words[9],
        "protections": bits(words[10], PACE_PROT_BITS),
        "protection_raw": words[10],
        "status": bits(words[11], PACE_STATUS_BITS),
        "status_raw": words[11],
        "cells": live_cells,
        "temps": temps,
    }


def level_name(value):
    return PYLON_LEVEL.get(value, f"0x{value:02X}")


def decode_pylon_analog(info):
    """Return a dict when the payload matches the usual 42H analog layout."""
    for skip in (1, 0):
        body = info[skip:]
        if len(body) < 4:
            continue
        module_count = body[0]
        cell_count = body[1]
        if not (1 <= cell_count <= 16 and module_count <= 16):
            cell_count = body[0]
            offset = 1
            if not 1 <= cell_count <= 16:
                continue
        else:
            offset = 2
        need = offset + cell_count * 2
        if len(body) < need + 1:
            continue
        cells = []
        for index in range(cell_count):
            pos = offset + index * 2
            cells.append((body[pos] << 8) | body[pos + 1])
        if not any(2000 <= cell <= 4500 for cell in cells):
            continue
        cursor = offset + cell_count * 2
        if cursor >= len(body):
            continue
        temp_count = body[cursor]
        cursor += 1
        if temp_count > 8 or cursor + temp_count * 2 + 4 > len(body):
            continue
        temps = []
        for _ in range(temp_count):
            raw = (body[cursor] << 8) | body[cursor + 1]
            cursor += 2
            temps.append(round((signed16(raw) - 2730) / 10.0, 1))
        if cursor + 4 > len(body):
            continue
        current = signed16((body[cursor] << 8) | body[cursor + 1]) / 100.0
        cursor += 2
        voltage_raw = (body[cursor] << 8) | body[cursor + 1]
        cursor += 2
        # Under 10000 the rack is reporting 10 mV steps (52.62 V is 0x148E).
        voltage = voltage_raw / 100.0 if voltage_raw < 10000 else voltage_raw / 1000.0
        remain = None
        full = None
        cycles = None
        soc = None
        soh = None
        if cursor + 2 <= len(body):
            remain = ((body[cursor] << 8) | body[cursor + 1]) / 100.0
            cursor += 2
        if cursor < len(body):
            cursor += 1  # user-defined count
        if cursor + 2 <= len(body):
            full = ((body[cursor] << 8) | body[cursor + 1]) / 100.0
            cursor += 2
        if cursor + 2 <= len(body):
            cycles = (body[cursor] << 8) | body[cursor + 1]
            cursor += 2
        if cursor < len(body) and body[cursor] <= 100:
            soc = body[cursor]
            cursor += 1
        if cursor < len(body) and body[cursor] <= 100:
            soh = body[cursor]
        return {
            "protocol": "Pylon/Pace ASCII",
            "current": current,
            "voltage": voltage,
            "soc": soc,
            "soh": soh,
            "remain_ah": remain,
            "full_ah": full,
            "cycles": cycles,
            "cells": [(index + 1, millivolts) for index, millivolts in enumerate(cells)],
            "temps": temps,
            "warnings": [],
            "protections": [],
            "status": [],
        }
    return None


def decode_pylon_alarm(info):
    """Cell and temperature states from a 44H alarm payload."""
    findings = []
    for skip in (1, 0):
        body = info[skip:]
        if len(body) < 4:
            continue
        cell_count = body[1] if body[0] <= 16 and 1 <= body[1] <= 16 else body[0]
        offset = 2 if cell_count == body[1] else 1
        if not 1 <= cell_count <= 16 or len(body) < offset + cell_count:
            continue
        states = body[offset : offset + cell_count]
        if not any(state in PYLON_LEVEL for state in states):
            continue
        for index, state in enumerate(states, start=1):
            if state:
                findings.append(f"cell {index} {level_name(state)}")
        cursor = offset + cell_count
        if cursor < len(body):
            temp_count = body[cursor]
            cursor += 1
            if 0 < temp_count <= 8 and cursor + temp_count <= len(body):
                for index, state in enumerate(body[cursor : cursor + temp_count], start=1):
                    if state:
                        findings.append(f"temperature sensor {index} {level_name(state)}")
                cursor += temp_count
        return findings
    return None


def cell_summary(cells):
    if not cells:
        return "no cell voltages"
    millivolts = [value for _, value in cells]
    low = min(cells, key=lambda item: item[1])
    high = max(cells, key=lambda item: item[1])
    spread = high[1] - low[1]
    return (
        f"{len(cells)} cells, {low[1]} mV (cell {low[0]}) to "
        f"{high[1]} mV (cell {high[0]}), spread {spread} mV"
    )


def explain_alarm(report):
    lines = []
    warnings = report.get("warnings") or []
    protections = report.get("protections") or []
    if protections:
        lines.append("Protection is active, so the ALM light is expected to stay on:")
        lines.extend(f"  - {item}" for item in protections)
    elif warnings:
        lines.append("A warning is active. The ALM light flashes for a warning and stays on for a protection:")
        lines.extend(f"  - {item}" for item in warnings)
    else:
        lines.append("No warning or protection bit was set in this reply.")
    cells = report.get("cells") or []
    if cells:
        low = min(value for _, value in cells)
        high = max(value for _, value in cells)
        if low < 2800:
            lines.append(f"Lowest cell is {low} mV. Under about 2850 mV this rack treats a cell as over-discharged.")
        if high > 3600:
            lines.append(f"Highest cell is {high} mV. Over about 3650 mV this rack stops charging that cell.")
        if high - low > 150:
            lines.append(f"Cell spread is {high - low} mV, which is large enough to raise an alarm on its own.")
    temps = [temp for temp in report.get("temps") or [] if temp is not None]
    if len(temps) >= 2:
        hottest = max(temps)
        cool = min(temps)
        if hottest >= 55 and hottest - cool >= 20:
            sensor = temps.index(hottest) + 1
            lines.append(
                f"Sensor {sensor} reads {hottest:.1f} C while the coolest sensor reads {cool:.1f} C. "
                "Current is not high enough to heat the pack, so this is a hot power component or a failing temperature sensor."
            )
    return lines


def format_report(report, slave_or_addr, baud):
    lines = [
        f"Protocol: {report['protocol']}",
        f"Link: {baud} baud, 8 data bits, no parity, 1 stop bit, address {slave_or_addr}",
        f"Pack: {report['voltage']:.2f} V, {report['current']:+.2f} A",
    ]
    if report.get("soc") is not None:
        lines.append(f"SOC: {report['soc']}%")
    if report.get("soh") is not None:
        lines.append(f"SOH: {report['soh']}%")
    if report.get("remain_ah") is not None and report.get("full_ah") is not None:
        lines.append(f"Capacity: {report['remain_ah']:.2f} / {report['full_ah']:.2f} Ah")
    if report.get("cycles") is not None:
        lines.append(f"Cycles: {report['cycles']}")
    lines.append("Cells: " + cell_summary(report.get("cells")))
    temps = [temp for temp in report.get("temps") or [] if temp is not None]
    if temps:
        lines.append("Temperatures: " + ", ".join(f"{temp:.1f} C" for temp in temps))
    if report.get("status"):
        lines.append("State: " + ", ".join(report["status"]))
    if "warning_raw" in report:
        lines.append(f"Warning bits: 0x{report['warning_raw']:04X}")
        lines.append(f"Protection bits: 0x{report['protection_raw']:04X}")
        lines.append(f"Status bits: 0x{report['status_raw']:04X}")
    if not report.get("cells") and report.get("analog_info"):
        rtn = report.get("analog_rtn")
        lines.append(f"Analog reply: {PYLON_RTN.get(rtn, rtn)} {hexdump(report['analog_info'][:120])}")
    if not report.get("warnings") and report.get("alarm_info"):
        rtn = report.get("alarm_rtn")
        lines.append(f"Alarm reply: {PYLON_RTN.get(rtn, rtn)} {hexdump(report['alarm_info'][:120])}")
    lines.extend(explain_alarm(report))
    return "\n".join(lines)


def probe_list():
    """Read-only requests, most likely first.

    This rack answered protocol version 0x21 at address 1, 9600 8N1.
    """
    items = []
    items.append(("pylon", "v21 addr 1 analog", pylon_frame(0x21, 1, 0x42, b"01")))
    for slave in (1, 2, 3):
        items.append(("modbus", f"slave {slave} holding 0..36", modbus_read(slave, 0, 37)))
    for version in (0x21, 0x20, 0x25):
        for address in (1, 2, 0):
            info = f"{address:02X}".encode()
            items.append(
                (
                    "pylon",
                    f"v{version:02X} addr {address} analog",
                    pylon_frame(version, address, 0x42, info),
                )
            )
            items.append(
                (
                    "pylon",
                    f"v{version:02X} addr {address} alarm",
                    pylon_frame(version, address, 0x44, info),
                )
            )
    for version in (0x21, 0x20, 0x25):
        items.append(("pylon", f"v{version:02X} system analog", pylon_frame(version, 2, 0x61)))
        items.append(("pylon", f"v{version:02X} system alarm", pylon_frame(version, 2, 0x62)))
    items.append(("modbus", "slave 1 input 0..16", modbus_read(1, 0, 17, function=0x04)))
    return items


def classify(kind, request, response):
    if kind == "modbus":
        return parse_modbus(request, response)
    frames = parse_pylon(strip_echo(request, response))
    return frames[0] if frames else None


def run_pass(port, baud, rts_mode, probes, log):
    fd = open_port(port, baud)
    found = []
    try:
        for kind, label, request in probes:
            response = transact(fd, request, rts_mode)
            response = strip_echo(request, response)
            if not response:
                continue
            parsed = classify(kind, request, response)
            line = f"{baud} {rts_mode} {label}: {len(response)} bytes {hexdump(response[:80])}"
            print(line)
            log.append(line)
            if parsed:
                parsed["label"] = label
                parsed["baud"] = baud
                parsed["rts"] = rts_mode
                parsed["request"] = request
                found.append(parsed)
                return found
        return found
    finally:
        os.close(fd)


def deepen(port, parsed):
    """Once a protocol answers, read the analog block and the alarm block."""
    baud = parsed["baud"]
    fd = open_port(port, baud)
    try:
        if parsed["kind"] == "modbus":
            request = modbus_read(parsed["slave"], 0, 37)
            response = transact(fd, request, parsed.get("rts", "auto"), overall=0.6)
            got = parse_modbus(request, response)
            if not got:
                return None
            report = decode_pace(got["words"])
            if report:
                report["address"] = parsed["slave"]
                report["baud"] = baud
            return report
        if parsed["kind"] == "pylon" and parsed["rtn"] in (0x00, 0x01):
            # A version-error reply still carries the version the BMS speaks.
            version = parsed["version"]
            address = parsed["address"]
            info = f"{address:02X}".encode()
            analog_req = pylon_frame(version, address, 0x42, info)
            alarm_req = pylon_frame(version, address, 0x44, info)
            mode = parsed.get("rts", "auto")
            analog = parse_pylon(strip_echo(analog_req, transact(fd, analog_req, mode, 0.6)))
            alarm = parse_pylon(strip_echo(alarm_req, transact(fd, alarm_req, mode, 0.6)))
            report = None
            if analog and analog[0]["rtn"] == 0x00:
                report = decode_pylon_analog(analog[0]["info"])
            if report is None:
                report = {
                    "protocol": "Pylon/Pace ASCII",
                    "current": 0.0,
                    "voltage": 0.0,
                    "cells": [],
                    "temps": [],
                    "warnings": [],
                    "protections": [],
                    "status": [],
                }
            if alarm and alarm[0]["rtn"] == 0x00:
                findings = decode_pylon_alarm(alarm[0]["info"]) or []
                report["warnings"] = findings
            if analog:
                report["analog_rtn"] = analog[0]["rtn"]
                report["analog_info"] = analog[0]["info"]
            if alarm:
                report["alarm_rtn"] = alarm[0]["rtn"]
                report["alarm_info"] = alarm[0]["info"]
            report["address"] = address
            report["baud"] = baud
            report["pylon_rtn"] = PYLON_RTN.get(parsed["rtn"], hex(parsed["rtn"]))
            return report
        if parsed["kind"] == "pylon":
            return {
                "protocol": "Pylon/Pace ASCII",
                "current": 0.0,
                "voltage": 0.0,
                "cells": [],
                "temps": [],
                "warnings": [],
                "protections": [PYLON_RTN.get(parsed["rtn"], f"return 0x{parsed['rtn']:02X}")],
                "status": [],
                "address": parsed["address"],
                "baud": baud,
            }
    finally:
        os.close(fd)
    return None


def main():
    parser = argparse.ArgumentParser(description="Read a BlueNova rack BMS over RS485.")
    parser.add_argument("--port", default=PORT_DEFAULT)
    args = parser.parse_args()
    self_check()
    ensure_access(args.port)

    log = []
    probes = probe_list()
    quick = probes[:8]
    print(f"Probing {args.port}. Read-only. The rack's ALM light is a BMS alarm.")
    found = []
    for baud in (9600, 115200, 19200):
        print(f"Listening and asking at {baud} baud...")
        found = run_pass(args.port, baud, "auto", probes, log)
        if found:
            break
    if not found:
        print("No answer with automatic direction control. Trying RTS direction control.")
        for baud in (9600, 115200):
            for mode in ("rts", "inv"):
                print(f"Asking at {baud} baud, direction {mode}...")
                found = run_pass(args.port, baud, mode, quick, log)
                if found:
                    break
            if found:
                break

    print()
    if not found:
        text = "\n".join(log) if log else "No bytes came back from any read request."
        summary = [
            "The rack did not answer.",
            "",
            "The adapter is working and the requests were sent. A BlueNova RacPower only",
            "replies on its RS485 jacks, and only when the cable is on those jacks.",
            "The CAN jacks use different pins and stay silent on this adapter.",
            "",
            "RS485 pinout on the battery RJ45: pin 1 and 8 = A, pin 2 and 7 = B,",
            "pin 3 and 6 = ground. If A and B are swapped, the rack stays silent.",
            "Some packs also need the RS485 protocol turned on with the vendor PC tool.",
            "",
            text,
        ]
        report = "\n".join(summary)
        print(report)
        with open(LOG_PATH, "w", encoding="utf-8") as handle:
            handle.write(report + "\n")
        print(f"\nLog written to {LOG_PATH}")
        return 1

    parsed = found[0]
    if parsed["kind"] == "modbus-exception":
        report = (
            f"Slave {parsed['slave']} answered Modbus at {parsed['baud']} baud "
            f"with exception code {parsed['code']}. The link works. "
            "The register read was rejected."
        )
    else:
        detailed = deepen(args.port, parsed)
        if detailed:
            report = format_report(detailed, detailed.get("address"), detailed.get("baud"))
        elif parsed["kind"] == "modbus":
            report = (
                f"Slave {parsed['slave']} answered Modbus at {parsed['baud']} baud, "
                f"but the values are not a 16-cell pack.\nRaw words: {parsed['words']}"
            )
        else:
            rtn = PYLON_RTN.get(parsed.get("rtn"), "?")
            report = (
                f"ASCII reply at {parsed['baud']} baud from address {parsed.get('address')}, "
                f"return {rtn}.\nInfo: {hexdump(parsed.get('info', b''))}"
            )
    print(report)
    with open(LOG_PATH, "w", encoding="utf-8") as handle:
        handle.write(report + "\n\n")
        handle.write("\n".join(log) + "\n")
    print(f"\nLog written to {LOG_PATH}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
