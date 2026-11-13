#!/bin/sh
# Kopiert die netshift-Dateien an ihren Platz – ohne Paketprüfung.
# Wird von install.sh und vom PKGBUILD (DESTDIR=…) verwendet.
# Für die normale Installation bitte install.sh im Hauptordner nutzen.
set -e
D="${DESTDIR:-}"
cd "$(dirname "$0")/.."

install -Dm755 netshift.py          "$D/usr/lib/netshift/netshift.py"
install -Dm755 netshift-gui.py      "$D/usr/lib/netshift/netshift-gui.py"
install -Dm755 netshift-gtk.py      "$D/usr/lib/netshift/netshift-gtk.py"
install -dm755 "$D/usr/bin"
ln -sf /usr/lib/netshift/netshift.py "$D/usr/bin/netshift"
install -Dm755 data/netshift-gui    "$D/usr/bin/netshift-gui"
for u in netshift-check.service netshift-check.timer netshift-boot.service netshift-boot.timer netshift-shutdown.service; do
    install -Dm644 "data/$u" "$D/usr/lib/systemd/system/$u"
done
install -Dm644 data/00-netshift.hook         "$D/usr/share/libalpm/hooks/00-netshift.hook"
install -Dm644 data/org.netshift.gui.policy  "$D/usr/share/polkit-1/actions/org.netshift.gui.policy"
install -Dm644 data/netshift.desktop         "$D/usr/share/applications/netshift.desktop"
install -Dm644 README.md                     "$D/usr/share/doc/netshift/README.md"
[ -f LICENSE ] && install -Dm644 LICENSE     "$D/usr/share/licenses/netshift/LICENSE"
exit 0
