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
import shlex
import sys
import time

import pylon

PORT_DEFAULT = "/dev/ttyUSB0"
LOG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "probe-log.txt")

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


def self_check():
    # Pace ASCII alarm read, address 2, from the Pace protocol example.
    expected = bytes.fromhex("7E3235303234363434453030323032464432430D")
    got = pylon.frame(0x25, 0x02, 0x44, b"02")
    if got != expected:
        raise SystemExit(f"frame check failed: {got.hex()} != {expected.hex()}")
    # Modbus read holding register 0, one register, slave 1.
    if modbus_read(1, 0, 1) != bytes.fromhex("010300000001840A"):
        raise SystemExit("modbus crc check failed")
    pylon.self_check()
    _check_driver()


class _Paths:
    """Enough of VeDbusService for the publish check. add_path is startup, not a poll write."""

    def __init__(self):
        self.rows = []

    def add_path(self, path, value):
        return None

    def register(self):
        return None

    def __setitem__(self, path, value):
        self.rows.append((path, value))


def _check_driver():
    """Publish gate, cell-index guard, and a 0x51 miss that must not look like a dead link."""
    import dbus_bluenova

    wire = bytes.fromhex(
        "01100CF40CEF0CF00CF60CF10CF00CF60CF20CED0CF70CF60CEC0CF20CF50CF00CF1"
        "070B9C0B9B0B9E0B9C0BCF0BCF0BB1FEA514B5251A04271000FC5F640000"
    ).hex().upper().encode()
    analog = pylon.decode_analog(wire)
    if analog is None:
        raise SystemExit("documented analog did not decode for the driver check")
    voltage = round(analog["voltage"], 2)
    current = round(analog["current"], 2)
    soc = int(analog["soc"])
    limit_paths = (
        "/Info/MaxChargeVoltage",
        "/Info/MaxChargeCurrent",
        "/Info/MaxDischargeCurrent",
        "/Info/BatteryLowVoltage",
        "/Io/AllowToCharge",
        "/Io/AllowToDischarge",
        "/Alarms/LowCellVoltage",
        "/Alarms/HighCellVoltage",
        "/Alarms/LowTemperature",
        "/Alarms/HighTemperature",
    )

    def require_measurements(entries, label):
        got = dict(entries)
        expected = {
            "/Dc/0/Voltage": voltage,
            "/Dc/0/Current": current,
            "/Soc": soc,
            "/Connected": 1,
        }
        for path, value in expected.items():
            if got.get(path) != value:
                raise SystemExit(f"{label} did not publish {path}={value}: {entries}")
        for path, value in entries:
            if path in limit_paths:
                raise SystemExit(f"{label} wrote {path}={value}")

    require_measurements(dbus_bluenova.publications(analog, None), "missing alarm")
    require_measurements(dbus_bluenova.publications(analog, {"cells": [], "temps": []}), "empty alarm")
    healthy = dbus_bluenova.publications(analog, {"cells": [0] * 16, "temps": [0] * 7})
    got = dict(healthy)
    expected = {
        "/Info/MaxChargeCurrent": 50.0,
        "/Info/MaxDischargeCurrent": 100.0,
        "/Info/BatteryLowVoltage": 46.5,
        "/Info/MaxChargeVoltage": 55.0,
        "/Io/AllowToCharge": 1,
        "/Io/AllowToDischarge": 1,
        "/Alarms/LowCellVoltage": 0,
        "/Alarms/HighCellVoltage": 0,
        "/Alarms/LowTemperature": 0,
        "/Alarms/HighTemperature": 0,
    }
    for path, value in expected.items():
        if got.get(path) != value:
            raise SystemExit(f"healthy alarm wrote {path}={got.get(path)!r}, want {value!r}")
    order = [path for path, _ in healthy]
    voltage_at = order.index("/Info/MaxChargeVoltage")
    if order.index("/Info/MaxChargeCurrent") > voltage_at or order.index("/Info/MaxDischargeCurrent") > voltage_at:
        raise SystemExit(f"current limits followed charge voltage: {order}")
    require_measurements(dbus_bluenova.publications(analog, None), "alarm gap")

    cases = (
        ("cells", 0x02, 0, 100.0),
        ("temps", 0x02, 0, 100.0),
        ("cells", 0x01, 50.0, 0),
        ("temps", 0x01, 50.0, 0),
        ("cells", 0xF0, 0, 0),
        ("temps", 0xF0, 0, 0),
        ("cells", 0x03, 50.0, 100.0),
        ("temps", 0x03, 50.0, 100.0),
    )
    for where, code, charge, discharge in cases:
        alarm = {"cells": [0] * 16, "temps": [0] * 7}
        alarm[where][0] = code
        got = dict(dbus_bluenova.publications(analog, alarm))
        if got.get("/Info/MaxChargeCurrent") != charge or got.get("/Info/MaxDischargeCurrent") != discharge:
            raise SystemExit(
                f"{where} 0x{code:02X} wrote charge {got.get('/Info/MaxChargeCurrent')!r} "
                f"discharge {got.get('/Info/MaxDischargeCurrent')!r}"
            )

    wide = dict(analog)
    wide["cells"] = [(index, 3000 + index) for index in range(1, 18)]
    tail = dbus_bluenova.publications(wide, {"cells": [0] * 16, "temps": [0]})
    if ("/Voltages/Cell1", 3.001) not in tail or ("/Voltages/Cell16", 3.016) not in tail:
        raise SystemExit(f"cell guard dropped a published cell: {tail}")
    if any(path == "/Voltages/Cell17" for path, _ in tail):
        raise SystemExit("cell 17 was published")

    service = _Paths()
    battery = dbus_bluenova.Battery("/dev/unused", service, 0)
    battery.mark_down()
    if service.rows != [("/Connected", 0)]:
        raise SystemExit(f"mark_down wrote {service.rows}")

    script = {"identity": "raise"}
    closed = []

    def fail_open():
        raise SystemExit("poll opened a serial port")

    def ask(cid2, info=b""):
        if cid2 == 0x42:
            return {"rtn": 0x00, "info": wire}
        if cid2 == 0x44:
            return None
        if cid2 == 0x51:
            if script["identity"] == "raise":
                raise TimeoutError("identity timeout")
            return script["identity"]
        raise SystemExit(f"driver sent 0x{cid2:02X}")

    battery.fd = "sentinel"
    battery.open = fail_open
    battery.close = lambda: closed.append(True)
    battery.ask = ask
    notes = []

    def note(message, *args, **kwargs):
        notes.append(message % args if args else message)

    previous = dbus_bluenova.log.warning
    dbus_bluenova.log.warning = note
    try:
        identities = (
            "raise",
            None,
            {"rtn": 0x00, "info": b"short"},
        )
        for identity in identities:
            script["identity"] = identity
            battery.fails = 2
            battery.named = False
            closed.clear()
            start = len(service.rows)
            if battery.poll() is not True:
                raise SystemExit("poll did not return true")
            if battery.fails != 0:
                raise SystemExit(f"identity miss counted as a link failure: {battery.fails}")
            if closed:
                raise SystemExit("identity miss closed the port")
            if battery.named:
                raise SystemExit("identity miss set named")
            tail = service.rows[start:]
            if ("/Dc/0/Voltage", voltage) not in tail:
                raise SystemExit(f"poll did not publish voltage: {tail}")
            if tail[-1:] == [("/Connected", 0)] or any(path in limit_paths for path, _ in tail):
                raise SystemExit(f"poll with no alarm wrote limits or marked down: {tail}")
    finally:
        dbus_bluenova.log.warning = previous
    soft = [line for line in notes if line == "identity reply missing or did not decode"]
    if len(soft) != 1:
        raise SystemExit(f"soft identity miss was logged {len(soft)} times: {notes}")
    if not any(line.startswith("identity read failed:") for line in notes):
        raise SystemExit(f"identity timeout was not logged: {notes}")


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


def parse_modbus(request, response):
    response = pylon.strip_echo(request, response)
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


def looks_like_pack(voltage, cells):
    live = [cell for cell in cells if 2000 <= cell <= 4500]
    return 30.0 <= voltage <= 70.0 and len(live) >= 8


def decode_pace(words):
    if len(words) < 13:
        return None
    current = pylon.signed16(words[0]) * 0.01
    voltage = words[1] * 0.01
    cells = list(words[15:31]) if len(words) >= 31 else []
    if not looks_like_pack(voltage, cells or [int(voltage / 16 * 1000)] * 8):
        return None
    temps = []
    for raw in words[31:37] if len(words) >= 37 else []:
        if raw in (0x0000, 0x7FFF, 0xFFFF):
            temps.append(None)
        else:
            temps.append(pylon.signed16(raw) * 0.1)
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
    items.append(("pylon", "v21 addr 1 analog", pylon.frame(0x21, 1, 0x42, b"01")))
    for slave in (1, 2, 3):
        items.append(("modbus", f"slave {slave} holding 0..36", modbus_read(slave, 0, 37)))
    for version in (0x21, 0x20, 0x25):
        for address in (1, 2, 0):
            info = f"{address:02X}".encode()
            items.append(
                (
                    "pylon",
                    f"v{version:02X} addr {address} analog",
                    pylon.frame(version, address, 0x42, info),
                )
            )
            items.append(
                (
                    "pylon",
                    f"v{version:02X} addr {address} alarm",
                    pylon.frame(version, address, 0x44, info),
                )
            )
    for version in (0x21, 0x20, 0x25):
        items.append(("pylon", f"v{version:02X} system analog", pylon.frame(version, 2, 0x61)))
        items.append(("pylon", f"v{version:02X} system alarm", pylon.frame(version, 2, 0x62)))
    items.append(("modbus", "slave 1 input 0..16", modbus_read(1, 0, 17, function=0x04)))
    return items


def classify(kind, request, response):
    if kind == "modbus":
        return parse_modbus(request, response)
    frames = pylon.parse(pylon.strip_echo(request, response))
    return frames[0] if frames else None


def run_pass(port, baud, rts_mode, probes, log):
    fd = pylon.open_port(port, baud)
    found = []
    try:
        for kind, label, request in probes:
            response = pylon.transact(fd, request, rts_mode)
            response = pylon.strip_echo(request, response)
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


def alarm_findings(info):
    states = pylon.decode_alarm(info)
    if not states:
        return []
    findings = []
    for index, state in enumerate(states["cells"], start=1):
        if state:
            findings.append(f"cell {index} {pylon.level_name(state)}")
    for index, state in enumerate(states["temps"], start=1):
        if state:
            findings.append(f"temperature sensor {index} {pylon.level_name(state)}")
    return findings


def deepen(port, parsed):
    """Once a protocol answers, read the analog block and the alarm block."""
    baud = parsed["baud"]
    fd = pylon.open_port(port, baud)
    try:
        if parsed["kind"] == "modbus":
            request = modbus_read(parsed["slave"], 0, 37)
            response = pylon.transact(fd, request, parsed.get("rts", "auto"), overall=0.6)
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
            analog_req = pylon.frame(version, address, 0x42, info)
            alarm_req = pylon.frame(version, address, 0x44, info)
            mode = parsed.get("rts", "auto")
            analog = pylon.parse(pylon.strip_echo(analog_req, pylon.transact(fd, analog_req, mode, 0.6)))
            alarm = pylon.parse(pylon.strip_echo(alarm_req, pylon.transact(fd, alarm_req, mode, 0.6)))
            report = None
            if analog and analog[0]["rtn"] == 0x00:
                report = pylon.decode_analog(analog[0]["info"])
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
            else:
                report["protocol"] = "Pylon/Pace ASCII"
            if alarm and alarm[0]["rtn"] == 0x00:
                findings = alarm_findings(alarm[0]["info"])
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
