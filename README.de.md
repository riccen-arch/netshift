# netshift

[🇬🇧 English](README.md) · 🇩🇪 Deutsch

System-Snapshots für Manjaro/Arch – wie Timeshift, aber auch auf **NAS (SMB), NFS und SSH**.

<p align="center">
  <img src="docs/screenshots/hauptfenster.png" alt="netshift Hauptfenster" width="780">
</p>

<table>
  <tr>
    <td width="50%"><img src="docs/screenshots/vorschau.png" alt="Vorschau vor dem Wiederherstellen"></td>
    <td width="50%"><img src="docs/screenshots/einstellungen-dunkel.png" alt="Einstellungen im Dunkelmodus"></td>
  </tr>
</table>

## Warum netshift?

netshift ist entstanden, weil mir ein Problem schon lange unter den Nägeln brannte: Ich wollte meine
Sicherungen nicht zwingend auf lokalen Datenträgern ablegen müssen. Stattdessen sollten die Snapshots –
so einfach, wie man es von Timeshift kennt – direkt auf einem Laufwerk im Netzwerk landen, ganz ohne
umständliches Hin- und Herkopieren. Genau daraus ist netshift geworden.

Ich lade euch herzlich ein, die Software auf Herz und Nieren zu testen. Alle Testdurchläufe, die ich
bisher durchgeführt habe, wurden erfolgreich abgeschlossen – über Rückmeldungen und Erfahrungsberichte
freue ich mich sehr.

## Funktionen

- **Sicherungsziele:** NAS/Windows-Freigabe (SMB), NFS, SSH-Server, USB-Laufwerk oder lokaler Ordner
- **Inkrementell:** Der erste Snapshot kopiert das System, danach nur Änderungen – unveränderte Dateien werden per Hardlink geteilt
- **Alle Linux-Rechte bleiben erhalten**, auch auf dem NAS (ext4-Image-Container, optional mit LUKS2 verschlüsselt)
- **Automatisch:** beim Start, stündlich, täglich, wöchentlich, monatlich und vor jedem pacman-/pamac-Update – verpasste Termine werden nachgeholt
- **Wiederherstellen mit Vorschau:** erst sehen, was sich ändern würde; auch einzelne Dateien zurückholen
- **netshift Live:** startfähiger USB-Stick (wie Clonezilla), um einen Rechner zu sichern oder wiederherzustellen, während er nicht läuft – auch verschlüsselte Platten
- **Sicher:** Sicherheits-Snapshot vor jeder Wiederherstellung, abgebrochene Sicherungen werden fortgesetzt, kein Hängen bei WLAN-Abbruch oder beim Herunterfahren
- **Benachrichtigung**, wenn eine Sicherung fehlschlägt oder die letzte zu alt ist
- Moderne GTK4-Oberfläche mit Dunkelmodus, klassische Oberfläche und Terminal als Rückfall
- Deutsch und Englisch – die Sprache wird automatisch erkannt

## Installation

```bash
git clone https://github.com/riccen-arch/netshift.git && cd netshift && sudo sh install.sh
```

Der Installer ergänzt nur fehlende Pakete und funktioniert auch auf einem veralteten Live-USB-Stick.
Aktualisieren: `cd netshift && git pull && sudo sh install.sh`

## Erste Schritte

1. **netshift** im Startmenü öffnen
2. **Einstellungen → Sicherungsziel:** z.B. SMB, Server `192.168.1.10`, Freigabe `backup`, Benutzer und Passwort → *Testen*
3. **Snapshot erstellen** – fertig. Den Zeitplan unter *Einstellungen → Zeitplan* festlegen.

**Tipp:** einen **netshift-Live**-USB-Stick bereithalten – die ISO unter
[Releases](https://github.com/riccen-arch/netshift/releases/tag/live-iso) laden und mit Ventoy oder balenaEtcher aufspielen.
Startet der Rechner nicht mehr, lässt er sich vom Stick aus wiederherstellen.

## Voraussetzungen

Manjaro oder Arch Linux · Python 3 · rsync – alles Weitere installiert der Installer.

📖 **[Ausführliche Anleitung](docs/ANLEITUNG.md)** (Einrichtung, Wiederherstellen, Rettungs-Stick, Befehle) · Lizenz: GPL-3.0
