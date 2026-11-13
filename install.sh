#!/bin/sh
# ============================================================================
#  netshift – Installer (Manjaro / Arch Linux)
#
#  Aus dem Git-Ordner:             sudo sh install.sh
#  Als Einzeldatei (Release):      sudo sh netshift-install.sh
#  Ohne Rückfragen:                … --yes
#  Nur prüfen, nichts ändern:      … --check
#  Entfernen:                      … --uninstall
#
#  Benötigt kein makepkg/base-devel. Installiert nur Pakete, die fehlen.
#  Konfiguration und Sicherungen (/etc/netshift, NAS) werden nie angefasst.
# ============================================================================
# Programmdateien: eingebettet (Einzeldatei) oder direkt aus dem Git-Ordner
SELF_DIR=$(cd "$(dirname "$0")" && pwd)
LINE=$(awk '/^__NETSHIFT_PAYLOAD__$/ {print NR + 1; exit}' "$0")
if [ -n "$LINE" ] && [ -n "$(tail -n +"$LINE" "$0" | head -c 1)" ]; then
    TMP=$(mktemp -d) || { echo "FEHLER: Kein temporäres Verzeichnis"; exit 1; }
    trap 'rm -rf "$TMP"' EXIT
    tail -n +"$LINE" "$0" | base64 -d | tar xz -C "$TMP" \
        || { echo "FEHLER: Installer-Datei ist beschädigt – bitte neu herunterladen."; exit 1; }
    S="$TMP/netshift"
else
    S="$SELF_DIR"
fi
[ -f "$S/tools/install-files.sh" ] || { echo "FEHLER: Programmdateien nicht gefunden ($S)."; exit 1; }
VERSION=$(sed -n 's/^VERSION = "\(.*\)"/\1/p' "$S/netshift.py")
ROOT="${NETSHIFT_INSTALL_ROOT:-}"          # nur für Tests
LOG=/tmp/netshift-install.log
YES=0; CHECK=0; UNINSTALL=0; CLASSIC=0
for a in "$@"; do
    case "$a" in
        -y|--yes) YES=1 ;;
        --check) CHECK=1 ;;
        --uninstall) UNINSTALL=1 ;;
        --classic) CLASSIC=1 ;;
        -h|--help) sed -n '2,13p' "$0"; exit 0 ;;
        *) echo "Unbekannte Option: $a"; exit 1 ;;
    esac
done

if [ -t 1 ]; then B="\033[1m"; G="\033[32m"; Y="\033[33m"; R="\033[31m"; N="\033[0m"; else B=""; G=""; Y=""; R=""; N=""; fi
step() { printf "\n${B}==> %s${N}\n" "$1"; }
ok()   { printf "  ${G}✔${N} %s\n" "$1"; }
warn() { printf "  ${Y}!${N} %s\n" "$1"; }
fail() { printf "\n${R}${B}FEHLER:${N} %s\n" "$1"; exit 1; }
ask()  {   # ask "Frage" -> 0 = ja
    [ "$YES" = 1 ] && return 0
    [ -t 0 ] || return 0
    printf "  %s [J/n] " "$1"; read -r a
    case "$a" in n|N|nein|Nein) return 1 ;; *) return 0 ;; esac
}

[ "$(id -u)" -eq 0 ] || fail "Bitte mit sudo starten:  sudo sh $0"

printf "${B}netshift %s – Installation${N}\n" "$VERSION"
: > "$LOG"

# --------------------------------------------------------------------------- Entfernen
if [ "$UNINSTALL" = 1 ]; then
    step "Entferne netshift"
    command -v systemctl >/dev/null && systemctl disable --now netshift-check.timer netshift-boot.timer >/dev/null 2>&1
    if pacman -Qq netshift >/dev/null 2>&1; then
        pacman -R --noconfirm netshift >>"$LOG" 2>&1 && ok "Paket netshift entfernt"
    fi
    rm -rf "$ROOT/usr/lib/netshift" "$ROOT/usr/share/doc/netshift" "$ROOT/usr/share/licenses/netshift"
    rm -f "$ROOT/usr/bin/netshift" "$ROOT/usr/bin/netshift-gui" \
          "$ROOT"/usr/lib/systemd/system/netshift-* \
          "$ROOT/usr/share/libalpm/hooks/00-netshift.hook" \
          "$ROOT/usr/share/polkit-1/actions/org.netshift.gui.policy" \
          "$ROOT/usr/share/applications/netshift.desktop"
    command -v systemctl >/dev/null && systemctl daemon-reload >/dev/null 2>&1
    ok "netshift entfernt (Einstellungen in /etc/netshift und die Sicherungen bleiben erhalten)"
    exit 0
fi

command -v pacman >/dev/null || fail "Dieser Installer ist für Manjaro/Arch Linux (pacman nicht gefunden)."

# --------------------------------------------------------------------------- Prüfen
have()   { command -v "$1" >/dev/null 2>&1; }
py()     { python3 -c "$1" >/dev/null 2>&1; }
gtk_ok() { py "import gi; gi.require_version('Gtk','4.0'); gi.require_version('Adw','1'); from gi.repository import Gtk, Adw"; }
tk_ok()  { py "import tkinter; tkinter.Tcl()"; }

check_core() {
    CORE_MISSING=""
    have python3    || CORE_MISSING="$CORE_MISSING python"
    have rsync      || CORE_MISSING="$CORE_MISSING rsync"
    have mkfs.ext4  || CORE_MISSING="$CORE_MISSING e2fsprogs"
    have findmnt    || CORE_MISSING="$CORE_MISSING util-linux"
    have mount.cifs || CORE_MISSING="$CORE_MISSING cifs-utils"
    have pkexec     || CORE_MISSING="$CORE_MISSING polkit"
}

step "Prüfe, was bereits vorhanden ist"
check_core
for p in python3:python rsync:rsync mkfs.ext4:e2fsprogs findmnt:util-linux mount.cifs:cifs-utils pkexec:polkit; do
    c=${p%%:*}; n=${p##*:}
    if have "$c"; then ok "$n"; else warn "$n fehlt"; fi
done
GUI=none
if [ "$CLASSIC" = 0 ] && gtk_ok; then GUI=gtk; ok "Moderne Oberfläche (GTK4/libadwaita)"
elif tk_ok; then GUI=tk; ok "Klassische Oberfläche (Tk)"
fi
[ "$GUI" = none ] && warn "Keine Oberfläche nutzbar – wird nachinstalliert"

if [ "$CHECK" = 1 ]; then
    printf "\nNur Prüfung (--check) – nichts verändert.\n"; exit 0
fi

# --------------------------------------------------------------------------- Pakete
DB_FIXED=0
repair_db() {
    [ "$DB_FIXED" = 1 ] && return 0
    printf "\n  ${Y}Die Pakete konnten nicht geladen werden.${N} Meist ist die Paketliste dieses Systems\n"
    printf "  veraltet (typisch bei einem selten benutzten USB-Stick). Abhilfe: Spiegelserver und\n"
    printf "  Schlüssel auffrischen und das System aktualisieren (pacman -Syu).\n"
    ask "Jetzt aktualisieren? Das kann je nach Alter des Systems einige Minuten dauern." || return 1
    if have pacman-mirrors; then
        echo "  Suche schnelle Spiegelserver…"
        pacman-mirrors --fasttrack 5 >>"$LOG" 2>&1 || warn "Spiegelserver-Suche fehlgeschlagen (weiter)"
    fi
    echo "  Aktualisiere Schlüssel…"
    keyrings="archlinux-keyring"
    pacman -Si manjaro-keyring >/dev/null 2>&1 && keyrings="$keyrings manjaro-keyring"
    pacman -Sy --needed --noconfirm $keyrings >>"$LOG" 2>&1 || {
        warn "Schlüssel-Aktualisierung fehlgeschlagen – setze Schlüsselbund neu auf"
        pacman-key --init >>"$LOG" 2>&1
        pacman-key --populate >>"$LOG" 2>&1
        pacman -Sy --needed --noconfirm $keyrings >>"$LOG" 2>&1
    }
    echo "  Aktualisiere das System…"
    if [ "$YES" = 1 ] || ! [ -t 0 ]; then
        pacman -Syu --noconfirm 2>&1 | tee -a "$LOG" | grep -E '^(::|error|Fehler|warning)'
    else
        pacman -Syu 2>&1 | tee -a "$LOG"
    fi
    DB_FIXED=1
}

install_pkgs() {   # install_pkgs "pakete" -> 0 bei Erfolg
    [ -z "$1" ] && return 0
    echo "  Installiere: $(echo $1)"
    pacman -S --needed --noconfirm $1 >>"$LOG" 2>&1 && return 0
    repair_db || return 1
    pacman -S --needed --noconfirm $1 >>"$LOG" 2>&1
}

if [ -n "$CORE_MISSING" ]; then
    step "Installiere fehlende Pakete"
    install_pkgs "$CORE_MISSING" || {
        tail -n 15 "$LOG"
        fail "Die benötigten Pakete ($CORE_MISSING ) konnten nicht installiert werden.
       Bitte die Internetverbindung prüfen. Das vollständige Protokoll liegt in $LOG –
       schick mir gern die letzten Zeilen daraus."
    }
    check_core
    [ -n "$CORE_MISSING" ] && fail "Weiterhin fehlend:$CORE_MISSING (siehe $LOG)"
    ok "Pakete installiert"
fi

if [ "$GUI" = none ] || { [ "$GUI" = tk ] && [ "$CLASSIC" = 0 ]; }; then
    step "Grafische Oberfläche"
    if [ "$CLASSIC" = 0 ] && install_pkgs "python-gobject gtk4 libadwaita" && gtk_ok; then
        GUI=gtk; ok "Moderne Oberfläche installiert"
    elif [ "$GUI" = tk ]; then
        warn "Moderne Oberfläche nicht verfügbar – die klassische Oberfläche wird verwendet"
    elif install_pkgs "tk" && tk_ok; then
        GUI=tk; ok "Klassische Oberfläche installiert"
    else
        warn "Keine Oberfläche installierbar – netshift ist im Terminal nutzbar (sudo netshift)"
    fi
fi

# --------------------------------------------------------------------------- Dateien
step "Installiere netshift $VERSION"

if pacman -Qq netshift >/dev/null 2>&1; then
    pacman -Rdd --noconfirm netshift >>"$LOG" 2>&1 && ok "Bisheriges netshift-Paket ersetzt (Einstellungen bleiben)"
fi

DESTDIR="$ROOT" sh "$S/tools/install-files.sh" >>"$LOG" 2>&1 || fail "Kopieren der Dateien fehlgeschlagen (siehe $LOG)"
install -dm700 "$ROOT/etc/netshift"
if command -v systemctl >/dev/null 2>&1 && [ -z "$ROOT" ]; then
    systemctl daemon-reload >/dev/null 2>&1
    # Zeitplan gemäß vorhandener Konfiguration wieder aktivieren (bei Aktualisierung)
    [ -f /etc/netshift/config.json ] && /usr/bin/netshift schedule-apply >/dev/null 2>&1
fi
ok "Dateien installiert"

# --------------------------------------------------------------------------- Fertig
INSTALLED=$(python3 "$ROOT/usr/lib/netshift/netshift.py" --version 2>/dev/null)
[ -n "$INSTALLED" ] || fail "netshift lässt sich nicht starten (siehe $LOG)"
printf "\n${G}${B}Fertig:${N} %s ist installiert.\n" "$INSTALLED"
case "$GUI" in
    gtk|tk) printf "  Starten: im Startmenü ${B}netshift${N} öffnen – oder im Terminal: netshift-gui\n" ;;
    *)      printf "  Starten im Terminal: ${B}sudo netshift setup${N}\n" ;;
esac
if [ -f /etc/netshift/config.json ] && grep -q '"rescue_mode": true' /etc/netshift/config.json 2>/dev/null; then
    printf "  Dieses System ist als ${B}Rettungssystem${N} eingerichtet (nur Wiederherstellung).\n"
elif [ ! -f /etc/netshift/config.json ]; then
    printf "  Für einen Rettungs-Stick: in den Einstellungen „Rettungssystem – nur wiederherstellen“ einschalten.\n"
fi
exit 0
__NETSHIFT_PAYLOAD__
