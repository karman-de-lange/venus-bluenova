# BlueNova RacPower on Venus OS

Read-only RS485 driver for a BlueNova RacPower rack (Pylon-family protocol 0x21). It publishes `com.victronenergy.battery.<tty>` so the GX can show the pack and, with DVCC on, use it as the controlling BMS.

The charge limits are the RacPower manual values, 55.0 V and 50 A, because this jack does not report a charge target. A cell or temperature alarm sets the matching current to 0. The driver never sends a write command.

The USB adapter is FTDI serial `AG0KFRQ1`. `serial-starter` starts the driver on whatever `ttyUSB` name that adapter receives.

## Install on a GX

Copy this directory to the GX and run, as root:

```sh
sh install.sh
```

One master only. Unplug the adapter from any other computer or GX before this one polls the battery.

In the menu, the pack shows up as BlueNova RacPower. Settings → System setup → DVCC, and choose this battery as the controlling BMS. The Pi used for development has no MultiPlus, so DVCC there has nothing to command.

A Venus firmware update removes the copy under `/opt` and `/etc`. `/data/rc.local` runs `install.sh` again when those files are missing.

A different RS485 dongle has a different serial number. Change `AG0KFRQ1` in `udev/zz-bluenova.rules` and run `install.sh` again.
