# netshift

🇬🇧 English · [🇩🇪 Deutsch](README.de.md)

System snapshots for Manjaro/Arch – like Timeshift, but also to a **NAS (SMB), NFS and SSH**.

<p align="center">
  <img src="docs/screenshots/main.png" alt="netshift main window" width="780">
</p>

<table>
  <tr>
    <td width="50%"><img src="docs/screenshots/preview.png" alt="Preview before restoring"></td>
    <td width="50%"><img src="docs/screenshots/settings-dark.png" alt="Settings in dark mode"></td>
  </tr>
</table>

## Features

- Incremental snapshots to a NAS, a server or a USB drive
- Restore with preview – also from a USB stick (rescue mode), also onto encrypted disks
- Schedule, snapshot before updates, notification when something goes wrong
- English and German – the language is detected automatically

## Installation

```bash
git clone https://github.com/riccen-arch/netshift.git && cd netshift && sudo sh install.sh
```

Update: `cd netshift && git pull && sudo sh install.sh`

📖 **[Guide](docs/GUIDE.md)** · License: GPL-3.0
