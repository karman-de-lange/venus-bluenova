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
    cp "$SRC/install.sh" "$SRC/dbus_bluenova.py" "$SRC/probe_bms.py" /data/bluenova/
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

cat > /data/rc.local << 'EOF'
#!/bin/sh
# After a Venus update the root filesystem no longer has this driver.
if [ ! -x /opt/victronenergy/service-templates/dbus-bluenova/run ] \
    || [ ! -f /etc/udev/rules.d/zz-bluenova.rules ]; then
    /data/bluenova/install.sh
fi
EOF
chmod 755 /data/rc.local

udevadm control --reload-rules
# serial-starter reads its service list once, at start.
svc -t /service/serial-starter || true
sleep 2
if [ -c /dev/ttyUSB0 ]; then
    udevadm trigger --action=add --name-match=ttyUSB0 || true
fi
echo "installed"
