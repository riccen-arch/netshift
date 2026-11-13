# netshift

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

## Funktionen

- Inkrementelle Snapshots auf NAS, Server oder USB-Laufwerk
- Wiederherstellen mit Vorschau – auch vom USB-Stick (Rettungsmodus), auch auf verschlüsselte Platten
- Zeitplan, Snapshot vor Updates, Benachrichtigung bei Problemen

## Installation

```bash
git clone https://github.com/riccen-arch/netshift.git && cd netshift && sudo sh install.sh
```

Aktualisieren: `cd netshift && git pull && sudo sh install.sh`

📖 **[Anleitung](docs/ANLEITUNG.md)** · Lizenz: GPL-3.0
