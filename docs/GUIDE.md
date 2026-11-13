# netshift – Guide

[← Back to overview](../README.md) · [🇩🇪 Deutsch](ANLEITUNG.md)

## What netshift can do

| Feature | Timeshift (rsync) | netshift |
|---|---|---|
| Incremental snapshots with hard links | ✔ | ✔ |
| Target: local partition | ✔ | ✔ (also USB, `UUID=…`, any folder) |
| Target: SMB/CIFS, NFS, SSH | ✘ | ✔ |
| Keep owners/permissions/ACLs/xattrs even on SMB | – | ✔ via ext4 image container |
| Encrypted backup (LUKS2) | ✘ | ✔ (optional) |
| Schedule: boot/hourly/daily/weekly/monthly | ✔ | ✔ (missed runs are caught up) |
| Snapshot before pacman/pamac updates | only with an extra package | ✔ built in |
| Restore the running system + reboot | ✔ | ✔ |
| Restore from a live USB / onto a new disk | ✔ | ✔ (Btrfs `@` is detected, GRUB optional) |
| Preview “what would change?” | ✘ | ✔ |
| Restore single files/folders | only via file manager | ✔ (also to another place, also with SSH) |
| Resume an interrupted backup | ✘ | ✔ |
| Safety snapshot before a restore | ✘ | ✔ |
| Exclude network/temp mounts automatically | ✘ | ✔ |

## Installation

**With Git** (recommended – updating is then a single command):
```bash
sudo pacman -S --needed git
git clone https://github.com/riccen-arch/netshift.git
cd netshift && sudo sh install.sh
```
Update:
```bash
cd netshift && git pull && sudo sh install.sh
```

**Without Git** (downloads the current version and installs it):
```bash
curl -L https://github.com/riccen-arch/netshift/archive/refs/heads/main.tar.gz | tar xz
cd netshift-main && sudo sh install.sh
```

The installer checks what is missing, installs only those packages (no makepkg/base-devel needed)
and repairs an outdated package list along the way (mirrors, keys, `pacman -Syu`) – typical for a
rarely used USB stick. If the modern interface cannot be installed, the classic one is used.
Settings and backups are kept when updating.

- Check only: `sudo sh install.sh --check`
- Uninstall: `sudo sh install.sh --uninstall`
- Log if something goes wrong: `/tmp/netshift-install.log`
- As a pacman package from the git folder: `makepkg -si`

### For developers
- `tools/build-release.sh` creates the single-file installer and the archive of a version in `dist/`.
- `packaging/aur/PKGBUILD` is the template for the AUR.
- Translations: German is the source language; `po/en.po` holds the English texts.
  `python3 tools/update-po.py` updates all `po/*.po` files, `python3 tools/update-po.py fr` creates
  a new language, `--check` lists missing translations.

## Interface

Modern GTK4/libadwaita interface: snapshots grouped by date, storage overview, progress with a
cancel button, preview of changes with filter, dark mode (follows the GNOME and KDE setting).
Shortcuts: Ctrl+N new snapshot, F5 refresh, Ctrl+, settings. If GTK4/libadwaita are missing, the
classic Tk interface starts automatically.

**Language:** netshift follows the system language – German if the system is set to German,
otherwise English. To force a language: `sudo NETSHIFT_LANG=en netshift list`.

## Setup (example: NAS via SMB)

In the interface under **Settings → Backup target**:
- Type: *SMB / Windows share / NAS*
- Server: `192.168.1.10`, share: `backup`, user + password
- “Test connection” → *Test* (settings are saved automatically when closing)

netshift creates `netshift/<computer name>.img` on the share – a file that only uses as much space
as is actually backed up (sparse). It holds the snapshots with all Linux permissions.
If the image becomes too small: `sudo netshift resize-image 400`.

The password is only stored in `/etc/netshift/smb-credentials` (readable by root only).

**SSH** (Linux server, Synology/QNAP with rsync): type *SSH*, then *SSH key* and run the displayed
`ssh-copy-id` command once. If the server user is not root, tick “User is not root”
(`--fake-super`; owners are then stored as metadata).

**Encryption:** enable “Encrypt image (LUKS2)”. The key is stored in `/etc/netshift/image.key` –
**be sure to back up this file separately** (USB stick, password manager). Without it, a restore
from a live USB is impossible.

## Monitoring and protection

**Notifications:** if an automatic backup fails, a desktop notification appears and netshift shows a
hint with “Back up now” at the top. If the NAS is merely unreachable (e.g. when travelling with the
notebook), netshift only reports once the last successful backup is older than the configured time
(default: 3 days). The same message comes at most once a day; as soon as it works again, you get an
all-clear. Configurable under *Settings → Schedule → Notifications*.

**Connection drops:** during a backup or restore netshift continuously checks whether the target is
reachable. Short dropouts are bridged; if the connection is gone for more than 45 seconds, netshift
stops with a clear message instead of hanging. A started snapshot is resumed next time; simply start
an interrupted restore again. netshift warns before a restore over **Wi-Fi**. On shutdown the backup
target is unmounted while the network is still up – the system no longer hangs.

**Low space:** if space on the target runs low, netshift shows it in time (with “Enlarge…” for the
image container). If there is not enough space for a snapshot, netshift deletes the oldest
**automatic** snapshots – but only if that demonstrably frees enough space, and only as many as
needed. Manual snapshots, safety snapshots and the newest snapshot are always kept. Can be switched
off under *Settings → Storage*.

## Restoring

### Running system (like Timeshift)
Select a snapshot → **Restore…** → “This system” → optionally *Preview changes* first →
**Restore**. netshift creates a safety snapshot first, writes the system files back and reboots.
Home directories stay unchanged (unless they were explicitly backed up).

Terminal: `sudo netshift restore latest` (or a number/name from `sudo netshift list`).

### System no longer boots → Linux USB boot stick (rescue system)

**Prepare once** (while everything works): boot a Linux USB boot stick and install netshift on it.
The live system must be Arch-based (e.g. Manjaro, EndeavourOS or Arch), because the installer uses pacman:
```bash
git clone https://github.com/riccen-arch/netshift.git && cd netshift && sudo sh install.sh
```
(or without Git as described above under “Installation”). Then open netshift → Settings:
- **Name in backup:** exactly as on the computer, e.g. `mycomputer`
- **Rescue system – restore only:** switch on
- Enter the backup target as on the computer (server, share, user, password) → *Test*

In the rescue system netshift never creates snapshots, schedule and update snapshots are off, and
“This system” is not offered when restoring. To try it out without risk: select a snapshot →
Restore… → choose the system partition of the computer → **Preview changes** (mounts the disk
read-only).

**In an emergency:** boot the USB boot stick → netshift → snapshot → *Restore…* → choose the system
partition of the computer via the drive icon → *Restore*. Then shut down, unplug the stick and
start the computer.

- Btrfs (e.g. Manjaro default): the subvolume `@` is used automatically, `@home` stays untouched.
- **Encrypted disk (LUKS):** the partition is shown with a lock in the drive list. Click it and
  enter the password you use to start the computer – done. If it was already unlocked in the file
  manager, netshift detects that itself. When quitting, netshift locks it again.
  In the terminal: `sudo netshift unlock /dev/nvme0n1p2` or `restore … --target /dev/nvme0n1p2`
  (then asks for the password directly).
- The EFI partition and a separate `/home` are not touched.
- Same disk as before: leave “Keep fstab and crypttab” and “Regenerate GRUB configuration” off.
  New disk: adjust fstab in the target system to the new UUIDs and reinstall GRUB.

**Additional protection:** every snapshot remembers the computer name. If the backups come from a
different computer, netshift refuses to create snapshots and to restore onto the running system –
even if rescue mode was forgotten.

### Single files
**Restore files…** → choose a file/folder in the snapshot → to the original location or elsewhere.
Terminal: `sudo netshift restore-files latest /etc/pacman.conf --to /tmp/old`

To browse freely: **Open in file manager** (opens the snapshot in the file manager).

## Commands

```
netshift create -c "before kernel update"  Create snapshot
netshift list                              Show snapshots
netshift diff 3                            What would restoring no. 3 change?
netshift restore 3 [--target …]            Restore
netshift restore-files 3 /etc/X [--to D]   Restore single paths
netshift delete 1 2                        Delete
netshift comment 3 "works"                 Change comment
netshift size [3]                          Used space (only this one / total)
netshift unlock|lock /dev/…                Unlock / lock an encrypted partition
netshift netcheck                          Target reachable? Connected via Wi-Fi?
netshift status                            Overview incl. last backup and problems
netshift mount / umount                    Mount snapshots for browsing
netshift test | status | setup             Test connection / overview / setup
netshift resize-image 400                  Enlarge the image container
```

## Good to know
- Excluded by default: `/home`, `/root`, caches, trash, `/mnt`, `/media`, swap file, the pacman
  package cache and automatically all mounted network and temporary file systems.
  Custom exclusions under *Settings → Exclusions*.
- `/etc/netshift` is never overwritten by a restore (credentials stay current).
- Manual snapshots and safety snapshots are never deleted automatically; for scheduled ones the
  configured number per level applies (one snapshot can carry several levels, like in Timeshift).
- The first snapshot transfers the whole system (typically 10–30 GB), after that only changes.
- Problems with SMB? Try *Additional mount options* such as `vers=2.1` or `sec=ntlmssp`.
- Verbose output: `sudo NETSHIFT_DEBUG=1 netshift create`
- Log of scheduled runs: `journalctl -u netshift-check -u netshift-boot`
