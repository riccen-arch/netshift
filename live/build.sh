#!/bin/bash
# Baut das netshift-Live-System (ISO) mit archiso – auf Arch Linux als root:
#   sudo pacman -S --needed archiso && sudo bash live/build.sh [ausgabeordner]
# Auf GitHub erledigt das .github/workflows/live-iso.yml automatisch.
set -euo pipefail
HERE=$(cd "$(dirname "$0")" && pwd)
ROOT=$(dirname "$HERE")
OUT=$(realpath -m "${1:-$ROOT/dist}")
WORK=${WORK:-/tmp/netshift-archiso}
P="$WORK/profile"
A="$P/airootfs"
VERSION=$(sed -n 's/^VERSION = "\(.*\)"/\1/p' "$ROOT/netshift.py")

rm -rf "$WORK"
mkdir -p "$P" "$OUT"
cp -a /usr/share/archiso/configs/releng/. "$P/"

# Pakete: Standardmedium + netshift-Bedarf; NetworkManager statt iwd
{ grep -v '^\s*#' "$P/packages.x86_64"; grep -v '^\s*#' "$HERE/packages.x86_64"; } \
    | sed '/^\s*$/d; /^iwd$/d' | sort -u > "$P/packages.new"
mv "$P/packages.new" "$P/packages.x86_64"

# Netzwerk: NetworkManager übernimmt (systemd-networkd/iwd des Standardmediums abschalten)
rm -f "$A/etc/systemd/system/multi-user.target.wants/iwd.service" \
      "$A/etc/systemd/system/multi-user.target.wants/systemd-networkd.service" \
      "$A/etc/systemd/system/sockets.target.wants/systemd-networkd.socket" \
      "$A/etc/systemd/system/network-online.target.wants/systemd-networkd-wait-online.service"
rm -rf "$A/etc/systemd/network" "$A/etc/systemd/system/systemd-networkd-wait-online.service.d"
mkdir -p "$A/etc/systemd/system/multi-user.target.wants"
ln -sf /usr/lib/systemd/system/NetworkManager.service "$A/etc/systemd/system/multi-user.target.wants/NetworkManager.service"

# Eigene Dateien und netshift selbst
cp -a "$HERE/airootfs/." "$A/"
DESTDIR="$A" sh "$ROOT/tools/install-files.sh"
rm -f "$A/usr/share/libalpm/hooks/00-netshift.hook"     # im Live-System keine Update-Snapshots

# Ausführbare Dateien: mkarchiso übernimmt keine Dateirechte → in profiledef eintragen
perms=$(cd "$A" && find . -type f -perm -u+x | sed 's|^\.||' | sort)
for f in $perms; do
    grep -q "\"$f\"" "$P/profiledef.sh" || sed -i "/^file_permissions=(/a \\  [\"$f\"]=\"0:0:755\"" "$P/profiledef.sh"
done

# Name, Bezeichnung, Bootmenü
sed -i -e 's|^iso_name=.*|iso_name="netshift-live"|' \
       -e 's|^iso_label=.*|iso_label="NETSHIFT_$(date --date="@${SOURCE_DATE_EPOCH:-$(date +%s)}" +%Y%m)"|' \
       -e 's|^iso_publisher=.*|iso_publisher="netshift <https://github.com/riccen-arch/netshift>"|' \
       -e 's|^iso_application=.*|iso_application="netshift Live"|' \
       -e "s|^iso_version=.*|iso_version=\"$VERSION\"|" "$P/profiledef.sh"
grep -rlZ "Arch Linux install medium" "$P/syslinux" "$P/grub" "$P/efiboot" 2>/dev/null \
    | xargs -0 -r sed -i 's/Arch Linux install medium/netshift Live/g'

mkarchiso -v -w "$WORK/work" -o "$OUT" "$P"
cd "$OUT"
iso=$(ls -t netshift-live-*.iso | head -1)
sha256sum "$iso" > "$iso.sha256"
echo "Fertig: $OUT/$iso"
