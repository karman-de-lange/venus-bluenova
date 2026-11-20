# BlueNova RacPower RS485, for a Victron GX driver

This is the command set to code against. It was read from this rack on 2026-11-13: Topband TB-48100-CAN, project BN07, 16 cells, 7 temperature sensors. The family specification is the Pylontech low-voltage RS485 protocol, source V3.5 (2019-08-07). A Spanish rendering of that document is at <https://cuencasolar.es/wp-content/uploads/2026/11/Pylontech_RS485_Baja_Tension_Protocolo_ES.pdf>. This BMS implements a small subset of it. The scan below is what the GX program should use.

The stock `dbus-serialbattery` Pace driver is the wrong dialect. It sends protocol version `0x25`. This rack answers `0x21` and returns a version error to `0x25`.

## Link

| Item | Value |
|---|---|
| Port | RS485 RJ45, not the CAN RJ45 |
| Pins (RacPower DU manual) | 1 = A, 2 = B, 3 and 6 = GND |
| Baud | 9600 8N1 |
| Role | Slave. It sends nothing until polled |
| Address | 1 |
| Protocol version | `0x21` |
| Device type CID1 | `0x46` |
| Adapter | FTDI USB-RS485. This one receives without toggling RTS |
| Masters | One. Unplug this PC before the GX polls the same pair |

A frame is about 150 bytes. At 9600 that is roughly 160 ms. Read until the carriage return, with an overall timeout of about 1 second. A short quiet-gap timeout splits the frame and the next request collides on the half-duplex pair. Some adapters echo the request; drop a leading copy of the bytes just sent.

## Frame

Bytes on the wire, except the start and end markers, are ASCII hex. One data byte is two characters.

```
~  VER ADR CID1 CID2 LENGTH INFO CHKSUM CR
```

| Field | Size | Meaning |
|---|---|---|
| `~` | 1 raw byte `0x7E` | Start |
| VER | 2 ASCII chars | Protocol version. Send `21` |
| ADR | 2 ASCII chars | Address. Send `01` |
| CID1 | 2 ASCII chars | `46` |
| CID2 | 2 ASCII chars | Command on the way out. Return code on the way back |
| LENGTH | 4 ASCII chars | High nibble is LCHKSUM. Low 12 bits are LENID, the ASCII length of INFO |
| INFO | LENID ASCII chars | Command data, or the reply payload |
| CHKSUM | 4 ASCII chars | `(-sum of every byte after ~ and before CHKSUM) & 0xFFFF` |
| CR | 1 raw byte `0x0D` | End |

LENID is the number of ASCII characters in INFO, not the number of decoded bytes. For INFO `01`, LENID is 2 and LENGTH is `E002`. For an empty INFO, LENGTH is `0000`.

```python
def length_field(info: bytes) -> int:
    n = len(info)
    total = (n & 0xF) + ((n >> 4) & 0xF) + ((n >> 8) & 0xF)
    check = (0x10 - (total % 16)) & 0xF
    return (check << 12) | n

def frame(cid2: int, info: bytes = b"", version: int = 0x21, address: int = 1) -> bytes:
    body = f"{version:02X}{address:02X}46{cid2:02X}{length_field(info):04X}".encode() + info
    checksum = (-sum(body)) & 0xFFFF
    return b"~" + body + f"{checksum:04X}".encode() + b"\r"
```

Worked request, checked on this rack:

```
~21014642E00201FD34
```

That is version `0x21`, address 1, CID2 `0x42`, INFO `01`. The same analog read with version `0x25` (`~25014642E00201FD30` and the Pace driver's `~25014642E002FFFD05`) comes back as:

```
~210146010000FDB1
```

Version in the reply is `0x21`. Return code `0x01` means version error. There is no payload.

### Return codes

| RTN | Meaning |
|---|---|
| `0x00` | OK |
| `0x01` | Version error |
| `0x02` | Checksum error |
| `0x03` | Length-checksum error |
| `0x04` | Command not supported |
| `0x05` | Format error |
| `0x06` | Invalid data |
| `0x90` | Address error |
| `0x91` | Internal communication error |

Most INFO payloads are ASCII hex and decode to bytes with `bytes.fromhex`. CID2 `0x51` is the exception: its INFO is raw ASCII text.

## Commands on this rack

Scanned read-only at version `0x21`, address 1, 9600 8N1. The V3.5 document also lists writes and shutdowns. Those were not sent.

| CID2 | V3.5 name | INFO sent | This rack |
|---|---|---|---|
| `0x42` | Analog values | `01` | OK. 64 decoded bytes. Poll this |
| `0x44` | Alarms | `01` | OK. 37 decoded bytes. Poll this |
| `0x47` | System parameters | empty | OK. 24 decoded bytes. Read once |
| `0x4F` | Protocol version | empty | OK, empty INFO. The version is the VER field, `0x21` |
| `0x51` | Manufacturer | empty or `01` | OK. 64 ASCII characters, not hex |
| `0x60` | System basic info | empty and `01` | `0x04` not supported |
| `0x61` | System analog | `01` | `0x04` not supported |
| `0x62` | System alarm | `01` | `0x04` not supported |
| `0x63` | System charge management | `01` | `0x04` not supported |
| `0x92` | Charge/discharge management | empty and `01` | `0x04` not supported |
| `0x93` | Serial number | empty and `01` | `0x04` not supported |
| `0x96` | Firmware version | empty and `01` | `0x04` not supported |

Earlier reads of the Pace v25 service commands (`0xC1`, `0xC2`, `0xDD`, `0xDC`, and the other v25 config commands) also returned `0x04` on this port. CID2 `0x49`, the parameter write, returned `0x04` and a following `0x47` read showed the block unchanged.

There is no charge-limit command on this jack. `0x92` and `0x63` are how a Pylon pack tells an inverter the charge voltage and the allowed current. This firmware does not implement them. The GX values for voltage and current limits have to be chosen in the driver. See the limits section.

Do not send `0x49` (set parameters), `0x94` (set charge management), `0x95` (shut down the pack), or `0x64` (shut down the system).

## CID2 `0x42` analog

Request: `frame(0x42, b"01")`.

Reply INFO, after hex decode. The first byte echoes the pack address. Big-endian.

| Offset | Size | Field | Scale |
|---|---|---|---|
| 0 | 1 | Address echo | `0x01` |
| 1 | 1 | Cell count | `0x10` = 16 |
| 2 | 32 | Cell millivolts | uint16, mV |
| 34 | 1 | Temperature count | `0x07` |
| 35 | 14 | Temperatures | uint16. °C = `(raw - 2730) / 10` |
| 49 | 2 | Current | int16, 0.01 A. Positive = charge, negative = discharge |
| 51 | 2 | Pack voltage | uint16. If raw < 10000, unit is 10 mV. This pack uses that |
| 53 | 2 | Remaining capacity | uint16, 0.01 Ah |
| 55 | 1 | User-count byte | `0x04` on this pack. Skip it. It is not a length |
| 56 | 2 | Full capacity | uint16, 0.01 Ah. `0x2710` = 100.00 Ah |
| 58 | 2 | Cycles | uint16. `0x00FC` = 252 |
| 60 | 1 | SOC | percent |
| 61 | 1 | SOH | percent |
| 62 | 2 | Unused | `00 00` |

Captured reply, return code `0x00`:

```
~21014600808001100CF40CEF0CF00CF60CF10CF00CF60CF20CED0CF70CF60CEC0CF20CF50CF00CF1070B9C0B9B0B9E0B9C0BCF0BCF0BB1FEA514B5251A04271000FC5F640000E081
```

That decodes to 53.01 V, −3.47 A, SOC 95%, 94.98 / 100.00 Ah, SOH 100%, 252 cycles, cells 3308–3319 mV, temperatures 24.2, 24.1, 24.4, 24.2, 29.3, 29.3, 26.3 °C.

Sensors 1–4 are the cell probes. Sensor 4 is the one that false-tripped at 60 °C when its lead was strained. Sensors 5–7 are on the board. Which of those three is the MOSFET is not identified. Publish the hottest of sensors 1–4 as the battery temperature.

Victron's battery current uses the same sign: positive charges, negative discharges.

## CID2 `0x44` alarm

Request: `frame(0x44, b"01")`.

| Offset | Size | Field |
|---|---|---|
| 0 | 1 | Address echo `0x01` |
| 1 | 1 | Cell count `0x10` |
| 2 | 16 | Cell state, one byte each |
| 18 | 1 | Temperature count `0x07` |
| 19 | 7 | Temperature state, one byte each |
| 26 | 11 | Tail. See below |

State byte:

| Value | Meaning |
|---|---|
| `0x00` | Normal |
| `0x01` | Below the lower limit |
| `0x02` | Above the upper limit |
| `0xF0` | Other fault |

A healthy capture:

```
01 10  (16 bytes of 00)  07  (7 bytes of 00)  00 00 08 00 06 40 00 00 00 00 00
```

Use the 16 cell bytes and the 7 temperature bytes. Ignore the 11-byte tail for charge control. `0x08` is present when the pack is healthy. A later byte has gone from `0x06` to `0x86` when the coulomb counter hit 0% while the cells were still about 3.24 V. That bit means the counter is empty. It must not set the charge current to 0. Charging is what refills that counter.

Charge block, set max charge current to 0:

- any cell state `0x02` or `0xF0`
- any temperature state `0x02` or `0xF0`

Discharge block, set max discharge current to 0:

- any cell state `0x01` or `0xF0`
- any temperature state `0x01` or `0xF0`

## CID2 `0x47` parameters

Request: `frame(0x47)`. Empty INFO. No address echo in the payload. Twelve big-endian uint16 words, 24 bytes.

| Word | This rack | Meaning |
|---|---|---|
| 0 | `0x0E74` = 3700 | Cell high, mV. 3.700 V |
| 1 | `0x0A5A` = 2650 | Cell low, mV. 2.650 V |
| 2 | `0x09C4` = 2500 | Cell under-voltage, mV. 2.500 V |
| 3 | `0x0D03` = 3331 | Charge high temperature. °C = `(raw - 2731) / 10` = 60.0 |
| 4 | `0x0A79` = 2681 | Charge low temperature. −5.0 °C |
| 5 | `0x2EE0` = 12000 | Charge current limit, 0.01 A. 120.00 A |
| 6 | `0x1720` = 5920 | Pack high, 10 mV. 59.20 V |
| 7 | `0x10E0` = 4320 | Pack low, 10 mV. 43.20 V |
| 8 | `0x0FA0` = 4000 | Pack under-voltage, 10 mV. 40.00 V |
| 9 | `0x0D35` = 3381 | Discharge high temperature. 65.0 °C |
| 10 | `0x09E3` = 2531 | Discharge low temperature. −20.0 °C |
| 11 | `0x2EE0` = 12000 | Discharge current limit. 120.00 A |

The temperature offset in this block is 2731. The live temperatures in `0x42` use 2730. Both match the readings.

These words are protection thresholds, not the charge target. Publishing 59.20 V or 120 A to the GX would aim the charger at the trip point.

## CID2 `0x51` manufacturer

Request: `frame(0x51)` or `frame(0x51, b"01")`. Both return the same text. INFO is 64 ASCII characters, and the checksum covers those characters as sent. Do not hex-decode them.

```
~21014600C040TB-48100-CAN        BN07Topband                                 F1FD
```

| Chars | Text |
|---|---|
| 0–19 | `TB-48100-CAN` padded with spaces |
| 20–23 | `BN07` |
| 24–63 | `Topband` padded with spaces |

Use that string as the serial / unique id: `TB-48100-CAN-BN07`.

## Values to publish on the GX

Service name `com.victronenergy.battery.<port>`. Product id reserved for dbus-serialbattery is `0xBA77`. The paths that make it a controlling BMS:

| Path | Source |
|---|---|
| `/Dc/0/Voltage` | `0x42` pack voltage |
| `/Dc/0/Current` | `0x42` current, same sign |
| `/Dc/0/Power` | voltage × current |
| `/Dc/0/Temperature` | hottest of sensors 1–4 |
| `/Soc` | `0x42` SOC byte |
| `/Soh` | `0x42` SOH byte |
| `/Capacity` | remaining Ah |
| `/InstalledCapacity` | full Ah, 100 |
| `/ConsumedAh` | full − remaining |
| `/Cycles` | cycle count |
| `/Info/MaxChargeVoltage` | 55.0 |
| `/Info/MaxChargeCurrent` | 50, or 0 while a charge alarm is set |
| `/Info/MaxDischargeCurrent` | 100, or 0 while a discharge alarm is set |
| `/Info/BatteryLowVoltage` | 46.5 |
| `/Voltages/Cell1` … `/Voltages/Cell16` | cell millivolts ÷ 1000 |
| `/ProductName` | `BlueNova RacPower` |
| `/HardwareVersion` | `TB-48100-CAN BN07` |
| `/Manufacturer` | `Topband` |

The four limit numbers come from the RacPower manual's voltage-based table, because this port has no charge-management reply:

- Charge voltage 55.0 V (3.44 V × 16). Float in that table is 54.0 V. DVCC has one max charge voltage.
- Maximum continuous charge current 50 A. The manual's recommended charge current is 25 A.
- Maximum continuous discharge current 100 A.
- Inverter switch-off 46.5 V (2.9 V × 16).

`0x47` stays in the log as the protection ceiling (59.20 V, 120 A, 40.00 V). It is not what the charger should target.

After the service is up: Settings → System setup → Battery monitor, and Settings → System setup → Charge control → Controlling BMS. DVCC then follows `/Info/MaxChargeVoltage`, `/Info/MaxChargeCurrent`, and `/Info/MaxDischargeCurrent`.

## One-shot test on the GX

1. Stop the poller on this PC. Move the FTDI adapter to a GX USB port and leave it in the battery RS485 socket.
2. Venus already has the FTDI driver. The port appears as `/dev/ttyUSB0` or under `/dev/serial/by-id/`.
3. Run one process that opens 9600 8N1, sends `0x42` and `0x44` once a second, and prints voltage, current, SOC, the 16 cells, the 7 temperatures, and any non-zero state bytes.
4. A good first print looks like the capture above: about 53 V, a small current, SOC near the VRM figure, cell spread under about 20 mV, sensor 4 within a couple of degrees of sensors 1–3, and no alarm bytes.
5. Only after that print is right, publish the dbus paths and select the service as the controlling BMS.

The driver class list and the pull-request checklist for `dbus-serialbattery` are at <https://mr-manuel.github.io/venus-os_dbus-serialbattery_docs/general/supported-bms/#add-by-opening-a-pull-request>. A new class is required. Patching the Pace class means replacing the hardcoded `25` and its byte offsets; the frames in this file are the ones that answer.

A Venus firmware update can remove an added driver. Keep the install script in `/data` so it can be put back.

## Out of scope for this port

CAN on this pack is a separate transceiver and it has already failed. No RS485 command fixes it. Protocol `0x21` cannot change parameters, cannot close the charge MOSFET, and cannot take a firmware file. The RS232 jack on the front panel is marked "Not active" in the RacPower manual.
