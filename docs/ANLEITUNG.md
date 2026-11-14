# netshift – Anleitung

[← Zurück zur Übersicht](../README.de.md) · [🇬🇧 English](GUIDE.md)

## Was netshift kann

| Funktion | Timeshift (rsync) | netshift |
|---|---|---|
| Inkrementelle Snapshots mit Hardlinks | ✔ | ✔ |
| Ziel: lokale Partition | ✔ | ✔ (auch USB, `UUID=…`, beliebiger Ordner) |
| Ziel: SMB/CIFS, NFS, SSH | ✘ | ✔ |
| Besitzer/Rechte/ACLs/xattrs auch auf SMB erhalten | – | ✔ über ext4-Image-Container |
| Verschlüsselte Sicherung (LUKS2) | ✘ | ✔ (optional) |
| Zeitplan: Boot/stündlich/täglich/wöchentlich/monatlich | ✔ | ✔ (verpasste Termine werden nachgeholt) |
| Snapshot vor pacman-/pamac-Update | nur mit Zusatzpaket | ✔ eingebaut |
| Wiederherstellung des laufenden Systems + Neustart | ✔ | ✔ |
| Wiederherstellung vom Live-USB / auf neue Platte | ✔ | ✔ (Btrfs-`@` wird erkannt, GRUB optional) |
| Vorschau „Was würde sich ändern?“ | ✘ | ✔ |
| Einzelne Dateien/Ordner zurückholen | nur per Dateimanager | ✔ (auch an anderen Ort, auch bei SSH) |
| Abgebrochene Sicherung fortsetzen | ✘ | ✔ |
| Sicherheits-Snapshot vor Restore | ✘ | ✔ |
| Netzwerk-/Temp-Mounts automatisch ausschließen | ✘ | ✔ |

## Installation

**Mit Git** (empfohlen – Aktualisieren geht dann mit einem Befehl):
```bash
sudo pacman -S --needed git
git clone https://github.com/riccen-arch/netshift.git
cd netshift && sudo sh install.sh
```
Aktualisieren:
```bash
cd netshift && git pull && sudo sh install.sh
```

**Ohne Git** (lädt den aktuellen Stand und installiert ihn):
```bash
curl -L https://github.com/riccen-arch/netshift/archive/refs/heads/main.tar.gz | tar xz
cd netshift-main && sudo sh install.sh
```

Der Installer prüft, was fehlt, installiert nur diese Pakete (kein makepkg/base-devel nötig) und
repariert dabei eine veraltete Paketliste (Spiegelserver, Schlüssel, `pacman -Syu`) – typisch bei
einem selten benutzten USB-Stick. Lässt sich die moderne Oberfläche nicht installieren, wird die
klassische verwendet. Einstellungen und Sicherungen bleiben bei Aktualisierungen erhalten.

- Nur prüfen: `sudo sh install.sh --check`
- Entfernen: `sudo sh install.sh --uninstall`
- Protokoll bei Problemen: `/tmp/netshift-install.log`
- Als pacman-Paket aus dem Git-Ordner: `makepkg -si`

### Für Entwickler
- `tools/build-release.sh` erzeugt in `dist/` den Einzeldatei-Installer und das Archiv einer Version.
- `packaging/aur/PKGBUILD` ist die Vorlage für das AUR.

## Oberfläche

Moderne GTK4/libadwaita-Oberfläche: Snapshots nach Datum gruppiert, Speicherplatz-Übersicht,
Fortschritt mit Abbrechen-Knopf, Vorschau der Änderungen mit Filter, Dunkelmodus (übernimmt die
Einstellung von GNOME und KDE). Tastenkürzel: Strg+N neuer Snapshot, F5 aktualisieren,
Strg+, Einstellungen. Fehlen GTK4/libadwaita, startet automatisch die klassische Tk-Oberfläche.

## Einrichtung (Beispiel NAS per SMB)

In der Oberfläche unter **Einstellungen → Sicherungsziel**:
- Art: *SMB / NAS*
- Server: `192.168.1.10`, Freigabe: `backup`, Benutzer + Passwort
- „Verbindung testen“ → *Testen* (Einstellungen werden beim Schließen automatisch gespeichert)

netshift legt auf der Freigabe `netshift/<Rechnername>.img` an – eine Datei, die nur so viel Platz
belegt, wie tatsächlich gesichert ist (sparse). Darin liegen die Snapshots mit allen Linux-Rechten.
Wird das Image zu klein: `sudo netshift resize-image 400`.

Das Passwort liegt nur in `/etc/netshift/smb-credentials` (nur für root lesbar).

**SSH** (Linux-Server, Synology/QNAP mit rsync): Typ *SSH*, dann *SSH-Schlüssel erzeugen/anzeigen*
und den angezeigten `ssh-copy-id`-Befehl einmal ausführen. Ist der Server-Benutzer nicht root,
„--fake-super“ anhaken (Besitzer werden dann als Metadaten gespeichert).

**Verschlüsselung**: Haken bei „Image verschlüsseln“. Der Schlüssel liegt in
`/etc/netshift/image.key` – **diese Datei unbedingt zusätzlich sichern** (USB-Stick, Passwortmanager).
Ohne sie ist eine Wiederherstellung vom Live-USB unmöglich.

## Überwachung und Schutz

**Benachrichtigungen:** Schlägt eine automatische Sicherung fehl, erscheint eine Meldung auf dem
Desktop und oben in netshift ein Hinweis mit „Jetzt sichern“. Ist das NAS nur nicht erreichbar
(z.B. mit dem Notebook unterwegs), meldet netshift sich erst, wenn die letzte erfolgreiche Sicherung
älter als die eingestellte Zeit ist (Standard: 3 Tage). Dieselbe Meldung kommt höchstens einmal am
Tag; sobald es wieder klappt, gibt es eine Entwarnung. Einstellbar unter *Einstellungen → Zeitplan →
Benachrichtigungen*.

**Verbindungsabbrüche:** netshift prüft während einer Sicherung oder Wiederherstellung laufend, ob
das Ziel erreichbar ist. Kurze Aussetzer werden überbrückt; ist die Verbindung länger als 45 Sekunden
weg, bricht netshift mit einer klaren Meldung ab, statt hängen zu bleiben. Ein angefangener
Snapshot wird beim nächsten Mal fortgesetzt; eine abgebrochene Wiederherstellung einfach erneut
starten. Vor einer Wiederherstellung über **WLAN** warnt netshift. Beim Herunterfahren wird das
Sicherungsziel ausgehängt, solange das Netzwerk noch da ist – das System hängt nicht mehr.

**Platzmangel:** Wird es auf dem Ziel eng, zeigt netshift das rechtzeitig an (mit „Vergrößern…“
beim Image-Container). Reicht der Platz für einen Snapshot nicht, löscht netshift die ältesten
**automatischen** Snapshots – aber nur, wenn das nachweislich genug Platz schafft, und nur so viele
wie nötig. Manuelle Snapshots, Sicherheits-Snapshots und der neueste Snapshot bleiben immer
erhalten. Abschaltbar unter *Einstellungen → Speicher*.

## Wiederherstellen

### Laufendes System (wie bei Timeshift)
Snapshot auswählen → **Wiederherstellen** → „Laufendes System“ → optional zuerst *Vorschau der
Änderungen* → **Wiederherstellen**. netshift legt vorher einen Sicherheits-Snapshot an, spielt die
Systemdateien zurück und startet neu. Home-Verzeichnisse bleiben unverändert (sofern nicht
ausdrücklich mitgesichert).

Terminal: `sudo netshift restore latest` (oder Nummer/Name aus `sudo netshift list`).

### System bootet nicht mehr → Linux-USB-Boot-Stick (Rettungssystem)

**Einmalig vorbereiten** (solange alles läuft): Einen Linux-USB-Boot-Stick starten und darauf
netshift installieren. Das Live-System muss Arch-basiert sein (z.B. Manjaro, EndeavourOS oder Arch),
weil der Installer pacman verwendet:
```bash
git clone https://github.com/riccen-arch/netshift.git && cd netshift && sudo sh install.sh
```
(oder ohne Git wie oben unter „Installation“ beschrieben). Dann netshift öffnen → Einstellungen:
- **Name im Backup:** genau wie beim Rechner, z.B. `meinrechner`
- **Rettungssystem (USB-Stick):** einschalten
- Sicherungsziel wie am Rechner eintragen (Server, Freigabe, Benutzer, Passwort) → *Testen*

Im Rettungssystem erstellt netshift nie Snapshots, Zeitplan und Update-Snapshots sind aus, und
„Dieses System“ wird beim Wiederherstellen nicht angeboten. Zum Ausprobieren ohne Risiko:
Snapshot wählen → Wiederherstellen → Systempartition des Rechners wählen → **Vorschau der
Änderungen** (bindet die Platte nur lesend ein).

**Im Notfall:** USB-Boot-Stick starten → netshift → Snapshot → *Wiederherstellen* → über das Laufwerk-Symbol
die Systempartition des Rechners wählen → *Wiederherstellen*. Danach herunterfahren, Stick abziehen,
Rechner starten.

- Btrfs (z.B. Manjaro-Standard): Das Subvolume `@` wird automatisch verwendet, `@home` bleibt unberührt.
- **Verschlüsselte Festplatte (LUKS):** In der Laufwerksliste steht die Partition mit einem Schloss.
  Anklicken, das Passwort eingeben, mit dem du den Rechner startest – fertig. Wurde sie schon im
  Dateimanager entsperrt, erkennt netshift das selbst. Beim Beenden sperrt netshift sie wieder.
  Im Terminal: `sudo netshift unlock /dev/nvme0n1p2` bzw. `restore … --target /dev/nvme0n1p2`
  (fragt dann direkt nach dem Passwort).
- Die EFI-Partition und ein separates `/home` werden nicht angefasst.
- Gleiche Platte wie vorher: „fstab behalten“ und „GRUB neu erzeugen“ ausgeschaltet lassen.
  Neue Platte: fstab im Zielsystem an die neuen UUIDs anpassen und GRUB neu installieren.

**Zusätzlicher Schutz:** Jeder Snapshot merkt sich den Rechnernamen. Stammen die Sicherungen von
einem anderen Rechner, verweigert netshift das Erstellen von Snapshots und das Zurückspielen auf das
laufende System – auch wenn der Rettungsmodus vergessen wurde.

### netshift Live – eigener startfähiger USB-Stick

Wie Clonezilla: Ein fertiges Live-System, das direkt mit netshift startet – nichts installieren,
keine veraltete Paketliste. Damit lässt sich ein Rechner **sichern** (während er nicht läuft) und
**wiederherstellen**. Es funktioniert auch für Rechner, auf denen netshift gar nicht installiert ist.

1. `netshift-live-x86_64.iso` unter [Releases](https://github.com/riccen-arch/netshift/releases/tag/live-iso)
   herunterladen.
2. Mit **Ventoy**, **balenaEtcher** oder `dd` auf einen USB-Stick schreiben (der Stick wird dabei gelöscht).
3. Den Rechner vom Stick starten (Boot-Menü, meist F12/F8/Esc). **Secure Boot** muss dafür im BIOS
   ausgeschaltet sein.
4. Sprache wählen → Netzwerk prüfen (LAN-Kabel wird erkannt, sonst *WLAN…*) → den Rechner in der
   Liste auswählen (verschlüsselte Partitionen per *Entsperren…*) → **Rechner sichern** oder
   **Wiederherstellen**.

Ist netshift auf dem Rechner eingerichtet, übernimmt das Live-System Sicherungsziel, Zugangsdaten und
Schlüssel automatisch von dessen Festplatte. Sonst fragt es einmal danach (die Angaben gelten nur bis
zum Ausschalten). Offline-Snapshots landen in derselben Sicherung wie die des laufenden Systems.

Auch ohne Live-Stick geht das: Im Rettungssystem (siehe oben) heißt der Knopf oben links
**Rechner sichern**, im Terminal `sudo netshift create --source /dev/nvme0n1p2`.

### Einzelne Dateien
**Dateien zurückholen** → Datei/Ordner im Snapshot wählen → an Originalort oder anderswohin.
Terminal: `sudo netshift restore-files latest /etc/pacman.conf --to /tmp/alt`

Zum freien Stöbern: **Durchsuchen** (öffnet den Snapshot im Dateimanager).

## Befehle

```
netshift create -c "vor Kernel-Update"   Snapshot erstellen
netshift create --source /dev/…          Nicht laufendes System sichern (vom USB-Stick)
netshift import-config /dev/…            Einstellungen von der Festplatte übernehmen
netshift list                            Snapshots anzeigen
netshift diff 3                          Was würde ein Restore von Nr. 3 ändern?
netshift restore 3 [--target …]          Wiederherstellen
netshift restore-files 3 /etc/X [--to D] Einzelne Pfade zurückholen
netshift delete 1 2                      Löschen
netshift comment 3 "funktioniert"        Kommentar ändern
netshift size [3]                        Belegter Platz (nur dieser / gesamt)
netshift unlock|lock /dev/…              Verschlüsselte Partition entsperren / sperren
netshift netcheck                        Ziel erreichbar? Verbindung über WLAN?
netshift status                          Übersicht inkl. letzter Sicherung und Problemen
netshift mount / umount                  Snapshots zum Durchsuchen einbinden
netshift test | status | setup           Verbindung testen / Übersicht / Einrichtung
netshift resize-image 400                Image-Container vergrößern
```

## Gut zu wissen
- Ausgeschlossen werden standardmäßig `/home`, `/root`, Caches, Papierkorb, `/mnt`, `/media`,
  Swapfile, Pacman-Paketcache sowie automatisch alle eingehängten Netzwerk- und Temp-Dateisysteme.
  Eigene Ausschlüsse unter *Einstellungen → Dateien & Ausschlüsse*.
- `/etc/netshift` wird bei einer Wiederherstellung nie überschrieben (Zugangsdaten bleiben aktuell).
- Manuelle Snapshots und Sicherheits-Snapshots werden nie automatisch gelöscht; für geplante gilt die
  eingestellte Anzahl pro Stufe (ein Snapshot kann mehrere Stufen tragen, wie bei Timeshift).
- Der erste Snapshot überträgt das ganze System (typisch 10–30 GB), danach nur Änderungen.
- Probleme mit SMB? *Zusätzliche Mount-Optionen* z.B. `vers=2.1` oder `sec=ntlmssp` probieren.
- Sprache: richtet sich nach der Systemsprache (Deutsch, sonst Englisch). Erzwingen z.B. mit
  `sudo NETSHIFT_LANG=en netshift list`. Weitere Sprachen: `python3 tools/update-po.py fr` legt
  `po/fr.po` an.
- Ausführliche Ausgabe: `sudo NETSHIFT_DEBUG=1 netshift create`
- Protokoll geplanter Läufe: `journalctl -u netshift-check -u netshift-boot`
