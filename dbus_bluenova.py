#!/usr/bin/env python3
"""Publish the BlueNova RacPower pack as a Venus OS battery.

Read-only on the wire. Polls Pylon-family commands 0x42 and 0x44. The pack
does not report a charge target, so the DVCC limits are the RacPower manual
values: 55.0 V, 50 A charge, 100 A discharge, 46.5 V low. An alarm byte
forces the matching current to zero. The 0x47 protection ceiling is not used.

Unknown measurements stay invalid. A zero on /Info/MaxDischargeCurrent is a
command to stop the inverter, and /Info/MaxChargeVoltage is what makes Venus
treat this service as the controlling BMS. Neither is published until a real
frame has been decoded. A lost reply does not send that stop command.
"""

import logging
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, "/opt/victronenergy/dbus-systemcalc-py/ext/velib_python")

from dbus.mainloop.glib import DBusGMainLoop
from gi.repository import GLib
from vedbus import VeDbusService

import pylon

VERSION = "1.0"
CHARGE_VOLTAGE = 55.0
CHARGE_CURRENT = 50.0
DISCHARGE_CURRENT = 100.0
LOW_VOLTAGE = 46.5
FAILS_BEFORE_DOWN = 3
POLL_MS = 1000

log = logging.getLogger("bluenova")


def blocked(states, codes):
    return any(state in codes for state in states)


def cell_summary(cells):
    if not cells:
        return "no cell voltages"
    low = min(cells, key=lambda item: item[1])
    high = max(cells, key=lambda item: item[1])
    return (
        f"{len(cells)} cells, {low[1]} mV (cell {low[0]}) to "
        f"{high[1]} mV (cell {high[0]}), spread {high[1] - low[1]} mV"
    )


class Battery:
    def __init__(self, port):
        self.port = port
        self.fd = None
        self.fails = 0
        self.named = False
        self.ticks = 0
        name = "com.victronenergy.battery." + os.path.basename(port)
        self.service = VeDbusService(name, register=False)
        self._add_paths()
        self.service.register()
        log.info("registered %s", name)

    def _instance(self):
        path = "/data/bluenova/device-instance"
        try:
            with open(path, encoding="ascii") as handle:
                return int(handle.read().strip())
        except (OSError, ValueError):
            os.makedirs("/data/bluenova", exist_ok=True)
            with open(path, "w", encoding="ascii") as handle:
                handle.write("0\n")
            return 0

    def _add_paths(self):
        add = self.service.add_path
        add("/Mgmt/ProcessName", "dbus-bluenova")
        add("/Mgmt/ProcessVersion", VERSION)
        add("/Mgmt/Connection", "RS485 " + self.port)
        add("/DeviceInstance", self._instance())
        add("/ProductId", 0xBA77)
        add("/ProductName", "BlueNova RacPower")
        add("/FirmwareVersion", "0x21")
        add("/HardwareVersion", "")
        add("/Serial", "")
        add("/Manufacturer", "Topband")
        add("/Connected", 0)
        add("/Soc", None)
        add("/Soh", None)
        add("/Capacity", None)
        add("/InstalledCapacity", None)
        add("/ConsumedAmphours", None)
        add("/History/ChargeCycles", None)
        add("/Dc/0/Voltage", None)
        add("/Dc/0/Current", None)
        add("/Dc/0/Power", None)
        add("/Dc/0/Temperature", None)
        # MaxChargeVoltage is what makes systemcalc select this service as the
        # BMS. Leave it invalid until apply() has real current limits to go with it.
        add("/Info/MaxChargeVoltage", None)
        add("/Info/MaxChargeCurrent", None)
        add("/Info/MaxDischargeCurrent", None)
        add("/Info/BatteryLowVoltage", None)
        add("/Io/AllowToCharge", None)
        add("/Io/AllowToDischarge", None)
        add("/System/MinCellVoltage", None)
        add("/System/MaxCellVoltage", None)
        add("/System/MinCellTemperature", None)
        add("/System/MaxCellTemperature", None)
        add("/System/NrOfCells", 16)
        for alarm in (
            "LowCellVoltage",
            "HighCellVoltage",
            "LowTemperature",
            "HighTemperature",
        ):
            add("/Alarms/" + alarm, 0)
        for cell in range(1, 17):
            add(f"/Voltages/Cell{cell}", None)

    def _set(self, path, value):
        self.service[path] = value

    def mark_down(self):
        # Connected=0 tells the GUI the link is down. The current limits stay
        # at the last real values. Writing 0 there would stop the inverter.
        self._set("/Connected", 0)

    def close(self):
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None

    def open(self):
        self.fd = pylon.open_port(self.port, 9600)
        self.named = False
        log.info("opened %s at 9600 8N1", self.port)

    def ask(self, cid2, info=b""):
        request = pylon.frame(0x21, 1, cid2, info)
        raw = pylon.transact(self.fd, request, "auto", 1.0)
        frames = pylon.parse(pylon.strip_echo(request, raw))
        for frame in frames:
            if frame["rtn"] == 0x00:
                return frame
        return None

    def apply_name(self):
        frame = self.ask(0x51)
        found = pylon.decode_identity(frame["info"]) if frame else None
        if not found:
            return
        model, project, maker = found
        self._set("/HardwareVersion", f"{model} {project}".strip())
        self._set("/Serial", f"{model}-{project}" if project else model)
        if maker:
            self._set("/Manufacturer", maker)
        self.named = True
        log.info("pack %s %s %s", model, project, maker)

    def apply(self, analog, cells, temps):
        voltage = analog["voltage"]
        current = analog["current"]
        remain = analog["remain_ah"]
        full = analog["full_ah"]
        self._set("/Dc/0/Voltage", round(voltage, 2))
        self._set("/Dc/0/Current", round(current, 2))
        self._set("/Dc/0/Power", round(voltage * current, 1))
        self._set("/Soc", int(analog["soc"]))
        self._set("/Soh", int(analog["soh"]))
        self._set("/Capacity", round(remain, 2))
        self._set("/InstalledCapacity", round(full, 2))
        self._set("/ConsumedAmphours", round(max(0.0, full - remain), 2))
        self._set("/History/ChargeCycles", int(analog["cycles"]))
        millivolts = [value for _, value in analog["cells"]]
        for index, value in analog["cells"]:
            self._set(f"/Voltages/Cell{index}", round(value / 1000.0, 3))
        if millivolts:
            self._set("/System/MinCellVoltage", round(min(millivolts) / 1000.0, 3))
            self._set("/System/MaxCellVoltage", round(max(millivolts) / 1000.0, 3))
        probes = analog["temps"][:4]
        if probes:
            self._set("/Dc/0/Temperature", round(max(probes), 1))
            self._set("/System/MinCellTemperature", round(min(probes), 1))
            self._set("/System/MaxCellTemperature", round(max(probes), 1))
        charge_ok = not blocked(cells, (0x02, 0xF0)) and not blocked(temps, (0x02, 0xF0))
        discharge_ok = not blocked(cells, (0x01, 0xF0)) and not blocked(temps, (0x01, 0xF0))
        self._set("/Io/AllowToCharge", 1 if charge_ok else 0)
        self._set("/Io/AllowToDischarge", 1 if discharge_ok else 0)
        # Currents first. Venus starts using this service as the BMS when
        # MaxChargeVoltage becomes valid, and a discharge current of 0 turns
        # the inverter off.
        self._set("/Info/MaxChargeCurrent", CHARGE_CURRENT if charge_ok else 0)
        self._set("/Info/MaxDischargeCurrent", DISCHARGE_CURRENT if discharge_ok else 0)
        self._set("/Info/BatteryLowVoltage", LOW_VOLTAGE)
        self._set("/Info/MaxChargeVoltage", CHARGE_VOLTAGE)
        self._set("/Alarms/HighCellVoltage", 2 if blocked(cells, (0x02, 0xF0)) else 0)
        self._set("/Alarms/LowCellVoltage", 2 if blocked(cells, (0x01, 0xF0)) else 0)
        self._set("/Alarms/HighTemperature", 2 if blocked(temps, (0x02, 0xF0)) else 0)
        self._set("/Alarms/LowTemperature", 2 if blocked(temps, (0x01, 0xF0)) else 0)
        self._set("/Connected", 1)

    def poll(self):
        self.ticks += 1
        try:
            if self.fd is None:
                self.open()
            analog_frame = self.ask(0x42, b"01")
            alarm_frame = self.ask(0x44, b"01")
            if analog_frame is None:
                raise TimeoutError("no analog reply")
            analog = pylon.decode_analog(analog_frame["info"])
            if analog is None:
                raise ValueError("analog payload did not decode")
            states = pylon.decode_alarm(alarm_frame["info"]) if alarm_frame else None
            cells = states["cells"] if states else []
            temps = states["temps"] if states else []
            if not self.named:
                self.apply_name()
            self.apply(analog, cells, temps)
            self.fails = 0
            if self.ticks % 60 == 1:
                log.info(
                    "%.2f V %+.2f A SOC %s%% cells %s",
                    analog["voltage"],
                    analog["current"],
                    analog["soc"],
                    cell_summary(analog["cells"]),
                )
        except Exception as exc:
            self.fails += 1
            if self.fails == 1 or self.fails % 30 == 0:
                log.warning("poll failed (%s): %s", self.fails, exc)
            self.close()
            if self.fails >= FAILS_BEFORE_DOWN:
                self.mark_down()
        return True


def port_path(argument):
    """serial-starter passes the tty name. A manual start may pass /dev/ttyUSB0."""
    if not argument:
        argument = "ttyUSB0"
    if argument.startswith("/dev/"):
        return argument
    return "/dev/" + argument


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    port = port_path(sys.argv[1] if len(sys.argv) > 1 else "")
    DBusGMainLoop(set_as_default=True)
    battery = Battery(port)
    GLib.timeout_add(POLL_MS, battery.poll)
    GLib.MainLoop().run()


if __name__ == "__main__":
    main()
