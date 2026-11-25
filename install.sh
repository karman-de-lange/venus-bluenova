#!/bin/sh
# Install the BlueNova driver the way Venus starts its own serial drivers.
# Run as root on the GX. Safe to run again, including after a firmware update.
set -e
SRC=$(CDPATH= cd -- "$(dirname "$0")" && pwd)

mkdir -p /data/bluenova/service-template/log \
    /data/bluenova/udev \
    /data/bluenova/serial-starter.d \
    /data/conf/serial-starter.d \
    /etc/udev/rules.d \
    /opt/victronenergy/service-templates/dbus-bluenova/log

if [ "$SRC" != "/data/bluenova" ]; then
    cp "$SRC/install.sh" "$SRC/dbus_bluenova.py" "$SRC/probe_bms.py" "$SRC/pylon.py" /data/bluenova/
    cp "$SRC/service-template/run" /data/bluenova/service-template/run
    cp "$SRC/service-template/log/run" /data/bluenova/service-template/log/run
    cp "$SRC/udev/zz-bluenova.rules" /data/bluenova/udev/zz-bluenova.rules
    cp "$SRC/serial-starter.d/bluenova.conf" /data/bluenova/serial-starter.d/bluenova.conf
fi
chmod 755 /data/bluenova/install.sh /data/bluenova/dbus_bluenova.py \
    /data/bluenova/service-template/run /data/bluenova/service-template/log/run

cp /data/bluenova/serial-starter.d/bluenova.conf /data/conf/serial-starter.d/bluenova.conf
rm -f /etc/udev/rules.d/99-bluenova.rules
cp /data/bluenova/udev/zz-bluenova.rules /etc/udev/rules.d/zz-bluenova.rules
cp /data/bluenova/service-template/run /opt/victronenergy/service-templates/dbus-bluenova/run
cp /data/bluenova/service-template/log/run /opt/victronenergy/service-templates/dbus-bluenova/log/run
chmod 755 /opt/victronenergy/service-templates/dbus-bluenova/run \
    /opt/victronenergy/service-templates/dbus-bluenova/log/run

# Drop the earlier service that always opened ttyUSB0.
if [ -L /service/dbus-bluenova ]; then
    svc -d /service/dbus-bluenova || true
    rm -f /service/dbus-bluenova
    sleep 1
fi

# Keep whatever else is already in rc.local. Add the restore step once.
if [ ! -f /data/rc.local ]; then
    printf '%s\n' '#!/bin/sh' > /data/rc.local
fi
if ! grep -q '/data/bluenova/install.sh' /data/rc.local; then
    cat >> /data/rc.local << 'EOF'

# After a Venus update the root filesystem no longer has this driver.
if [ ! -x /opt/victronenergy/service-templates/dbus-bluenova/run ] \
    || [ ! -f /etc/udev/rules.d/zz-bluenova.rules ]; then
    /data/bluenova/install.sh
fi
EOF
fi
chmod 755 /data/rc.local

# Reload the rule table only. Do not restart serial-starter and do not
# synthesize an add event: both make it claim a port again. The other
# ports on a GX are the built-in VE.Direct MPPT and the USB TTL MPPT.
udevadm control --reload-rules

# This adapter only. Stop the generic probe serial-starter already started
# on it, then supervise the BlueNova driver ourselves. The running
# serial-starter keeps the locks on the other ttys and is left alone.
serial=$(sed -n 's/.*ID_SERIAL_SHORT}=="\([^"]*\)".*/\1/p' /data/bluenova/udev/zz-bluenova.rules | head -n 1)
if [ -z "$serial" ]; then
    echo "zz-bluenova.rules does not name an adapter serial" >&2
    exit 1
fi
id=$(echo /dev/serial/by-id/*"$serial"*)
if [ -e "$id" ]; then
    adapter=$(realpath "$id")
    tty=$(basename "$adapter")
    /opt/victronenergy/serial-starter/stop-tty.sh "$tty"
    rm -f "/dev/serial-starter/$tty"

    name="dbus-bluenova.$tty"
    svc -d "/service/$name" 2>/dev/null || true
    rm -f "/service/$name"
    rm -rf "/var/volatile/services/$name"
    mkdir -p /var/volatile/services
    cp -a /opt/victronenergy/service-templates/dbus-bluenova \
        "/var/volatile/services/$name"
    sed -i "s:TTY:$tty:g" \
        "/var/volatile/services/$name/run" \
        "/var/volatile/services/$name/log/run"
    ln -s "/var/volatile/services/$name" "/service/$name"
fi
echo "installed"
