#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
netshift – inkrementelle System-Snapshots für Linux (Manjaro/Arch), ähnlich Timeshift
im rsync-Modus, aber mit Netzwerkzielen:

  * lokal (Verzeichnis, Gerät, UUID=/LABEL=)
  * SMB/CIFS (Windows-Freigabe, NAS, Samba)
  * NFS
  * SSH (rsync über ssh zu einem Linux-Server/NAS)

Auf SMB/NFS (und FAT/exFAT/NTFS) werden die Snapshots in einem ext4-Image-Container
abgelegt, damit Besitzer, Rechte, ACLs, xattrs, Symlinks und Hardlinks exakt erhalten
bleiben – optional LUKS-verschlüsselt. Wiederherstellung erfolgt auf demselben Weg.
"""

import argparse
import datetime as dt
import fcntl
import json
import os
import re
import secrets
import shlex
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time

VERSION = "1.1.5"
ETC_DIR = os.environ.get("NETSHIFT_ETC", "/etc/netshift")
CONFIG_PATH = os.environ.get("NETSHIFT_CONFIG", os.path.join(ETC_DIR, "config.json"))
RUN_DIR = os.environ.get("NETSHIFT_RUNDIR", "/run/netshift")
STATE_DIR = os.environ.get("NETSHIFT_STATEDIR", "/var/lib/netshift")
NET_TIMEOUT = int(os.environ.get("NETSHIFT_NET_TIMEOUT", "45"))   # Sekunden ohne Verbindung → Abbruch
NET_PROBE = None          # (host, port) des Netzwerkziels, gesetzt beim Öffnen des Ziels
SOURCE = os.environ.get("NETSHIFT_SOURCE", "/")  # nur für Tests ändern
GUI_MODE = os.environ.get("NETSHIFT_GUI") == "1"
TIME_FMT = "%Y-%m-%d_%H-%M-%S"
INPROGRESS = ".in-progress"
SNAP_RE = re.compile(r"^\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2}$")

TAG_NAMES = {
    "O": "Manuell", "B": "Boot", "H": "Stündlich", "D": "Täglich",
    "W": "Wöchentlich", "M": "Monatlich", "P": "Pacman", "R": "Vor Wiederherstellung",
}
# (Konfig-Schlüssel, Tag, Intervall in Sekunden)
LEVELS = [("hourly", "H", 3600), ("daily", "D", 86400),
          ("weekly", "W", 7 * 86400), ("monthly", "M", 30 * 86400)]
RETENTION_TAGS = {"boot": "B", "hourly": "H", "daily": "D", "weekly": "W", "monthly": "M"}

NETWORK_FS = {"cifs", "smb3", "smbfs", "nfs", "nfs4", "fuse.sshfs", "sshfs", "9p",
              "fuse.rclone", "davfs", "fuse.davfs2", "glusterfs", "ceph", "fuse.gvfsd-fuse",
              "afs", "fuse.s3fs"}
VOLATILE_FS = {"tmpfs", "proc", "sysfs", "devtmpfs", "devpts", "cgroup", "cgroup2",
               "securityfs", "debugfs", "tracefs", "configfs", "fusectl", "pstore",
               "efivarfs", "bpf", "mqueue", "hugetlbfs", "autofs", "binfmt_misc",
               "squashfs", "overlay", "ramfs", "rpc_pipefs", "nsfs"}
NON_POSIX_FS = {"vfat", "msdos", "exfat", "ntfs", "ntfs3", "fuseblk"}

# Immer ausgeschlossen (nicht abschaltbar)
FORCED_EXCLUDES = [
    "/dev/*", "/proc/*", "/sys/*", "/run/*", "/tmp/*",
    "/var/lib/pacman/db.lck", "/lost+found",
]
HOME_EXCLUDES = ["/home/*", "/root/*"]
PACMAN_CACHE_EXCLUDES = ["/var/cache/pacman/pkg/*"]
# Bei der Wiederherstellung nie überschreiben/löschen
RESTORE_PROTECT = ["/etc/netshift"]
FSTAB_PROTECT = ["/etc/fstab", "/etc/crypttab"]

DEFAULT_CONFIG = {
    "machine_name": "",
    "target": {
        "type": "smb",               # local | smb | nfs | ssh
        "path": "",                  # local: Verzeichnis, /dev/sdX1, UUID=..., LABEL=...
        "server": "",                # smb/nfs/ssh: Hostname oder IP
        "share": "",                 # smb: Freigabename (darf Unterordner enthalten: share/ordner)
        "export": "",                # nfs: exportierter Pfad, z.B. /volume1/backup
        "smb_version": "3.0",
        "mount_options": "",
        "ssh_user": "root",
        "ssh_port": 22,
        "ssh_key": os.path.join(ETC_DIR, "id_ed25519"),
        "ssh_path": "/srv/netshift",
        "ssh_fake_super": False,
        "container": "auto",         # auto | image | direct
        "image_size_gb": 200,
        "encrypt": False,
        "key_file": os.path.join(ETC_DIR, "image.key"),
        "subdir": "netshift",
    },
    "include_home": False,
    "exclude_pacman_cache": True,
    "exclude": [
        "/mnt/*", "/media/*", "/swapfile", "/swap/swapfile",
        "/var/tmp/*", "/var/lib/systemd/coredump/*",
        "/root/.cache/*", "/home/*/.cache/*", "/home/*/.local/share/Trash/*",
        "/home/*/.gvfs", "/home/*/.thumbnails/*",
        "/var/lib/flatpak/repo/tmp/*",
    ],
    "schedule": {
        "boot": {"enabled": False, "keep": 3},
        "hourly": {"enabled": False, "keep": 6},
        "daily": {"enabled": True, "keep": 7},
        "weekly": {"enabled": True, "keep": 4},
        "monthly": {"enabled": False, "keep": 3},
    },
    "rescue_mode": False,            # Rettungssystem (USB-Stick): nur wiederherstellen
    "notify": {"enabled": True, "overdue_days": 3},
    "auto_cleanup": True,            # bei Platzmangel älteste automatische Snapshots löschen
    "pacman_hook": {"enabled": False, "keep": 5, "min_interval_minutes": 30,
                    "abort_on_fail": False},
    "min_free_gb": 5,
    "rsync_extra_args": [],
}


# --------------------------------------------------------------------------- Hilfen

class NetshiftError(Exception):
    def __init__(self, msg, kind="error"):
        super().__init__(msg)
        self.kind = kind          # error | offline | space


def die(msg, kind="error"):
    raise NetshiftError(msg, kind)


def info(msg):
    print(msg, flush=True)


def warn(msg):
    print("WARNUNG: " + msg, file=sys.stderr if not GUI_MODE else sys.stdout, flush=True)


def run(cmd, check=True, capture=False, input=None, quiet=False, env=None, timeout=None):
    if not quiet and os.environ.get("NETSHIFT_DEBUG"):
        print("+ " + " ".join(shlex.quote(c) for c in cmd), flush=True)
    try:
        p = subprocess.run(cmd, input=input, env=env,
                           stdout=subprocess.PIPE if capture else None,
                           stderr=subprocess.PIPE if capture else None,
                           text=True, timeout=timeout)
    except FileNotFoundError:
        die(f"Programm nicht gefunden: {cmd[0]} – bitte installieren.")
    except subprocess.TimeoutExpired:
        p = subprocess.CompletedProcess(cmd, 124, "", f"Zeitüberschreitung nach {timeout} s")
    if check and p.returncode != 0:
        err = (p.stderr or "").strip() if capture else ""
        die(f"Befehl fehlgeschlagen ({p.returncode}): {' '.join(cmd)}" + (f"\n{err}" if err else ""))
    return p


def need(prog, pkg=None):
    if shutil.which(prog) is None:
        die(f"'{prog}' fehlt. Installieren mit: sudo pacman -S {pkg or prog}")


def hostname():
    return socket.gethostname().split(".")[0] or "linux"


def safe_name(s):
    return re.sub(r"[^A-Za-z0-9._-]", "_", s) or "linux"


def human(n):
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if abs(n) < 1024 or unit == "TiB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{n} B"
        n /= 1024.0


def deep_merge(base, override):
    out = dict(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def load_config(path=None):
    path = path or CONFIG_PATH
    data = {}
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    cfg = deep_merge(DEFAULT_CONFIG, data)
    if not cfg.get("machine_name"):
        cfg["machine_name"] = hostname()
    return cfg


def save_config(cfg, path=None):
    path = path or CONFIG_PATH
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def cred_path():
    return os.path.join(ETC_DIR, "smb-credentials")


def read_smb_credentials():
    d = {"username": "", "password": "", "domain": ""}
    p = cred_path()
    if os.path.exists(p):
        with open(p, encoding="utf-8") as f:
            for line in f:
                if "=" in line:
                    k, v = line.rstrip("\n").split("=", 1)
                    d[k.strip()] = v
    return d


def write_smb_credentials(username, password, domain=""):
    os.makedirs(ETC_DIR, mode=0o700, exist_ok=True)
    p = cred_path()
    fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(f"username={username}\npassword={password}\n")
        if domain:
            f.write(f"domain={domain}\n")
    os.chmod(p, 0o600)


def rsync_escape(path):
    return re.sub(r"([*?\[\]\\])", r"\\\1", path)


def mountinfo():
    """Liefert [(mountpoint, fstype)] des laufenden Systems."""
    res = []
    try:
        with open("/proc/self/mountinfo") as f:
            for line in f:
                left, _, right = line.partition(" - ")
                mp = left.split()[4]
                fstype = right.split()[0]
                mp = re.sub(r"\\(\d{3})", lambda m: chr(int(m.group(1), 8)), mp)
                res.append((mp, fstype))
    except OSError:
        pass
    return res


def fs_type_of(path):
    p = run(["findmnt", "-n", "-o", "FSTYPE", "-T", path], check=False, capture=True, quiet=True)
    return (p.stdout or "").strip().splitlines()[0] if p.returncode == 0 and p.stdout.strip() else ""


# --------------------------------------------------------------------------- Netzwerk

def target_probe(t):
    """(host, port) eines Netzwerkziels oder None bei lokalen Zielen."""
    typ = t.get("type")
    if typ == "smb":
        return (t.get("server"), 445)
    if typ == "nfs":
        return (t.get("server"), 2049)
    if typ == "ssh":
        return (t.get("server"), int(t.get("ssh_port") or 22))
    return None


def reachable(probe, timeout=4):
    if not probe or not probe[0]:
        return True
    try:
        with socket.create_connection(probe, timeout=timeout):
            return True
    except OSError:
        return False


def offline_msg(probe):
    return (f"Das Sicherungsziel {probe[0]} ist nicht erreichbar. Ist das NAS eingeschaltet und der "
            "Rechner mit dem Heimnetz verbunden?")


def route_info(host):
    """Über welche Netzwerkschnittstelle geht die Verbindung zum Ziel – und ist es WLAN?"""
    res = {"host": host, "iface": None, "wireless": False, "ip": None}
    try:
        res["ip"] = socket.getaddrinfo(host, None)[0][4][0]
    except OSError:
        return res
    dev = None
    if shutil.which("ip"):
        p = run(["ip", "-j", "route", "get", res["ip"]], check=False, capture=True, quiet=True)
        try:
            dev = json.loads(p.stdout or "[]")[0].get("dev")
        except (ValueError, IndexError, AttributeError):
            dev = None
    if dev is None and ":" not in res["ip"]:
        dev = _route_dev_proc(res["ip"])
    res["iface"] = dev
    if dev:
        res["wireless"] = os.path.isdir(f"/sys/class/net/{dev}/wireless") or dev.startswith("wl")
    return res


def _route_dev_proc(ip):
    """Schnittstelle zu einer IPv4-Adresse aus /proc/net/route (längster Präfix)."""
    import struct
    try:
        target = struct.unpack("<I", socket.inet_aton(ip))[0]
        best, best_len = None, -1
        with open("/proc/net/route") as f:
            next(f)
            for line in f:
                c = line.split()
                dest, mask = int(c[1], 16), int(c[7], 16)
                if target & mask == dest:
                    plen = bin(mask).count("1")
                    if plen > best_len:
                        best, best_len = c[0], plen
        return best
    except (OSError, ValueError, StopIteration):
        return None


# --------------------------------------------------------------------------- Status & Benachrichtigung

AUTO_TAGS = set("BHDWMP")      # automatisch erstellte Snapshots


def status_path():
    return os.path.join(STATE_DIR, "status.json")


def load_status():
    try:
        with open(status_path(), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_status(st):
    try:
        os.makedirs(STATE_DIR, mode=0o755, exist_ok=True)
        tmp = status_path() + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(st, f, indent=2, ensure_ascii=False)
        os.chmod(tmp, 0o644)
        os.replace(tmp, status_path())
    except OSError:
        pass


def sync_status(storage, st=None):
    """Status aus dem geöffneten Ziel aktualisieren (letzte Sicherungen, Platz)."""
    own = st is None
    st = load_status() if own else st
    snaps = [x for x in snapshots(storage) if x["valid"]]
    if snaps:
        st["last_success"] = max(st.get("last_success") or 0, snaps[-1]["epoch"])
    by_tag = {}
    for x in snaps:
        for tg in x["tags"]:
            by_tag[tg] = max(by_tag.get(tg, 0), x["epoch"])
    st["last_by_tag"] = by_tag
    try:
        free, total = storage.free()
        st["space"] = {"free": free, "total": total, "time": time.time()}
    except Exception:
        pass
    if own:
        save_status(st)
    return st


def record_success(st):
    st["last_success"] = time.time()
    st["last_error"] = None
    st["failures"] = 0


def record_failure(st, err, source="auto"):
    kind = getattr(err, "kind", "error")
    st["last_error"] = {"time": time.time(), "msg": str(err), "kind": kind, "source": source}
    st["failures"] = int(st.get("failures") or 0) + 1


def space_need(cfg, total):
    """Mindestens frei zu haltender Platz auf dem Ziel."""
    return max(float(cfg.get("min_free_gb", 5)) * 1024 ** 3, 0.05 * (total or 0))


def health(cfg, st=None, now=None):
    """Liste der aktuellen Probleme [{kind, level, title, body}]."""
    if cfg.get("rescue_mode"):
        return []
    st = load_status() if st is None else st
    now = now or time.time()
    out = []
    last = st.get("last_success")
    err = st.get("last_error")
    if err and err.get("kind") != "offline" and (not last or err["time"] > last):
        when = dt.datetime.fromtimestamp(err["time"]).strftime("%d.%m. %H:%M")
        title = ("Letzte automatische Sicherung fehlgeschlagen" if err.get("source") != "manual"
                 else "Letzte Sicherung fehlgeschlagen")
        out.append({"kind": "space" if err.get("kind") == "space" else "error", "level": "error",
                    "title": title, "body": f"{when}: {err['msg'].splitlines()[0]}"})
    days = float((cfg.get("notify") or {}).get("overdue_days", 3) or 0)
    auto = any(cfg["schedule"].get(l, {}).get("enabled") for l in ("hourly", "daily", "weekly", "monthly"))
    if auto and days > 0:
        ref = last or st.get("first_seen")
        if ref and now - ref > days * 86400:
            age = int((now - ref) // 86400)
            body = (f"Die letzte erfolgreiche Sicherung ist {age} Tage alt." if last
                    else f"Seit {age} Tagen wurde noch keine Sicherung erstellt.")
            if err and err.get("kind") == "offline" and (not last or err["time"] > last):
                body += " Das Sicherungsziel war zuletzt nicht erreichbar."
            out.append({"kind": "overdue", "level": "warning", "title": "Sicherung überfällig", "body": body})
    sp = st.get("space") or {}
    if sp.get("total") and not any(p["kind"] == "space" for p in out):
        warn_at = max(2 * space_need(cfg, sp["total"]), 0.15 * sp["total"])
        if sp["free"] < warn_at:
            out.append({"kind": "space", "level": "warning", "title": "Sicherungsziel fast voll",
                        "body": f"Noch {human(sp['free'])} von {human(sp['total'])} frei."})
    return out


def _notify_targets():
    """Session-Busse der angemeldeten Benutzer: [(benutzername|None, busadresse)]."""
    if os.environ.get("NETSHIFT_NOTIFY_BUS"):
        return [(None, os.environ["NETSHIFT_NOTIFY_BUS"])]
    out = []
    import pwd
    try:
        uids = [int(d) for d in os.listdir("/run/user") if d.isdigit() and int(d) >= 1000]
    except OSError:
        uids = []
    for uid in uids:
        bus = f"/run/user/{uid}/bus"
        if os.path.exists(bus):
            try:
                out.append((pwd.getpwuid(uid).pw_name, f"unix:path={bus}"))
            except KeyError:
                pass
    return out


def _gv_str(s):
    return "'" + s.replace("\\", "\\\\").replace("'", "\\'") + "'"


def desktop_notify(title, body, urgency=1):
    """Benachrichtigung an alle angemeldeten Benutzer (über org.freedesktop.Notifications)."""
    if not shutil.which("gdbus"):
        return 0
    sent = 0
    for user, bus in _notify_targets():
        cmd = ["gdbus", "call", "--session", "--dest", "org.freedesktop.Notifications",
               "--object-path", "/org/freedesktop/Notifications",
               "--method", "org.freedesktop.Notifications.Notify",
               "'netshift'", "0", "'drive-harddisk'", _gv_str(title), _gv_str(body), "@as []",
               "{'urgency': <byte %d>, 'desktop-entry': <'netshift'>}" % urgency, "int32 -1"]
        env_cmd = ["env", f"DBUS_SESSION_BUS_ADDRESS={bus}"] + cmd
        if user and os.geteuid() == 0:
            env_cmd = ["runuser", "-u", user, "--"] + env_cmd
        if run(env_cmd, check=False, capture=True, quiet=True, timeout=10).returncode == 0:
            sent += 1
    return sent


def notify_health(cfg, st):
    """Probleme melden – gedrosselt, damit nicht jede Stunde dieselbe Meldung kommt."""
    if cfg.get("rescue_mode") or not (cfg.get("notify") or {}).get("enabled", True):
        return
    now = time.time()
    notified = st.setdefault("notified", {})
    problems = health(cfg, st, now)
    err = st.get("last_error") or {}
    repeat = {"error": 86400, "overdue": 86400, "space": 3 * 86400}
    current = set()
    for p in problems:
        if p["kind"] == "error" and err.get("source") == "manual":
            continue                      # sieht der Benutzer ohnehin
        key = p["kind"] + ":" + (str(int(err.get("time", 0))) if p["kind"] == "error" else "")
        current.add(p["kind"])
        last = notified.get(p["kind"], {})
        if last.get("key") == key and now - last.get("time", 0) < repeat[p["kind"]]:
            continue
        desktop_notify("netshift: " + p["title"], p["body"], 2 if p["level"] == "error" else 1)
        notified[p["kind"]] = {"key": key, "time": now}
    # Entwarnung, wenn ein gemeldetes Problem behoben ist
    solved = [k for k in list(notified) if k in ("error", "overdue") and k not in current]
    if solved:
        desktop_notify("netshift: Sicherung läuft wieder",
                       "Die letzte Sicherung war erfolgreich." if st.get("last_success") else "Problem behoben.")
    for k in list(notified):
        if k not in current:
            del notified[k]


class Lock:
    def __init__(self, wait=False):
        os.makedirs(RUN_DIR, mode=0o755, exist_ok=True)
        self.f = open(os.path.join(RUN_DIR, "lock"), "w")
        flags = fcntl.LOCK_EX | (0 if wait else fcntl.LOCK_NB)
        try:
            fcntl.flock(self.f, flags)
        except BlockingIOError:
            self.f.close()
            die("Ein anderer netshift-Vorgang läuft bereits (z.B. ein geplanter Snapshot). "
                "Bitte kurz warten und erneut versuchen.")

    def release(self):
        try:
            fcntl.flock(self.f, fcntl.LOCK_UN)
            self.f.close()
        except Exception:
            pass


# --------------------------------------------------------------------------- Speicher

class LocalStorage:
    """Snapshots liegen in einem lokal erreichbaren Verzeichnis (auch gemountete Images)."""
    remote = False

    def __init__(self, base):
        self.base = base
        self.snapdir = os.path.join(base, "snapshots")

    def describe(self):
        return self.base

    def prepare(self):
        os.makedirs(self.snapdir, mode=0o755, exist_ok=True)

    def abs(self, name):
        return os.path.join(self.snapdir, name)

    def loc(self, name, sub="localhost/"):
        return os.path.join(self.snapdir, name, sub)

    def rsync_opts(self):
        return []

    def mkdirs(self, rel):
        os.makedirs(os.path.join(self.snapdir, rel), mode=0o755, exist_ok=True)

    def exists(self, name):
        return os.path.isdir(self.abs(name))

    def list_raw(self):
        """{name: info-dict or None}"""
        out = {}
        if not os.path.isdir(self.snapdir):
            return out
        for n in os.listdir(self.snapdir):
            p = os.path.join(self.snapdir, n)
            if not os.path.isdir(p):
                continue
            inf = None
            ip = os.path.join(p, "info.json")
            if os.path.exists(ip):
                try:
                    with open(ip, encoding="utf-8") as f:
                        inf = json.load(f)
                except Exception:
                    inf = None
            out[n] = inf
        return out

    def write_info(self, name, inf):
        p = os.path.join(self.abs(name), "info.json")
        with open(p + ".tmp", "w", encoding="utf-8") as f:
            json.dump(inf, f, indent=2, ensure_ascii=False)
        os.replace(p + ".tmp", p)

    def rename(self, a, b):
        os.rename(self.abs(a), self.abs(b))

    def delete(self, name):
        p = self.abs(name)
        if os.path.isdir(p):
            run(["rm", "-rf", "--one-file-system", p])

    def free(self):
        os.makedirs(self.base, exist_ok=True)
        st = os.statvfs(self.base)
        return st.f_bavail * st.f_frsize, st.f_blocks * st.f_frsize


class SshStorage:
    """Snapshots liegen auf einem Linux-Server, erreichbar per SSH."""
    remote = True

    def __init__(self, t, label):
        need("ssh", "openssh")
        self.t = t
        self.host = f"{t['ssh_user']}@{t['server']}" if t.get("ssh_user") else t["server"]
        self.base = os.path.join(t["ssh_path"].rstrip("/") or "/", label)
        self.snapdir = self.base + "/snapshots"
        self.ssh = ["ssh", "-p", str(t.get("ssh_port") or 22), "-o", "BatchMode=yes",
                    "-o", "StrictHostKeyChecking=accept-new", "-o", "ServerAliveInterval=30"]
        key = t.get("ssh_key")
        if key and os.path.exists(key):
            self.ssh += ["-i", key]

    def describe(self):
        return f"{self.host}:{self.base}"

    def sh(self, script, input=None, check=True):
        p = run(self.ssh + [self.host, script], capture=True, input=input, check=False, quiet=True)
        if check and p.returncode != 0:
            die(f"SSH-Befehl auf {self.host} fehlgeschlagen: {p.stderr.strip() or p.returncode}")
        return p

    def prepare(self):
        self.sh(f"mkdir -p {shlex.quote(self.snapdir)} && command -v rsync >/dev/null || "
                "{ echo 'rsync fehlt auf dem Server' >&2; exit 3; }")

    def abs(self, name):
        return f"{self.snapdir}/{name}"

    def loc(self, name, sub="localhost/"):
        return f"{self.host}:{self.snapdir}/{name}/{sub}"

    def rsync_opts(self):
        o = ["-e", " ".join(shlex.quote(c) for c in self.ssh)]
        if self.t.get("ssh_fake_super"):
            o.append("--rsync-path=rsync --fake-super")
        return o

    def mkdirs(self, rel):
        self.sh(f"mkdir -p {shlex.quote(self.snapdir + '/' + rel)}")

    def exists(self, name):
        return self.sh(f"test -d {shlex.quote(self.abs(name))}", check=False).returncode == 0

    def list_raw(self):
        q = shlex.quote(self.snapdir)
        script = (f"cd {q} 2>/dev/null || exit 0; for d in * .in-progress; do [ -d \"$d\" ] || continue; "
                  "echo \"@@@NETSHIFT@@@$d\"; [ -f \"$d/info.json\" ] && cat \"$d/info.json\"; echo; done")
        out = {}
        cur, buf = None, []
        for line in self.sh(script).stdout.splitlines():
            if line.startswith("@@@NETSHIFT@@@"):
                if cur is not None:
                    out[cur] = _parse_json("\n".join(buf))
                cur, buf = line[len("@@@NETSHIFT@@@"):], []
            else:
                buf.append(line)
        if cur is not None:
            out[cur] = _parse_json("\n".join(buf))
        return out

    def write_info(self, name, inf):
        p = shlex.quote(self.abs(name) + "/info.json")
        self.sh(f"cat > {p}.tmp && mv {p}.tmp {p}", input=json.dumps(inf, indent=2, ensure_ascii=False))

    def rename(self, a, b):
        self.sh(f"mv {shlex.quote(self.abs(a))} {shlex.quote(self.abs(b))}")

    def delete(self, name):
        self.sh(f"rm -rf {shlex.quote(self.abs(name))}")

    def free(self):
        out = self.sh(f"df -Pk {shlex.quote(self.base)} | tail -1").stdout.split()
        try:
            return int(out[3]) * 1024, int(out[1]) * 1024
        except (IndexError, ValueError):
            return 0, 0


def _parse_json(s):
    s = s.strip()
    if not s:
        return None
    try:
        return json.loads(s)
    except Exception:
        return None


# --------------------------------------------------------------------------- Ziel (Mounts)

class Target:
    """Bindet das Sicherungsziel ein und liefert ein Storage-Objekt."""

    def __init__(self, cfg):
        self.cfg = cfg
        self.t = cfg["target"]
        self.label = safe_name(cfg.get("machine_name") or hostname())
        self.share_mp = os.path.join(RUN_DIR, "share")
        self.image_mp = os.path.join(RUN_DIR, "image")
        self.mapper = "netshift-" + self.label
        self.mounted_share = False
        self.mounted_image = False
        self.opened_crypt = False
        self.storage = None
        self.mode = None
        self.image_path = None
        self.share_root = None
        self.rescue = bool(cfg.get("rescue_mode"))

    # ---- Beschreibung
    def describe(self):
        t = self.t
        typ = t["type"]
        if typ == "smb":
            return f"SMB //{t['server']}/{t['share']}"
        if typ == "nfs":
            return f"NFS {t['server']}:{t['export']}"
        if typ == "ssh":
            return f"SSH {t.get('ssh_user') or ''}@{t['server']}:{t['ssh_path']}"
        return f"Lokal {t['path']}"

    def validate(self):
        t = self.t
        typ = t.get("type")
        if typ == "local" and not t.get("path"):
            die("Kein Sicherungsziel konfiguriert (Pfad fehlt). 'sudo netshift setup' ausführen.")
        if typ == "smb" and not (t.get("server") and t.get("share")):
            die("SMB-Ziel unvollständig (Server/Freigabe). 'sudo netshift setup' ausführen.")
        if typ == "nfs" and not (t.get("server") and t.get("export")):
            die("NFS-Ziel unvollständig (Server/Export). 'sudo netshift setup' ausführen.")
        if typ == "ssh" and not (t.get("server") and t.get("ssh_path")):
            die("SSH-Ziel unvollständig (Server/Pfad). 'sudo netshift setup' ausführen.")
        if typ not in ("local", "smb", "nfs", "ssh"):
            die(f"Unbekannter Zieltyp: {typ}")

    # ---- Öffnen
    def open(self):
        global NET_PROBE
        self.validate()
        os.makedirs(RUN_DIR, mode=0o755, exist_ok=True)
        typ = self.t["type"]
        NET_PROBE = target_probe(self.t)
        if os.environ.get("NETSHIFT_TEST_PROBE"):            # nur für Tests
            h, _, port = os.environ["NETSHIFT_TEST_PROBE"].rpartition(":")
            NET_PROBE = (h, int(port))
        if NET_PROBE and not reachable(NET_PROBE):
            die(offline_msg(NET_PROBE), kind="offline")
        try:
            if typ == "ssh":
                self.mode = "ssh"
                self.storage = SshStorage(self.t, self.label)
                self.storage.prepare()
                return self.storage

            share = self._mount_share()
            self.share_root = os.path.join(share, self.t.get("subdir") or "netshift")
            self.mode = self._container_mode(share)
            if self.mode == "direct":
                self.storage = LocalStorage(os.path.join(self.share_root, self.label))
            else:
                self._open_image()
                self.storage = LocalStorage(self.image_mp)
            self.storage.prepare()
            return self.storage
        except BaseException:
            self.close(force=True)
            raise

    def _mount(self, cmd, mp):
        os.makedirs(mp, mode=0o700, exist_ok=True)
        if os.path.ismount(mp):
            return False
        p = run(cmd + [mp], check=False, capture=True)
        if p.returncode != 0:
            die(f"Einbinden fehlgeschlagen: {' '.join(cmd)} {mp}\n{p.stderr.strip()}")
        return True

    def _mount_share(self):
        t = self.t
        typ = t["type"]
        extra = t.get("mount_options", "").strip().strip(",")
        if typ == "local":
            p = t["path"]
            if p.startswith("/dev/") or re.match(r"^(UUID|LABEL|PARTUUID|PARTLABEL)=", p):
                cmd = ["mount"] + (["-o", extra] if extra else []) + [p]
                self.mounted_share = self._mount(cmd, self.share_mp) or os.path.ismount(self.share_mp)
                return self.share_mp
            if not os.path.isdir(p):
                die(f"Zielverzeichnis existiert nicht: {p} (Laufwerk eingesteckt/eingebunden?)")
            return p
        if typ == "smb":
            need("mount.cifs", "cifs-utils")
            cred = cred_path()
            opts = ["uid=0", "gid=0", "file_mode=0600", "dir_mode=0700", "iocharset=utf8"]
            if t.get("smb_version"):
                opts.append("vers=" + str(t["smb_version"]))
            if os.path.exists(cred):
                opts.append("credentials=" + cred)
            else:
                opts.append("guest")
            if extra:
                opts.append(extra)
            src = f"//{t['server']}/{t['share'].strip('/')}"
            cmd = ["mount", "-t", "cifs", src, "-o", ",".join(opts)]
        else:  # nfs
            need("mount.nfs", "nfs-utils")
            src = f"{t['server']}:{t['export']}"
            cmd = ["mount", "-t", "nfs", src] + (["-o", extra] if extra else [])
        self.mounted_share = self._mount(cmd, self.share_mp) or os.path.ismount(self.share_mp)
        return self.share_mp

    def _container_mode(self, share):
        c = self.t.get("container", "auto")
        typ = self.t["type"]
        if typ == "smb" and c == "direct":
            die("SMB kann Linux-Besitzer/Rechte nicht zuverlässig speichern – "
                "für SMB ist nur der Image-Container möglich (container: image/auto).")
        if c in ("image", "direct"):
            return c
        if typ in ("smb", "nfs"):
            return "image"
        if fs_type_of(share) in NON_POSIX_FS:
            return "image"
        return "direct"

    def _open_image(self):
        t = self.t
        os.makedirs(self.share_root, mode=0o700, exist_ok=True)
        self.image_path = os.path.join(self.share_root, self.label + ".img")
        os.makedirs(self.image_mp, mode=0o755, exist_ok=True)
        if os.path.ismount(self.image_mp):
            return  # bereits eingebunden (z.B. durch 'netshift mount')
        need("mkfs.ext4", "e2fsprogs")
        encrypt = bool(t.get("encrypt"))
        mapper_dev = "/dev/mapper/" + self.mapper
        if not os.path.exists(self.image_path) and self.rescue:
            others = sorted(f[:-4] for f in os.listdir(self.share_root) if f.endswith(".img"))
            die(f"Auf dem Ziel gibt es keine Sicherung für den Rechner '{self.label}'."
                + (f"\nVorhandene Rechner: {', '.join(others)}\nBitte in den Einstellungen unter "
                   "„Name im Backup“ genau diesen Namen eintragen." if others
                   else "\nIm Ordner netshift auf der Freigabe liegt keine Sicherung."))
        if not os.path.exists(self.image_path):
            self._create_image(encrypt)
        dev = self.image_path
        if encrypt:
            need("cryptsetup")
            if not os.path.exists(mapper_dev):
                key = t.get("key_file")
                if not key or not os.path.exists(key):
                    die(f"Schlüsseldatei fehlt: {key}. Ohne sie ist das verschlüsselte Image nicht lesbar.")
                run(["cryptsetup", "open", "--type", "luks", "--key-file", key,
                     self.image_path, self.mapper])
                self.opened_crypt = True
            dev = mapper_dev
        # Dateisystemprüfung (schnell, wenn sauber)
        p = run(["e2fsck", "-p", dev], check=False, capture=True)
        if p.returncode >= 4:
            die(f"Das Image-Dateisystem ist beschädigt (e2fsck {p.returncode}). "
                f"Manuell prüfen: e2fsck -f {dev}\n{p.stderr.strip()}")
        opts = "noatime" + ("" if encrypt else ",loop")
        run(["mount", "-o", opts, dev, self.image_mp])
        self.mounted_image = True

    def _create_image(self, encrypt):
        t = self.t
        size = int(t.get("image_size_gb") or 100)
        info(f"Erstelle Image-Container {self.image_path} ({size} GiB, wächst bei Bedarf "
             f"bis zu dieser Größe – sparse)…")
        tmp = self.image_path + ".creating"
        with open(tmp, "wb") as f:
            f.truncate(size * 1024 ** 3)
        try:
            self._format_image(tmp, encrypt)
        except BaseException:
            if self.opened_crypt:
                run(["cryptsetup", "close", self.mapper], check=False, capture=True)
                self.opened_crypt = False
            os.unlink(tmp)
            raise
        os.rename(tmp, self.image_path)

    def _format_image(self, img, encrypt):
        t = self.t
        dev = img
        if encrypt:
            need("cryptsetup")
            key = t.get("key_file")
            if not os.path.exists(key):
                os.makedirs(os.path.dirname(key), mode=0o700, exist_ok=True)
                fd = os.open(key, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o400)
                with os.fdopen(fd, "wb") as f:
                    f.write(secrets.token_bytes(64))
                warn(f"Neuer Schlüssel erzeugt: {key}\n"
                     "         UNBEDINGT SICHERN (z.B. USB-Stick/Passwortmanager)! "
                     "Ohne diese Datei ist eine Wiederherstellung von einem Live-System unmöglich.")
            run(["cryptsetup", "luksFormat", "--batch-mode", "--type", "luks2",
                 "--key-file", key, img])
            run(["cryptsetup", "open", "--type", "luks", "--key-file", key, img, self.mapper])
            self.opened_crypt = True
            dev = "/dev/mapper/" + self.mapper
        run(["mkfs.ext4", "-q", "-F", "-m", "0", "-L", "netshift", dev])

    # ---- Schließen
    def close(self, force=False):
        if not force and os.path.exists(os.path.join(RUN_DIR, "keep-mounted")):
            return
        if self.mounted_image or force:
            _umount(self.image_mp)
        if self.opened_crypt or force:
            if os.path.exists("/dev/mapper/" + self.mapper):
                run(["cryptsetup", "close", self.mapper], check=False, capture=True)
        if self.mounted_share or force:
            _umount(self.share_mp)


def _umount(mp, fast=False):
    """Aushängen – hängt nie: bei toter Verbindung wird verzögert (lazy) ausgehängt."""
    if not os.path.ismount(mp):
        return
    if not fast:
        run(["sync", "-f", mp], check=False, capture=True, quiet=True, timeout=20)
        for _ in range(3):
            if run(["umount", mp], check=False, capture=True, timeout=20).returncode == 0:
                return
            time.sleep(1)
        warn(f"{mp} lässt sich nicht normal aushängen – wird verzögert ausgehängt.")
    run(["umount", "-l", mp], check=False, capture=True, timeout=10)


def unmount_all(cfg, fast=False):
    try:
        os.remove(os.path.join(RUN_DIR, "keep-mounted"))
    except FileNotFoundError:
        pass
    t = Target(cfg)
    if fast:
        _umount(t.image_mp, fast=True)
        if os.path.exists("/dev/mapper/" + t.mapper):
            run(["cryptsetup", "close", t.mapper], check=False, capture=True, timeout=10)
        if os.path.ismount(t.share_mp):
            run(["umount", "-f", "-l", t.share_mp], check=False, capture=True, timeout=10)
        return
    t.close(force=True)


# --------------------------------------------------------------------------- Snapshots

def snapshots(storage):
    """Sortierte Liste fertiger Snapshots (alt → neu) als dicts."""
    res = []
    for name, inf in storage.list_raw().items():
        if not SNAP_RE.match(name):
            continue
        inf = dict(inf or {})
        inf.setdefault("name", name)
        inf["name"] = name
        inf.setdefault("tags", [])
        inf.setdefault("comment", "")
        if "epoch" not in inf:
            try:
                inf["epoch"] = dt.datetime.strptime(name, TIME_FMT).timestamp()
            except ValueError:
                inf["epoch"] = 0
        inf["valid"] = inf.get("status") == "ok" or inf.get("status") == "warnings"
        res.append(inf)
    res.sort(key=lambda s: s["name"])
    return res


def resolve(storage, ident):
    snaps = snapshots(storage)
    if not snaps:
        die("Keine Snapshots vorhanden.")
    if ident in ("latest", "last", "neuester"):
        return snaps[-1]
    if ident.isdigit() and int(ident) >= 1 and int(ident) <= len(snaps) and not SNAP_RE.match(ident):
        return snaps[int(ident) - 1]
    for s in snaps:
        if s["name"] == ident:
            return s
    matches = [s for s in snaps if s["name"].startswith(ident)]
    if len(matches) == 1:
        return matches[0]
    die(f"Snapshot '{ident}' nicht gefunden. 'netshift list' zeigt alle.")


def build_excludes(cfg, storage=None, target=None):
    ex = list(FORCED_EXCLUDES)
    ex += cfg.get("exclude", [])
    if not cfg.get("include_home"):
        ex += HOME_EXCLUDES
    if cfg.get("exclude_pacman_cache", True):
        ex += PACMAN_CACHE_EXCLUDES
    if SOURCE == "/":
        # Netzwerk- und flüchtige Dateisysteme automatisch ausschließen (Mountpunkt bleibt erhalten)
        for mp, fstype in mountinfo():
            if mp == "/" or any(mp.startswith(p) for p in ("/proc", "/sys", "/dev", "/run", "/tmp")):
                continue
            if fstype in NETWORK_FS or fstype in VOLATILE_FS or fstype.startswith("fuse."):
                ex.append(rsync_escape(mp.rstrip("/")) + "/*")
        # Lokales Ziel innerhalb des Systems ausschließen
        if target is not None and target.t["type"] == "local" and not target.mounted_share:
            real = os.path.realpath(target.t["path"])
            if real != "/":
                ex.append(rsync_escape(real.rstrip("/")) + "/*")
    seen, out = set(), []
    for e in ex:
        e = e.strip()
        if e and e not in seen:
            seen.add(e)
            out.append(e)
    return out


def write_exclude_file(patterns):
    os.makedirs(RUN_DIR, exist_ok=True)
    fd, path = tempfile.mkstemp(prefix="exclude-", dir=RUN_DIR, text=True)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write("\n".join(patterns) + "\n")
    return path


PROGRESS_RE = re.compile(r"^\s*([\d.,]+\s*[KMGTP]?i?B?)\s+(\d{1,3})%\s+(\S+)\s+(\d+:\d{2}:\d{2})")


def _net_watch(p, stop, state):
    """Prüft alle 5 s, ob das Netzwerkziel erreichbar ist; bricht rsync nach NET_TIMEOUT ab."""
    down = None
    while not stop.wait(5):
        if reachable(NET_PROBE, timeout=4):
            if down is not None:
                print("@@PHASE Verbindung wieder da – geht weiter…" if GUI_MODE
                      else "Verbindung zum Sicherungsziel wieder da.", flush=True)
            down = None
            continue
        down = down or time.time() - 5
        gone = int(time.time() - down)
        if GUI_MODE:
            print(f"@@PHASE Verbindung zum Sicherungsziel unterbrochen – warte… ({gone} s)", flush=True)
        elif gone < 10:
            print("\nWARNUNG: Verbindung zum Sicherungsziel unterbrochen – warte…", flush=True)
        if gone >= NET_TIMEOUT:
            state["lost"] = gone
            p.terminate()
            try:
                p.wait(10)
            except subprocess.TimeoutExpired:
                p.kill()
            return


def run_rsync(cmd, lost_hint=""):
    """rsync ausführen (im GUI-Modus mit maschinenlesbarem Fortschritt) und die Verbindung überwachen."""
    if os.environ.get("NETSHIFT_DEBUG"):
        print("+ " + " ".join(shlex.quote(c) for c in cmd), flush=True)
    if GUI_MODE:
        print("@@PHASE Dateiliste wird erstellt…", flush=True)
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE if GUI_MODE else None,
                         stderr=subprocess.STDOUT if GUI_MODE else None)
    stop, state = threading.Event(), {}
    if NET_PROBE:
        threading.Thread(target=_net_watch, args=(p, stop, state), daemon=True).start()
    try:
        rc = _rsync_output(p) if GUI_MODE else p.wait()
    except KeyboardInterrupt:
        p.terminate()
        raise
    finally:
        stop.set()
    if state.get("lost"):
        die(f"Die Verbindung zum Sicherungsziel ist abgebrochen (seit {state['lost']} s nicht erreichbar)."
            + (" " + lost_hint if lost_hint else ""), kind="offline")
    return rc


def _rsync_output(p):
    buf = b""
    last = 0.0
    best = 0
    while True:
        chunk = p.stdout.read1(4096)
        if not chunk:
            break
        for ch in chunk:
            ch = bytes([ch])
            if ch not in (b"\r", b"\n"):
                buf += ch
                continue
            line = buf.decode("utf-8", "replace")
            buf = b""
            m = PROGRESS_RE.match(line)
            if m:
                best = max(best, int(m.group(2)))   # Anzeige läuft nie rückwärts
                now = time.time()
                if now - last > 0.3:
                    last = now
                    print(f"@@PROGRESS {best} {m.group(3)} {m.group(4)} {m.group(1).strip()}", flush=True)
            elif line.strip():
                print(line, flush=True)
    rc = p.wait()
    if rc in (0, 24):
        print("@@PROGRESS 100 - 0:00:00 -", flush=True)
    return rc


def rsync_base(cfg):
    # --no-inc-recursive: komplette Dateiliste vorab erstellen, damit die
    # Prozentangabe von Anfang an auf der echten Gesamtmenge beruht.
    cmd = ["rsync", "-aAXH", "--numeric-ids", "--info=progress2", "--human-readable",
           "--no-inc-recursive"]
    cmd += [str(a) for a in cfg.get("rsync_extra_args", [])]
    return cmd


def system_info():
    pretty = ""
    try:
        with open(os.path.join(SOURCE, "etc/os-release")) as f:
            for line in f:
                if line.startswith("PRETTY_NAME="):
                    pretty = line.split("=", 1)[1].strip().strip('"')
    except OSError:
        pass
    return {"os": pretty, "kernel": os.uname().release, "host": hostname()}


def foreign_host(storage):
    """Rechnername des neuesten Snapshots, falls er nicht dieser Rechner ist."""
    snaps = [s for s in snapshots(storage) if s["valid"] and s.get("host")]
    if snaps and snaps[-1]["host"] != hostname():
        return snaps[-1]["host"]
    return None


def create_snapshot(cfg, target, storage, tags, comment=""):
    need("rsync")
    if cfg.get("rescue_mode"):
        die("Rettungssystem: Auf diesem System werden keine Snapshots erstellt – sonst würde ein Abbild "
            "des Rettungssystems zwischen den Sicherungen des eigentlichen Rechners landen.")
    other = foreign_host(storage)
    if other and not cfg.get("_allow_other_host"):
        die(f"Die Snapshots in dieser Sicherung stammen vom Rechner '{other}', dieser Rechner heißt "
            f"'{hostname()}'. Damit nicht versehentlich ein fremdes System (z.B. ein Rettungs-Stick) "
            "zwischen den Sicherungen landet, wurde abgebrochen.\n"
            "Falls du diesen Rechner nur umbenannt hast: sudo netshift create --allow-other-host")
    free, total = storage.free()
    need_free = space_need(cfg, total)
    if total and free < need_free and cfg.get("auto_cleanup", True):
        cleanup_for_space(cfg, storage, need_free)
        free, total = storage.free()
    if total and free < need_free:
        die(f"Zu wenig freier Speicher auf dem Sicherungsziel ({human(free)} frei, benötigt mindestens "
            f"{human(need_free)}). "
            + ("Image vergrößern (netshift → Hinweis „Vergrößern“ oder 'sudo netshift resize-image GRÖSSE') "
               "oder alte Snapshots löschen." if target.mode == "image" else "Alte Snapshots löschen."),
            kind="space")
    snaps = [s for s in snapshots(storage) if s["valid"]]
    prev = snaps[-1] if snaps else None
    name = dt.datetime.now().strftime(TIME_FMT)
    while storage.exists(name):
        time.sleep(1)
        name = dt.datetime.now().strftime(TIME_FMT)

    storage.mkdirs(INPROGRESS + "/localhost")
    patterns = build_excludes(cfg, storage, target)
    exfile = write_exclude_file(patterns)
    started = time.time()
    info(f"Erstelle Snapshot {name} [{','.join(tags)}] → {target.describe()}"
         + (f" (inkrementell zu {prev['name']})" if prev else " (erster, vollständiger Snapshot)"))
    try:
        cmd = rsync_base(cfg) + ["--delete", "--delete-excluded", "--exclude-from", exfile, "--stats"]
        cmd += storage.rsync_opts()
        if prev:
            cmd.append("--link-dest=" + storage.abs(prev["name"]) + "/localhost")
        cmd += [SOURCE.rstrip("/") + "/", storage.loc(INPROGRESS)]
        rc = run_rsync(cmd, "Der angefangene Snapshot wird beim nächsten Mal fortgesetzt.")
    finally:
        os.unlink(exfile)
    if rc not in (0, 24):
        f2, t2 = storage.free()
        if t2 and f2 < max(0.01 * t2, 256 * 1024 ** 2):
            die("Das Sicherungsziel ist voll – der Snapshot konnte nicht fertiggestellt werden. "
                + ("Bitte das Image vergrößern oder alte Snapshots löschen." if target.mode == "image"
                   else "Bitte alte Snapshots löschen."), kind="space")
    if rc not in (0, 23, 24):
        die(f"rsync ist mit Fehler {rc} abgebrochen. Der unvollständige Snapshot bleibt als "
            f"'{INPROGRESS}' liegen und wird beim nächsten Lauf fortgesetzt.")
    if rc == 23:
        warn("Einige Dateien konnten nicht gesichert werden (rsync 23) – Details siehe Ausgabe oben.")
    inf = {
        "name": name,
        "created": dt.datetime.now().isoformat(timespec="seconds"),
        "epoch": time.time(),
        "tags": sorted(set(tags), key=lambda x: "OBHDWMPR".find(x)),
        "comment": comment,
        "status": "ok" if rc in (0, 24) else "warnings",
        "rsync_exit": rc,
        "duration_s": round(time.time() - started, 1),
        "link_dest": prev["name"] if prev else None,
        "include_home": bool(cfg.get("include_home")),
        "exclude_used": patterns,
        "netshift_version": VERSION,
        **system_info(),
    }
    storage.write_info(INPROGRESS, inf)
    storage.rename(INPROGRESS, name)
    info(f"Snapshot {name} erfolgreich erstellt ({inf['duration_s']} s).")
    return inf


def _freeable(storage, names):
    """Wie viel Platz würde frei, wenn ALLE genannten Snapshots gelöscht würden?
    (Dateien zählen nur, wenn keine anderen Snapshots per Hardlink darauf verweisen.)"""
    seen, total = {}, 0
    for n in names:
        stack = [storage.abs(n)]
        while stack:
            d = stack.pop()
            try:
                it = os.scandir(d)
            except OSError:
                continue
            with it:
                for e in it:
                    try:
                        st = e.stat(follow_symlinks=False)
                    except OSError:
                        continue
                    if e.is_dir(follow_symlinks=False):
                        total += st.st_blocks * 512
                        stack.append(e.path)
                    else:
                        rec = seen.setdefault(st.st_ino, [st.st_blocks * 512, 0, st.st_nlink])
                        rec[1] += 1
    return total + sum(b for b, here, nlink in seen.values() if here >= nlink)


def cleanup_for_space(cfg, storage, need_free):
    """Älteste automatische Snapshots löschen, bis genug Platz frei ist – aber nur, wenn das
    überhaupt reicht. Manuelle Snapshots, Sicherheits-Snapshots und der neueste bleiben immer."""
    snaps = [x for x in snapshots(storage) if x["valid"]]
    candidates = [x for x in snaps[:-1] if x["tags"] and set(x["tags"]) <= AUTO_TAGS]
    deleted = []
    if not candidates:
        return deleted
    if storage.remote:
        info("Platzmangel: Bei SSH-Zielen werden Snapshots nicht automatisch gelöscht.")
        return deleted
    free, _ = storage.free()
    info(f"Platzmangel ({human(free)} frei) – prüfe, ob das Löschen alter automatischer Snapshots reicht…")
    possible = _freeable(storage, [x["name"] for x in candidates])
    if free + possible < need_free:
        info(f"Das Löschen aller {len(candidates)} automatischen Snapshots würde nur {human(possible)} "
             "freigeben und nicht reichen – es wird nichts gelöscht.")
        return deleted
    for x in candidates:
        free, _ = storage.free()
        if free >= need_free:
            break
        info(f"Platzmangel: lösche ältesten automatischen Snapshot {x['name']} ({','.join(x['tags'])})")
        storage.delete(x["name"])
        deleted.append(x["name"])
    if deleted:
        info(f"{len(deleted)} automatische(r) Snapshot(s) gelöscht, um Platz zu schaffen.")
    return deleted


def prune(cfg, storage, extra_keep=None):
    """Aufbewahrungsregeln anwenden: pro Tag nur N neueste behalten."""
    keep = {}
    for lvl, tag in RETENTION_TAGS.items():
        keep[tag] = int(cfg["schedule"].get(lvl, {}).get("keep", 0))
    keep["P"] = int(cfg.get("pacman_hook", {}).get("keep", 5))
    if extra_keep:
        keep.update(extra_keep)
    snaps = [s for s in snapshots(storage) if s["valid"]]
    changed = {}
    for tag, n in keep.items():
        tagged = [s for s in snaps if tag in s["tags"]]
        tagged.sort(key=lambda s: s["name"], reverse=True)
        for s in tagged[max(n, 0):]:
            s["tags"] = [x for x in s["tags"] if x != tag]
            changed[s["name"]] = s
    deleted = []
    for name, s in changed.items():
        if not s["tags"]:
            info(f"Lösche abgelaufenen Snapshot {name}")
            storage.delete(name)
            deleted.append(name)
        else:
            raw = {k: v for k, v in s.items() if k != "valid"}
            storage.write_info(name, raw)
    return deleted


# --------------------------------------------------------------------------- Restore

# --------------------------------------------------------------------------- Verschlüsselte Partitionen

def real_device(dev):
    """UUID=…/LABEL=… in einen Gerätepfad auflösen."""
    if re.match(r"^(UUID|LABEL|PARTUUID|PARTLABEL)=", dev):
        p = run(["findfs", dev], check=False, capture=True, quiet=True)
        if p.returncode == 0 and p.stdout.strip():
            return p.stdout.strip()
    return dev


def is_luks(dev):
    dev = real_device(dev)
    if shutil.which("cryptsetup"):
        return run(["cryptsetup", "isLuks", dev], check=False, capture=True, quiet=True).returncode == 0
    p = run(["blkid", "-o", "value", "-s", "TYPE", dev], check=False, capture=True, quiet=True)
    return (p.stdout or "").strip() == "crypto_LUKS"


def luks_children(dev):
    """Bereits entsperrte Zuordnungen (/dev/mapper/…) einer LUKS-Partition."""
    p = run(["lsblk", "-J", "-o", "PATH,TYPE", real_device(dev)], check=False, capture=True, quiet=True)
    try:
        tree = json.loads(p.stdout or "{}").get("blockdevices", [])
    except ValueError:
        return []
    out = []
    for d in tree:
        for c in d.get("children") or []:
            if c.get("type") == "crypt" and c.get("path"):
                out.append(c["path"])
    return out


def mapper_name(dev):
    return "netshift-" + safe_name(os.path.basename(real_device(dev)))


def unlock_device(dev, passphrase):
    """Entsperrt eine LUKS-Partition und liefert den Pfad der entsperrten Partition."""
    need("cryptsetup")
    dev = real_device(dev)
    if not is_luks(dev):
        die(f"{dev} ist nicht verschlüsselt.")
    kids = luks_children(dev)
    if kids:
        return kids[0]
    name = mapper_name(dev)
    path = "/dev/mapper/" + name
    if os.path.exists(path):
        return path
    p = subprocess.run(["cryptsetup", "open", "--type", "luks", "--key-file=-", dev, name],
                       input=passphrase.encode() if isinstance(passphrase, str) else passphrase,
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if p.returncode == 2:
        die("Falsches Passwort – bitte erneut versuchen.")
    if p.returncode != 0:
        die(f"Entsperren fehlgeschlagen: {p.stderr.decode(errors='replace').strip() or p.returncode}")
    for _ in range(20):               # auf udev warten
        if os.path.exists(path):
            break
        time.sleep(0.1)
    return path


def resolve_restore_device(dev):
    """Verschlüsselte Partition → entsperrte Variante (vorhandene nutzen oder nach Passwort fragen)."""
    if not is_luks(dev):
        return dev
    kids = luks_children(dev)
    if kids:
        info(f"{dev} ist verschlüsselt und bereits entsperrt – verwende {kids[0]}.")
        return kids[0]
    if os.path.exists("/dev/mapper/" + mapper_name(dev)):
        return "/dev/mapper/" + mapper_name(dev)
    if not GUI_MODE and sys.stdin.isatty():
        import getpass
        for _ in range(3):
            pw = getpass.getpass(f"{dev} ist verschlüsselt. Passwort zum Entsperren: ")
            try:
                path = unlock_device(dev, pw)
                info(f"Entsperrt: {path}")
                return path
            except NetshiftError as e:
                print(e, file=sys.stderr)
        die("Entsperren fehlgeschlagen.")
    die(f"{dev} ist verschlüsselt. Bitte zuerst entsperren – in netshift einfach in der Laufwerksliste auf "
        f"die Partition mit dem Schloss klicken, oder im Terminal: sudo netshift unlock {dev}")


def cmd_unlock(cfg, args):
    if args.stdin:
        pw = sys.stdin.buffer.read().rstrip(b"\r\n")
    elif sys.stdin.isatty():
        import getpass
        pw = getpass.getpass(f"Passwort für {args.device}: ")
    else:
        die("Passwort fehlt (--stdin verwenden).")
    print(unlock_device(args.device, pw), flush=True)


def cmd_lock(cfg, args):
    for d in args.devices:
        name = os.path.basename(d) if d.startswith("/dev/mapper/") else mapper_name(d)
        if not name.startswith("netshift-"):
            die(f"{d} wurde nicht von netshift entsperrt – bitte selbst schließen.")
        if os.path.exists("/dev/mapper/" + name):
            p = run(["cryptsetup", "close", name], check=False, capture=True)
            if p.returncode != 0:
                warn(f"{name} konnte nicht geschlossen werden (noch eingebunden?)")
                continue
        info(f"Gesperrt: {name}")


def mount_restore_target(dev, readonly=False):
    p = run(["findmnt", "-rn", "-o", "TARGET", "--source", dev], check=False, capture=True, quiet=True)
    if "/" in (p.stdout or "").split():
        die(f"{dev} ist das gerade laufende System. Bitte die Partition der Festplatte des Rechners "
            "wählen, der wiederhergestellt werden soll.")
    mp = os.path.join(RUN_DIR, "target")
    os.makedirs(mp, mode=0o755, exist_ok=True)
    if os.path.ismount(mp):
        _umount(mp)
    ro = ["-o", "ro"] if readonly else []
    run(["mount"] + ro + [dev, mp])
    # Btrfs (Manjaro-Standard): Subvolume @ verwenden, falls vorhanden
    if fs_type_of(mp) == "btrfs" and os.path.isdir(os.path.join(mp, "@")) \
            and not os.path.isdir(os.path.join(mp, "etc")):
        _umount(mp)
        run(["mount", "-o", "subvol=@" + (",ro" if readonly else ""), dev, mp])
        info("Btrfs erkannt – Subvolume '@' als Wurzel eingebunden.")
    if not readonly and not os.path.isdir(os.path.join(mp, "etc")) and os.listdir(mp) \
            and set(os.listdir(mp)) - {"lost+found"}:
        _umount(mp)
        die(f"Auf {dev} liegt kein Linux-System (kein /etc), aber andere Daten. Zur Sicherheit "
            "abgebrochen – bitte die richtige Partition wählen (die Systempartition des Rechners).")
    return mp


def restore(cfg, target, storage, snap, dest="/", dry_run=False, keep_fstab=False, delete=True):
    need("rsync")
    live = os.path.realpath(dest) == "/"
    patterns = list(dict.fromkeys(
        (snap.get("exclude_used") or (FORCED_EXCLUDES + HOME_EXCLUDES))
        + (build_excludes(cfg, storage, target) if live else [])
        + RESTORE_PROTECT + (FSTAB_PROTECT if keep_fstab else [])
        # Auf ein anderes Laufwerk: Die EFI-Partition ist dort nicht eingehängt – nichts hineinschreiben
        + ([] if live else ["/boot/efi/*", "/efi/*"])))
    exfile = write_exclude_file(patterns)
    try:
        cmd = rsync_base(cfg) + ["--exclude-from", exfile]
        if delete:
            cmd.append("--delete")
        if dry_run:
            cmd = [c for c in cmd if not c.startswith("--info=")]
            cmd += ["--dry-run", "--itemize-changes", "--out-format=%i %n%L"]
        cmd += storage.rsync_opts()
        cmd += [storage.loc(snap["name"]), dest.rstrip("/") + "/"]
        rc = run_rsync(cmd, "" if dry_run else
                       "Das Ziel ist jetzt nur teilweise wiederhergestellt. Bitte bei stabiler Verbindung "
                       "(am besten per LAN-Kabel) erneut wiederherstellen – bereits übertragene Dateien "
                       "werden dabei übersprungen.")
    finally:
        os.unlink(exfile)
    if rc not in (0, 23, 24):
        die(f"Wiederherstellung fehlgeschlagen (rsync {rc}).")
    if rc == 23:
        warn("Einige Dateien konnten nicht wiederhergestellt werden (rsync 23).")
    return rc


def update_grub(root):
    info("Aktualisiere GRUB-Konfiguration im wiederhergestellten System…")
    chroot = shutil.which("arch-chroot") or shutil.which("manjaro-chroot")
    if chroot:
        run([chroot, root, "grub-mkconfig", "-o", "/boot/grub/grub.cfg"], check=False)
        return
    mounted = []
    try:
        for d in ("dev", "proc", "sys", "run"):
            mp = os.path.join(root, d)
            os.makedirs(mp, exist_ok=True)
            if run(["mount", "--rbind", "/" + d, mp], check=False, capture=True).returncode == 0:
                mounted.append(mp)
        run(["chroot", root, "grub-mkconfig", "-o", "/boot/grub/grub.cfg"], check=False)
    finally:
        for mp in reversed(mounted):
            run(["umount", "-R", mp], check=False, capture=True)


# --------------------------------------------------------------------------- Befehle

class Session:
    """Lock + Ziel öffnen/schließen als Kontext."""

    def __init__(self, cfg, wait=False):
        self.cfg = cfg
        self.wait = wait

    def __enter__(self):
        self.lock = Lock(self.wait)
        self.target = Target(self.cfg)
        try:
            self.storage = self.target.open()
        except BaseException:
            self.lock.release()
            raise
        return self

    def __exit__(self, *exc):
        try:
            self.target.close()
        finally:
            self.lock.release()


def confirm(prompt, args, strong=False):
    if getattr(args, "yes", False):
        return True
    if not sys.stdin.isatty():
        die("Bestätigung nötig – mit --yes ausführen.")
    if strong:
        a = input(prompt + " Zum Fortfahren 'JA' eingeben: ")
        return a.strip() == "JA"
    a = input(prompt + " [j/N] ")
    return a.strip().lower() in ("j", "ja", "y", "yes")


def cmd_create(cfg, args):
    tags = list(args.tags.upper()) if args.tags else ["O"]
    if args.allow_other_host:
        cfg["_allow_other_host"] = True
    st = load_status()
    try:
        with Session(cfg) as s:
            create_snapshot(cfg, s.target, s.storage, tags, args.comment or "")
            if args.prune:
                prune(cfg, s.storage)
            record_success(st)
            sync_status(s.storage, st)
    except NetshiftError as e:
        if "anderer netshift-Vorgang" not in str(e):
            record_failure(st, e, "manual")
        raise
    finally:
        notify_health(cfg, st)      # ggf. Entwarnung
        save_status(st)


def fmt_snap_row(i, s):
    tags = ",".join(s["tags"]) or "-"
    st = "" if s["valid"] else " (UNVOLLSTÄNDIG)"
    if s.get("status") == "warnings":
        st = " (Warnungen)"
    return f"{i:>3}  {s['name']}  {tags:<8} {s.get('os', '')[:24]:<24} {s.get('comment', '')}{st}"


def cmd_list(cfg, args):
    with Session(cfg) as s:
        snaps = snapshots(s.storage)
        raw = s.storage.list_raw()
        free, total = s.storage.free()
        st = sync_status(s.storage) if not cfg.get("rescue_mode") else {}
        if args.json:
            print(json.dumps({
                "target": s.target.describe(), "mode": s.target.mode,
                "free": free, "total": total, "in_progress": INPROGRESS in raw,
                "health": health(cfg, st), "snapshots": snaps}, ensure_ascii=False))
            return
        info(f"Ziel: {s.target.describe()}  [{s.target.mode}]  frei: {human(free)} von {human(total)}")
        if not snaps:
            info("Noch keine Snapshots vorhanden.")
        else:
            info("  Nr  Name                 Tags     System                   Kommentar")
            for i, sn in enumerate(snaps, 1):
                info(fmt_snap_row(i, sn))
        if INPROGRESS in raw:
            info("Hinweis: Ein unterbrochener Snapshot liegt vor und wird beim nächsten Lauf fortgesetzt.")


def cmd_delete(cfg, args):
    with Session(cfg) as s:
        targets = [resolve(s.storage, n) for n in args.snapshots]
        names = ", ".join(t["name"] for t in targets)
        if not confirm(f"Snapshots löschen: {names}?", args):
            info("Abgebrochen.")
            return
        for t in targets:
            info(f"Lösche {t['name']} …")
            s.storage.delete(t["name"])
        info("Fertig.")


def _size_key(storage):
    """Ändert sich, sobald Snapshots hinzukommen oder gelöscht werden."""
    import hashlib
    names = sorted(s["name"] for s in snapshots(storage))
    return hashlib.sha1("|".join(names).encode()).hexdigest()[:16]


def _measure_local(root):
    """Belegter Platz eines Snapshots. Ein Inode zählt als 'exklusiv', wenn alle seine
    Hardlinks innerhalb dieses Snapshots liegen – dieser Platz wird beim Löschen frei."""
    seen = {}            # inode -> [blöcke, links_im_snapshot, links_gesamt]
    total = exclusive = files = 0
    stack = [root]
    while stack:
        d = stack.pop()
        try:
            it = os.scandir(d)
        except OSError:
            continue
        with it:
            for e in it:
                try:
                    st = e.stat(follow_symlinks=False)
                except OSError:
                    continue
                b = st.st_blocks * 512
                if e.is_dir(follow_symlinks=False):
                    total += b          # Verzeichnisse gehören immer nur zu einem Snapshot
                    exclusive += b
                    stack.append(e.path)
                    continue
                files += 1
                rec = seen.get(st.st_ino)
                if rec is None:
                    seen[st.st_ino] = [b, 1, st.st_nlink]
                    total += b
                else:
                    rec[1] += 1
    for b, here, nlink in seen.values():
        if here >= nlink:
            exclusive += b
    return {"total": total, "exclusive": exclusive, "files": files}


def _measure_ssh(storage, root):
    q = shlex.quote(root)
    out = storage.sh(f"du -sk {q} | cut -f1; "
                     f"find {q} -xdev \\( -type d -o -links 1 \\) -printf '%b\\n' | awk '{{s+=$1}} END {{print s*512}}'; "
                     f"find {q} -xdev ! -type d | wc -l").stdout.split()
    return {"total": int(out[0]) * 1024, "exclusive": int(out[1] or 0), "files": int(out[2])}


def snapshot_size(storage, snap, refresh=False):
    key = _size_key(storage)
    cached = snap.get("size") or {}
    if not refresh and cached.get("key") == key:
        return cached
    root = storage.abs(snap["name"]) + "/localhost"
    res = _measure_ssh(storage, root) if storage.remote else _measure_local(root)
    res.update(key=key, measured=dt.datetime.now().isoformat(timespec="seconds"))
    snap["size"] = res
    storage.write_info(snap["name"], {k: v for k, v in snap.items() if k != "valid"})
    return res


def cmd_size(cfg, args):
    with Session(cfg) as s:
        targets = [resolve(s.storage, n) for n in args.snapshots] if args.snapshots \
            else [x for x in snapshots(s.storage) if x["valid"]]
        res = {}
        for sn in targets:
            if not args.json:
                info(f"Berechne {sn['name']} …")
            res[sn["name"]] = snapshot_size(s.storage, sn, refresh=args.refresh)
        if args.json:
            print(json.dumps(res))
            return
        info("Snapshot             Nur dieser   Gesamtumfang   Dateien")
        for name, r in res.items():
            info(f"{name}  {human(r['exclusive']):>11}  {human(r['total']):>13}  {r['files']:>8}")
        info("'Nur dieser' = Platz, der beim Löschen frei wird. Unveränderte Dateien teilen sich "
             "die Snapshots per Hardlink.")


def cmd_comment(cfg, args):
    with Session(cfg) as s:
        sn = resolve(s.storage, args.snapshot)
        sn["comment"] = args.text
        if args.tags is not None:
            sn["tags"] = list(dict.fromkeys(args.tags.upper()))
        s.storage.write_info(sn["name"], {k: v for k, v in sn.items() if k != "valid"})
        info("Gespeichert.")


def cmd_restore(cfg, args):
    if SOURCE != "/" and args.target in (None, "/"):
        die("Testmodus: --target angeben.")
    dest = args.target or "/"
    live = os.path.realpath(dest) == "/" and not dest.startswith("/dev/")
    restore_mp = None
    if live and cfg.get("rescue_mode"):
        die("Rettungssystem: Bitte als Ziel die Systempartition des Rechners wählen (z.B. /dev/nvme0n1p2), "
            "nicht das laufende System.")
    with Session(cfg) as s:
        snap = resolve(s.storage, args.snapshot)
        if not snap["valid"]:
            die(f"Snapshot {snap['name']} ist unvollständig und kann nicht wiederhergestellt werden.")
        if live and snap.get("host") and snap["host"] != hostname() and not args.allow_other_host:
            die(f"Dieser Snapshot stammt vom Rechner '{snap['host']}', du bist gerade auf '{hostname()}'. "
                "Zur Sicherheit wird er nicht auf das laufende System gespielt. Bitte als Ziel die "
                "Systempartition des Rechners wählen.")
        if dest.startswith("/dev/") or re.match(r"^(UUID|LABEL)=", dest):
            dest = resolve_restore_device(dest)
            restore_mp = mount_restore_target(dest, readonly=args.dry_run)
            dest_path = restore_mp
        else:
            dest_path = dest
            if not os.path.isdir(dest_path):
                die(f"Zielpfad existiert nicht: {dest_path}")
            content = set(os.listdir(dest_path)) - {"lost+found"}
            if not live and content and not os.path.isdir(os.path.join(dest_path, "etc")):
                die(f"In {dest_path} liegt kein Linux-System (kein /etc), aber andere Daten. Eine "
                    "Wiederherstellung würde diese Daten löschen – zur Sicherheit abgebrochen. Bitte die "
                    "Wurzel des Zielsystems oder einen leeren Ordner wählen.")
        what = "das LAUFENDE SYSTEM" if live else dest
        try:
            if args.dry_run:
                info(f"Vorschau: Änderungen, die eine Wiederherstellung von {snap['name']} auf {what} "
                     "vornehmen würde (Legende: > geändert/neu, *deleting = wird gelöscht):")
                restore(cfg, s.target, s.storage, snap, dest_path, dry_run=True,
                        keep_fstab=args.keep_fstab)
                return
            probe = target_probe(cfg["target"])
            if probe and route_info(probe[0]).get("wireless"):
                warn("Die Verbindung zum Sicherungsziel läuft über WLAN. Bei Aussetzern bricht die "
                     "Wiederherstellung ab – ein LAN-Kabel ist deutlich sicherer.")
            if not confirm(f"Snapshot {snap['name']} ({snap.get('comment') or 'ohne Kommentar'}) "
                           f"auf {what} zurückspielen? Dateien, die seitdem neu hinzugekommen sind, "
                           f"werden gelöscht{' (Home-Verzeichnisse bleiben unberührt)' if not snap.get('include_home') else ''}.",
                           args, strong=live):
                info("Abgebrochen.")
                return
            if live and not args.no_pre_snapshot:
                info("Sicherheits-Snapshot des aktuellen Zustands wird erstellt…")
                create_snapshot(cfg, s.target, s.storage, ["R"],
                                f"Vor Wiederherstellung von {snap['name']}")
            info(f"Stelle {snap['name']} auf {what} wieder her…")
            restore(cfg, s.target, s.storage, snap, dest_path, keep_fstab=args.keep_fstab)
            os.sync()
            if args.update_grub and not live:
                update_grub(dest_path)
            info("Wiederherstellung abgeschlossen.")
        finally:
            if restore_mp:
                _umount(restore_mp)
    if live:
        if args.reboot or (not GUI_MODE and sys.stdin.isatty() and not args.no_reboot
                           and input("Jetzt neu starten (dringend empfohlen)? [J/n] ").strip().lower()
                           in ("", "j", "ja", "y")):
            info("Starte neu…")
            run(["systemctl", "reboot"], check=False)
        else:
            info("Bitte das System so bald wie möglich neu starten.")


def cmd_restore_files(cfg, args):
    if cfg.get("rescue_mode") and not args.to:
        die("Rettungssystem: Bitte mit --to einen Zielordner angeben (sonst landen die Dateien im Rettungssystem).")
    with Session(cfg) as s:
        snap = resolve(s.storage, args.snapshot)
        dest = args.to or "/"
        if args.to:
            os.makedirs(args.to, exist_ok=True)
        root = s.storage.loc(snap["name"], "localhost")
        srcs = []
        for p in args.paths:
            if not p.startswith("/"):
                die(f"Pfad muss absolut sein: {p}")
            rel = p.lstrip("/").rstrip("/")
            srcs.append(f"{root}/./{rel}" + ("/" if args.delete else ""))
        if not confirm(f"{len(srcs)} Pfad(e) aus {snap['name']} nach {dest} zurückholen?", args):
            info("Abgebrochen.")
            return
        cmd = rsync_base(cfg) + ["--relative"] + s.storage.rsync_opts()
        if args.delete:
            cmd.append("--delete")
        if args.dry_run:
            cmd = [c for c in cmd if not c.startswith("--info=")] + ["-n", "--itemize-changes"]
        for src in srcs:
            rc = run_rsync(cmd + [src, dest.rstrip("/") + "/"])
            if rc not in (0, 24):
                warn(f"rsync meldet {rc} für {src}")
        info("Fertig.")


def cmd_check(cfg, args):
    """Vom Timer aufgerufen: fällige Snapshots erstellen, aufräumen, Probleme melden."""
    sched = cfg["schedule"]
    if cfg.get("rescue_mode"):
        return
    if args.boot and not sched.get("boot", {}).get("enabled"):
        return
    enabled = [(lvl, tag, iv) for lvl, tag, iv in LEVELS if sched.get(lvl, {}).get("enabled")]
    if not args.boot and not enabled:
        return
    st = load_status()
    st.setdefault("first_seen", time.time())
    now = time.time()
    by_tag = st.get("last_by_tag")
    # Fällig? – zuerst anhand des gespeicherten Status, damit das NAS nicht jede Stunde geweckt wird
    if by_tag is not None and not args.boot:
        due = [tag for _, tag, iv in enabled if now - by_tag.get(tag, 0) >= iv - 900]
        if not due:
            info("Kein geplanter Snapshot fällig.")
            notify_health(cfg, st)
            save_status(st)
            return
    try:
        with Session(cfg, wait=False) as sess:
            sync_status(sess.storage, st)
            snaps = [x for x in snapshots(sess.storage) if x["valid"]]
            due = ["B"] if args.boot else []
            for _, tag, iv in enabled:
                last = max((x["epoch"] for x in snaps if tag in x["tags"]), default=0)
                if now - last >= iv - 900:
                    due.append(tag)
            other = foreign_host(sess.storage)
            if due and other:
                info(f"Übersprungen: Die Sicherung gehört zum Rechner '{other}', nicht zu '{hostname()}'.")
                return
            if due:
                create_snapshot(cfg, sess.target, sess.storage, due, "Geplant")
                record_success(st)
            else:
                info("Kein geplanter Snapshot fällig.")
            prune(cfg, sess.storage)
            sync_status(sess.storage, st)
    except NetshiftError as e:
        if "anderer netshift-Vorgang" in str(e):
            info("Anderer Vorgang läuft – übersprungen.")
            return
        record_failure(st, e)
        raise
    finally:
        notify_health(cfg, st)
        save_status(st)


def cmd_pacman_hook(cfg, args):
    ph = cfg.get("pacman_hook", {})
    if not ph.get("enabled") or cfg.get("rescue_mode"):
        return 0
    pkgs = [l.strip() for l in sys.stdin.read().split() if l.strip()] if not sys.stdin.isatty() else []
    st = load_status()
    try:
        with Session(cfg) as s:
            snaps = [x for x in snapshots(s.storage) if x["valid"] and "P" in x["tags"]]
            if snaps and time.time() - snaps[-1]["epoch"] < ph.get("min_interval_minutes", 30) * 60:
                info("netshift: letzter Pacman-Snapshot ist noch aktuell – übersprungen.")
                return 0
            if foreign_host(s.storage):
                info("netshift: Sicherung gehört zu einem anderen Rechner – kein Snapshot.")
                return 0
            short = " ".join(pkgs[:8]) + (f" (+{len(pkgs) - 8})" if len(pkgs) > 8 else "")
            create_snapshot(cfg, s.target, s.storage, ["P"], f"pacman: {short}".strip())
            prune(cfg, s.storage)
            record_success(st)
            sync_status(s.storage, st)
    except NetshiftError as e:
        print(f"netshift: Snapshot vor Paketänderung fehlgeschlagen: {e}", file=sys.stderr)
        record_failure(st, e)
        notify_health(cfg, st)
        save_status(st)
        return 1 if ph.get("abort_on_fail") else 0
    notify_health(cfg, st)
    save_status(st)
    return 0


def cmd_mount(cfg, args):
    lock = Lock()
    try:
        t = Target(cfg)
        st = t.open()
        open(os.path.join(RUN_DIR, "keep-mounted"), "w").close()
        if st.remote:
            info(f"SSH-Ziel – Snapshots liegen auf {st.describe()}/snapshots. "
                 "Zum Durchsuchen z.B. sshfs nutzen oder 'netshift restore-files'.")
        else:
            info(st.snapdir)
            if not args.quiet:
                info("Snapshots eingebunden. Jeder Snapshot liegt unter <name>/localhost. "
                     "Aushängen mit: sudo netshift umount")
    finally:
        lock.release()


def cmd_umount(cfg, args):
    if args.shutdown:
        # Beim Herunterfahren: nicht auf andere Vorgänge warten, nie hängen bleiben
        unmount_all(cfg, fast=True)
        info("netshift: Sicherungsziel ausgehängt.")
        return
    lock = Lock()
    try:
        unmount_all(cfg)
        info("Ausgehängt.")
    finally:
        lock.release()


def cmd_netcheck(cfg, args):
    probe = target_probe(cfg["target"])
    if not probe:
        res = {"network": False, "wireless": False, "reachable": True}
    else:
        res = route_info(probe[0])
        res.update(network=True, reachable=reachable(probe))
    if args.json:
        print(json.dumps(res))
        return
    if not res["network"]:
        info("Lokales Ziel – keine Netzwerkverbindung nötig.")
        return
    info(f"Ziel {probe[0]}: {'erreichbar' if res['reachable'] else 'NICHT erreichbar'}, "
         f"Verbindung über {res.get('iface') or '?'}{' (WLAN)' if res['wireless'] else ''}")


def cmd_status(cfg, args):
    t = Target(cfg)
    timers = {}
    for u in ("netshift-check.timer", "netshift-boot.timer"):
        p = run(["systemctl", "is-enabled", u], check=False, capture=True, quiet=True)
        timers[u] = (p.stdout or "").strip() or "unbekannt"
    st = load_status()
    data = {"version": VERSION, "config": CONFIG_PATH, "target": t.describe(),
            "machine_name": cfg["machine_name"], "include_home": cfg["include_home"],
            "schedule": {k: v for k, v in cfg["schedule"].items() if v.get("enabled")},
            "pacman_hook": cfg["pacman_hook"]["enabled"], "timers": timers,
            "last_success": st.get("last_success"), "health": health(cfg, st)}
    if args.json:
        print(json.dumps(data, ensure_ascii=False))
        return
    info(f"netshift {VERSION}")
    info(f"Konfiguration: {CONFIG_PATH}")
    info(f"Ziel:          {t.describe()}  (Maschine: {cfg['machine_name']})")
    if cfg.get("rescue_mode"):
        info("Modus:         RETTUNGSSYSTEM (nur wiederherstellen, keine Snapshots)")
    info(f"Home sichern:  {'ja' if cfg['include_home'] else 'nein'}")
    sch = ", ".join(f"{k} (behalte {v['keep']})" for k, v in data["schedule"].items()) or "aus"
    info(f"Zeitplan:      {sch}")
    info(f"Pacman-Hook:   {'an' if data['pacman_hook'] else 'aus'}")
    for u, stt in timers.items():
        info(f"{u}: {stt}")
    if data["last_success"]:
        info(f"Letzte Sicherung: {dt.datetime.fromtimestamp(data['last_success']).strftime('%d.%m.%Y %H:%M')}")
    for pr in data["health"]:
        info(f"PROBLEM: {pr['title']} – {pr['body']}")


def cmd_test(cfg, args):
    with Session(cfg) as s:
        free, total = s.storage.free()
        if not s.storage.remote:
            p = os.path.join(s.storage.base, ".netshift-test")
            with open(p, "w") as f:
                f.write("ok")
            os.chown(p, 1234, 1234)
            st = os.stat(p)
            os.unlink(p)
            if st.st_uid != 1234:
                warn("Ziel speichert keine Linux-Besitzer – Image-Container verwenden (container: image).")
        info(f"Verbindung OK: {s.target.describe()} [{s.target.mode}] – {human(free)} frei von {human(total)}")


def cmd_resize_image(cfg, args):
    lock = Lock()
    try:
        unmount_all(cfg)
        t = Target(cfg)
        t.validate()
        if t.t["type"] == "ssh":
            die("Bei SSH gibt es keinen Image-Container.")
        share = t._mount_share()
        try:
            if t._container_mode(share) != "image":
                die("Dieses Ziel verwendet keinen Image-Container.")
            img = os.path.join(share, t.t.get("subdir") or "netshift", t.label + ".img")
            if not os.path.exists(img):
                die(f"Image nicht gefunden: {img}")
            cur = os.path.getsize(img)
            new = int(args.size_gb) * 1024 ** 3
            if new <= cur:
                die(f"Neue Größe muss größer als die aktuelle sein ({cur // 1024 ** 3} GiB). Verkleinern wird nicht unterstützt.")
            with open(img, "r+b") as f:
                f.truncate(new)
            dev = img
            if t.t.get("encrypt"):
                run(["cryptsetup", "open", "--type", "luks", "--key-file", t.t["key_file"], img, t.mapper])
                t.opened_crypt = True
                dev = "/dev/mapper/" + t.mapper
            run(["e2fsck", "-f", "-y", dev], check=False)
            run(["resize2fs", dev])
            cfg["target"]["image_size_gb"] = int(args.size_gb)
            save_config(cfg)
            info(f"Image auf {args.size_gb} GiB vergrößert.")
        finally:
            t.close(force=True)
    finally:
        lock.release()


def cmd_schedule_apply(cfg, args):
    sch = cfg["schedule"]
    periodic = any(sch.get(l, {}).get("enabled") for l, _, _ in LEVELS)
    boot = bool(sch.get("boot", {}).get("enabled"))
    if cfg.get("rescue_mode"):
        periodic = boot = False
    for unit, on in (("netshift-check.timer", periodic), ("netshift-boot.timer", boot),
                     ("netshift-shutdown.service", True)):
        run(["systemctl", "enable" if on else "disable", "--now", unit], check=False, capture=True)
        info(f"{unit}: {'aktiviert' if on else 'deaktiviert'}")


def cmd_ssh_setup(cfg, args):
    t = cfg["target"]
    need("ssh-keygen", "openssh")
    key = t.get("ssh_key") or os.path.join(ETC_DIR, "id_ed25519")
    if not os.path.exists(key):
        os.makedirs(os.path.dirname(key), mode=0o700, exist_ok=True)
        run(["ssh-keygen", "-t", "ed25519", "-N", "", "-C", f"netshift@{hostname()}", "-f", key])
    with open(key + ".pub") as f:
        pub = f.read().strip()
    info("Öffentlicher Schlüssel:\n" + pub)
    if args.copy and t.get("server"):
        host = f"{t.get('ssh_user')}@{t['server']}" if t.get("ssh_user") else t["server"]
        info(f"Übertrage Schlüssel zu {host} (Passwort wird einmalig abgefragt)…")
        run(["ssh-copy-id", "-i", key + ".pub", "-p", str(t.get("ssh_port") or 22), host], check=False)


def cmd_config(cfg, args):
    if args.set_json:
        new = json.load(sys.stdin)
        save_config(deep_merge(DEFAULT_CONFIG, new))
        info("Konfiguration gespeichert.")
    elif args.path:
        print(CONFIG_PATH)
    else:
        print(json.dumps(cfg, indent=2, ensure_ascii=False))


def ask(prompt, default=""):
    a = input(f"{prompt}" + (f" [{default}]" if default != "" else "") + ": ").strip()
    return a if a else str(default)


def cmd_setup(cfg, args):
    if not sys.stdin.isatty():
        die("'setup' ist interaktiv. Alternativ die GUI (netshift-gui) verwenden.")
    t = cfg["target"]
    info("=== netshift Einrichtung ===  (Enter übernimmt den Vorgabewert)")
    cfg["machine_name"] = ask("Name dieses Rechners im Backup", cfg["machine_name"])
    typ = ask("Zieltyp: smb / nfs / ssh / local", t["type"]).lower()
    t["type"] = typ
    if typ == "smb":
        t["server"] = ask("Server (Name oder IP, z.B. 192.168.1.10)", t["server"])
        t["share"] = ask("Freigabe (z.B. backup oder backup/ordner)", t["share"])
        t["smb_version"] = ask("SMB-Version (3.0, 2.1, 1.0)", t["smb_version"])
        cur = read_smb_credentials()
        user = ask("Benutzername (leer = Gast)", cur.get("username", ""))
        if user:
            import getpass
            pw = getpass.getpass("Passwort (Enter = unverändert): ") or cur.get("password", "")
            dom = ask("Domäne/Arbeitsgruppe (optional)", cur.get("domain", ""))
            write_smb_credentials(user, pw, dom)
        elif os.path.exists(cred_path()):
            os.remove(cred_path())
    elif typ == "nfs":
        t["server"] = ask("Server", t["server"])
        t["export"] = ask("Export-Pfad (z.B. /volume1/backup)", t["export"])
    elif typ == "ssh":
        t["server"] = ask("Server", t["server"])
        t["ssh_user"] = ask("Benutzer", t["ssh_user"])
        t["ssh_port"] = int(ask("Port", t["ssh_port"]))
        t["ssh_path"] = ask("Zielpfad auf dem Server", t["ssh_path"])
        fs = ask("Server-Benutzer ist nicht root? Besitzer per --fake-super speichern (j/n)",
                 "j" if t["ssh_fake_super"] else ("j" if t["ssh_user"] != "root" else "n"))
        t["ssh_fake_super"] = fs.lower().startswith("j")
    elif typ == "local":
        t["path"] = ask("Pfad, Gerät oder UUID=… (z.B. /run/media/user/USB oder UUID=1234-…)", t["path"])
    else:
        die("Unbekannter Typ.")
    if typ != "ssh":
        if typ in ("smb", "nfs") or t["container"] == "image":
            t["image_size_gb"] = int(ask("Maximale Größe des Image-Containers in GiB", t["image_size_gb"]))
            t["encrypt"] = ask("Image mit LUKS verschlüsseln? (j/n)", "j" if t["encrypt"] else "n").lower().startswith("j")
    cfg["include_home"] = ask("Home-Verzeichnisse mitsichern? (j/n)", "j" if cfg["include_home"] else "n").lower().startswith("j")
    for lvl, label in (("boot", "Beim Booten"), ("hourly", "Stündlich"), ("daily", "Täglich"),
                       ("weekly", "Wöchentlich"), ("monthly", "Monatlich")):
        s = cfg["schedule"][lvl]
        on = ask(f"{label} automatisch sichern? (j/n)", "j" if s["enabled"] else "n").lower().startswith("j")
        s["enabled"] = on
        if on:
            s["keep"] = int(ask(f"  Wie viele {label.lower()}e Snapshots behalten", s["keep"]))
    ph = cfg["pacman_hook"]
    ph["enabled"] = ask("Snapshot vor jedem pacman-Update? (j/n)", "j" if ph["enabled"] else "n").lower().startswith("j")
    save_config(cfg)
    info(f"Gespeichert in {CONFIG_PATH}.")
    if typ == "ssh":
        cmd_ssh_setup(cfg, argparse.Namespace(copy=True))
    try:
        cmd_schedule_apply(cfg, args)
    except NetshiftError as e:
        warn(str(e))
    info("Teste Verbindung…")
    cmd_test(cfg, args)


# --------------------------------------------------------------------------- main

def build_parser():
    p = argparse.ArgumentParser(prog="netshift", description=(
        "System-Snapshots wie Timeshift – auch auf SMB-, NFS- und SSH-Ziele."))
    p.add_argument("--config", help="Alternative Konfigurationsdatei (z.B. im Live-System)")
    p.add_argument("--version", action="version", version=f"netshift {VERSION}")
    sp = p.add_subparsers(dest="cmd", metavar="BEFEHL")

    c = sp.add_parser("create", help="Snapshot erstellen")
    c.add_argument("-c", "--comment", default="")
    c.add_argument("--tags", help="Tags, z.B. O (manuell), D (täglich) … Standard: O")
    c.add_argument("--prune", action="store_true", help="danach Aufbewahrungsregeln anwenden")
    c.add_argument("--allow-other-host", action="store_true",
                   help="auch erstellen, wenn die Sicherung von einem anders benannten Rechner stammt")

    l = sp.add_parser("list", help="Snapshots anzeigen")
    l.add_argument("--json", action="store_true")

    d = sp.add_parser("delete", help="Snapshot(s) löschen")
    d.add_argument("snapshots", nargs="+", help="Name, Nummer aus 'list' oder 'latest'")
    d.add_argument("-y", "--yes", action="store_true")

    ul = sp.add_parser("unlock", help="Verschlüsselte Partition (LUKS) entsperren")
    ul.add_argument("device")
    ul.add_argument("--stdin", action="store_true", help="Passwort von der Standardeingabe lesen")
    lk = sp.add_parser("lock", help="Von netshift entsperrte Partition wieder sperren")
    lk.add_argument("devices", nargs="+")
    sz = sp.add_parser("size", help="Belegten Speicherplatz von Snapshots anzeigen")
    sz.add_argument("snapshots", nargs="*", help="Name/Nummer (Standard: alle)")
    sz.add_argument("--json", action="store_true")
    sz.add_argument("--refresh", action="store_true", help="neu berechnen statt zwischengespeichert")
    cm = sp.add_parser("comment", help="Kommentar/Tags eines Snapshots ändern")
    cm.add_argument("snapshot")
    cm.add_argument("text")
    cm.add_argument("--tags")

    r = sp.add_parser("restore", help="System aus Snapshot wiederherstellen")
    r.add_argument("snapshot", help="Name, Nummer aus 'list' oder 'latest'")
    r.add_argument("--target", help="Ziel: / (Standard), Verzeichnis, /dev/sdXN oder UUID=… (z.B. vom Live-USB)")
    r.add_argument("--dry-run", action="store_true", help="nur anzeigen, was geändert würde")
    r.add_argument("--keep-fstab", action="store_true", help="/etc/fstab und /etc/crypttab des Ziels behalten")
    r.add_argument("--no-pre-snapshot", action="store_true", help="vorher keinen Sicherheits-Snapshot erstellen")
    r.add_argument("--update-grub", action="store_true", help="nach Restore auf anderes Ziel grub-mkconfig im chroot")
    r.add_argument("--reboot", action="store_true", help="danach sofort neu starten")
    r.add_argument("--no-reboot", action="store_true")
    r.add_argument("--allow-other-host", action="store_true",
                   help="Snapshot eines anders benannten Rechners auf das laufende System spielen")
    r.add_argument("-y", "--yes", action="store_true")

    rf = sp.add_parser("restore-files", help="Einzelne Dateien/Ordner aus Snapshot zurückholen")
    rf.add_argument("snapshot")
    rf.add_argument("paths", nargs="+", help="absolute Pfade, z.B. /etc/pacman.conf")
    rf.add_argument("--to", help="in dieses Verzeichnis statt an den Originalort")
    rf.add_argument("--delete", action="store_true", help="Ordner exakt angleichen (löscht neuere Dateien darin)")
    rf.add_argument("--dry-run", action="store_true")
    rf.add_argument("-y", "--yes", action="store_true")

    df = sp.add_parser("diff", help="Zeigt, was ein Restore ändern würde (Vorschau)")
    df.add_argument("snapshot")
    df.add_argument("--target")
    df.add_argument("--keep-fstab", action="store_true")

    ck = sp.add_parser("check", help="Geplante Snapshots (vom Timer aufgerufen)")
    ck.add_argument("--boot", action="store_true")

    sp.add_parser("pacman-hook", help="(intern) vom pacman-Hook aufgerufen")
    m = sp.add_parser("mount", help="Snapshots zum Durchsuchen einbinden")
    m.add_argument("-q", "--quiet", action="store_true")
    um = sp.add_parser("umount", help="Eingebundene Snapshots aushängen")
    um.add_argument("--shutdown", action="store_true", help="(intern) schnell aushängen beim Herunterfahren")
    nc = sp.add_parser("netcheck", help="Verbindung zum Netzwerkziel prüfen (WLAN?)")
    nc.add_argument("--json", action="store_true")
    st = sp.add_parser("status", help="Konfiguration und Zeitplan anzeigen")
    st.add_argument("--json", action="store_true")
    sp.add_parser("test", help="Verbindung zum Ziel testen")
    rs = sp.add_parser("resize-image", help="Image-Container vergrößern")
    rs.add_argument("size_gb", type=int)
    sp.add_parser("schedule-apply", help="systemd-Timer gemäß Konfiguration (de)aktivieren")
    ss = sp.add_parser("ssh-setup", help="SSH-Schlüssel erzeugen und auf den Server kopieren")
    ss.add_argument("--no-copy", dest="copy", action="store_false")
    cf = sp.add_parser("config", help="Konfiguration anzeigen")
    cf.add_argument("--set-json", action="store_true", help="Konfiguration als JSON von stdin übernehmen")
    cf.add_argument("--path", action="store_true")
    sp.add_parser("setup", help="Interaktive Einrichtung im Terminal")
    return p


def main(argv=None):
    global CONFIG_PATH
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.cmd:
        parser.print_help()
        return 0
    if args.config:
        CONFIG_PATH = os.path.abspath(args.config)
    if os.geteuid() != 0 and args.cmd not in ("config",):
        print("netshift benötigt Root-Rechte – bitte mit sudo ausführen.", file=sys.stderr)
        return 1
    cfg = load_config()
    handlers = {
        "create": cmd_create, "list": cmd_list, "delete": cmd_delete, "comment": cmd_comment, "size": cmd_size, "unlock": cmd_unlock, "lock": cmd_lock, "netcheck": cmd_netcheck,
        "restore": cmd_restore, "restore-files": cmd_restore_files, "check": cmd_check,
        "pacman-hook": cmd_pacman_hook, "mount": cmd_mount, "umount": cmd_umount,
        "status": cmd_status, "test": cmd_test, "resize-image": cmd_resize_image,
        "schedule-apply": cmd_schedule_apply, "ssh-setup": cmd_ssh_setup,
        "config": cmd_config, "setup": cmd_setup,
    }
    if args.cmd == "diff":
        args.dry_run, args.no_pre_snapshot, args.update_grub = True, True, False
        args.reboot, args.no_reboot, args.yes = False, True, True
        args.allow_other_host = True
        handlers["diff"] = cmd_restore
    try:
        return handlers[args.cmd](cfg, args) or 0
    except NetshiftError as e:
        print(f"FEHLER: {e}", file=sys.stderr if not GUI_MODE else sys.stdout, flush=True)
        return 2
    except KeyboardInterrupt:
        print("\nAbgebrochen.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
