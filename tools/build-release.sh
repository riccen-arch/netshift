#!/bin/sh
# Baut die Download-Dateien einer Version nach dist/:
#   netshift-install.sh      Installer in einer Datei (Programm eingebettet)
#   netshift-<version>.tar.gz
set -e
cd "$(dirname "$0")/.."
V=$(sed -n 's/^VERSION = "\(.*\)"/\1/p' netshift.py)
mkdir -p dist
TMP=$(mktemp -d); trap 'rm -rf "$TMP"' EXIT
mkdir "$TMP/netshift"
tar cf - --exclude=.git --exclude=dist --exclude=__pycache__ . | tar xf - -C "$TMP/netshift"
cp install.sh dist/netshift-install.sh
tar czf - -C "$TMP" netshift | base64 >> dist/netshift-install.sh
chmod 755 dist/netshift-install.sh
tar czf "dist/netshift-$V.tar.gz" -C "$TMP" netshift
echo "dist/netshift-install.sh und dist/netshift-$V.tar.gz (Version $V) erstellt"
