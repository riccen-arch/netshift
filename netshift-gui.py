#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""netshift – grafische Oberfläche (Tkinter). Läuft als root (über pkexec gestartet)."""

import importlib.util
import json
import os
import pwd
import queue
import shutil
import subprocess
import sys
import threading
import tkinter as tk
from tkinter import ttk, messagebox, simpledialog, filedialog
from tkinter.scrolledtext import ScrolledText

HERE = os.path.dirname(os.path.realpath(__file__))
CORE = next((p for p in (os.path.join(HERE, "netshift.py"), "/usr/lib/netshift/netshift.py")
             if os.path.exists(p)), None)
if CORE is None:
    sys.exit("netshift.py not found")
_spec = importlib.util.spec_from_file_location("netshift_core", CORE)
ns = importlib.util.module_from_spec(_spec)
for _a in list(sys.argv[1:]):                    # Sprache vom Starter (pkexec verwirft LANG)
    if _a.startswith("--lang="):
        os.environ["NETSHIFT_LANG"] = _a.split("=", 1)[1]
    if _a.startswith("--"):
        sys.argv.remove(_a)
_spec.loader.exec_module(ns)
_ = ns._

TYPE_LABELS = [("smb", _('Netzwerkfreigabe (SMB/Windows/NAS)')), ("nfs", _('NFS-Freigabe')),
               ("ssh", _('Linux-Server über SSH')), ("local", _('Lokales Laufwerk / USB'))]
CONTAINER_LABELS = [("auto", _('Automatisch (empfohlen)')), ("image", _('Image-Container (ext4)')),
                    ("direct", _('Direkt im Dateisystem'))]
LEVEL_LABELS = [("boot", _('Beim Systemstart')), ("hourly", _('Stündlich')), ("daily", _('Täglich')),
                ("weekly", _('Wöchentlich')), ("monthly", _('Monatlich'))]


def lsblk_parts():
    try:
        out = subprocess.run(["lsblk", "-J", "-o", "PATH,UUID,FSTYPE,SIZE,LABEL,MOUNTPOINT,TYPE"],
                             capture_output=True, text=True).stdout
        devs = []

        def walk(lst):
            for d in lst:
                kids = d.get("children") or []
                if d.get("fstype") == "crypto_LUKS":
                    if not any(k.get("type") == "crypt" for k in kids):
                        devs.append(dict(d, fstype=_('verschlüsselt – wird beim Start entsperrt')))
                elif d.get("fstype") and d.get("type") in ("part", "crypt", "lvm", "disk") \
                        and d.get("fstype") not in ("swap", "LVM2_member"):
                    devs.append(d)
                walk(kids)
        walk(json.loads(out).get("blockdevices", []))
        return devs
    except Exception:
        return []


def dev_label(d):
    return f"{d['path']}  ({d.get('fstype')}, {d.get('size')}{', ' + d['label'] if d.get('label') else ''}" \
           f"{_(', eingehängt: ') + d['mountpoint'] if d.get('mountpoint') else ''})"


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(_('netshift – System-Snapshots'))
        self.geometry("1040x660")
        self.minsize(820, 480)
        try:
            ttk.Style(self).theme_use("clam")
        except tk.TclError:
            pass
        self.busy = False
        self.q = queue.Queue()
        self.snaps = []
        self.mounted_by_us = False
        self.unlocked = []
        self.listing = None
        self._build()
        self.protocol("WM_DELETE_WINDOW", self.on_close)
        self.after(100, self._poll)
        if not os.path.exists(ns.CONFIG_PATH):
            self.after(300, lambda: (messagebox.showinfo(
                _('Willkommen'), _('netshift ist noch nicht eingerichtet.\nBitte zuerst das Sicherungsziel festlegen.')), self.open_settings()))
        else:
            self.after(200, self.refresh)

    # ------------------------------------------------------------------ Aufbau
    def _build(self):
        bar = ttk.Frame(self, padding=(8, 8, 8, 4))
        bar.pack(fill="x")
        self.buttons = []
        for text, cmd in ((_('➕ Erstellen'), self.create), (_('↺ Wiederherstellen'), self.restore_dialog),
                          (_('📄 Dateien zurückholen'), self.files_dialog), (_('🗑 Löschen'), self.delete),
                          (_('📂 Durchsuchen'), self.browse), (_('⚙ Einstellungen'), self.open_settings),
                          (_('⟳ Aktualisieren'), self.refresh)):
            b = ttk.Button(bar, text=text, command=cmd)
            b.pack(side="left", padx=(0, 6))
            self.buttons.append(b)

        mid = ttk.Frame(self, padding=(8, 0))
        mid.pack(fill="both", expand=True)
        cols = ("name", "tags", "system", "comment", "status")
        self.tree = ttk.Treeview(mid, columns=cols, show="headings", selectmode="extended")
        for c, t, w in (("name", _('Snapshot'), 170), ("tags", _('Typ'), 150), ("system", _('System'), 200),
                        ("comment", _('Kommentar'), 340), ("status", _('Status'), 90)):
            self.tree.heading(c, text=t)
            self.tree.column(c, width=w, anchor="w", stretch=(c == "comment"))
        sb = ttk.Scrollbar(mid, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        sb.pack(side="left", fill="y")
        self.tree.bind("<Double-1>", lambda e: self.details())

        st = ttk.Frame(self, padding=(8, 6))
        st.pack(fill="x")
        self.lbl_target = ttk.Label(st, text=_('Ziel: –'))
        self.lbl_target.pack(side="left")
        self.lbl_free = ttk.Label(st, text="")
        self.lbl_free.pack(side="right")

        pr = ttk.Frame(self, padding=(8, 0))
        pr.pack(fill="x")
        self.progress = ttk.Progressbar(pr, mode="determinate", maximum=100)
        self.progress.pack(side="left", fill="x", expand=True)
        self.lbl_prog = ttk.Label(pr, text=_('Bereit'), width=42, anchor="e")
        self.lbl_prog.pack(side="left", padx=(8, 0))

        lf = ttk.LabelFrame(self, text=_('Protokoll'), padding=4)
        lf.pack(fill="both", padx=8, pady=8)
        self.log = ScrolledText(lf, height=8, font=("monospace", 9), state="disabled", wrap="word")
        self.log.pack(fill="both", expand=True)

    # ------------------------------------------------------------------ Hilfen
    def write_log(self, text):
        self.log.configure(state="normal")
        self.log.insert("end", text.rstrip("\n") + "\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    def set_busy(self, busy, msg=""):
        self.busy = busy
        rescue = bool(ns.load_config().get("rescue_mode"))
        for i, b in enumerate(self.buttons):
            b.configure(state="disabled" if busy else "normal")
        self.configure(cursor="watch" if busy else "")
        if busy:
            self.progress.configure(mode="indeterminate")
            self.progress.start(12)
            self.lbl_prog.configure(text=msg or _('Arbeite…'))
        else:
            self.progress.stop()
            self.progress.configure(mode="determinate", value=0)
            self.lbl_prog.configure(text=_('Bereit'))

    def run_cli(self, args, done=None, msg="", log=True, capture=False):
        if self.busy:
            messagebox.showwarning(_('Bitte warten'), _('Es läuft bereits ein Vorgang.'))
            return
        self.set_busy(True, msg)
        if log:
            self.write_log("▶ netshift " + " ".join(args))

        def worker():
            env = dict(os.environ, NETSHIFT_GUI="1", PYTHONUNBUFFERED="1")
            lines = []
            try:
                p = subprocess.Popen([sys.executable, CORE] + args, stdout=subprocess.PIPE,
                                     stderr=subprocess.STDOUT, text=True, bufsize=1, env=env)
                for line in p.stdout:
                    lines.append(line)
                    if not capture or line.startswith(("@@ERROR", "@@WARN", "@@PROGRESS", "@@PHASE", "@@STEP")):
                        self.q.put(("line", line, log))
                rc = p.wait()
            except Exception as e:
                lines.append(f"@@ERROR {e}\n")
                self.q.put(("line", f"@@ERROR {e}", True))
                rc = 99
            self.q.put(("done", rc, lines, done))
        threading.Thread(target=worker, daemon=True).start()

    def _poll(self):
        try:
            while True:
                item = self.q.get_nowait()
                if item[0] == "line":
                    line, log = item[1], item[2]
                    if line.startswith("@@PHASE"):
                        # neue rsync-Phase: Balken pendelt, bis die Gesamtmenge bekannt ist
                        self.progress.configure(mode="indeterminate", value=0)
                        self.progress.start(12)
                        self.lbl_prog.configure(text=line[len("@@PHASE"):].strip())
                    elif line.startswith("@@PROGRESS"):
                        parts = line.split()
                        try:
                            pct = int(parts[1])
                            if str(self.progress.cget("mode")) != "determinate":
                                self.progress.stop()
                                self.progress.configure(mode="determinate")
                            self.progress.configure(value=pct)
                            if pct >= 100:
                                self.lbl_prog.configure(text=_('100%  – wird abgeschlossen…'))
                            else:
                                self.lbl_prog.configure(text=_('{pct}%  {0}  Rest {1}').format(parts[2], parts[3], pct=pct))
                        except (IndexError, ValueError):
                            pass
                    elif line.startswith("@@ERROR "):
                        self.write_log("✖ " + line[8:])
                    elif line.startswith("@@WARN "):
                        self.write_log("⚠ " + line[7:])
                    elif line.startswith("@@STEP "):
                        self.write_log(line[7:])
                        self.lbl_prog.configure(text=line[7:].strip()[:42])
                    elif line.startswith("@@RESULT "):
                        self.write_log(line[9:])
                    elif log:
                        self.write_log(line)
                else:
                    _x, rc, lines, done = item
                    self.set_busy(False)
                    if rc != 0:
                        err = [l[8:] for l in lines if l.startswith("@@ERROR ")]
                        if err:
                            messagebox.showerror(_('Fehler'), "".join(err))
                    if done:
                        done(rc, lines)
        except queue.Empty:
            pass
        self.after(100, self._poll)

    def selected(self, single=True):
        sel = self.tree.selection()
        if not sel:
            messagebox.showinfo(_('Auswahl'), _('Bitte zuerst einen Snapshot in der Liste auswählen.'))
            return None
        names = [self.tree.item(i, "values")[0] for i in sel]
        snaps = [s for s in self.snaps if s["name"] in names]
        if single:
            if len(snaps) != 1:
                messagebox.showinfo(_('Auswahl'), _('Bitte genau einen Snapshot auswählen.'))
                return None
            return snaps[0]
        return snaps

    def cfg(self):
        return ns.load_config()

    # ------------------------------------------------------------------ Aktionen
    def refresh(self):
        self.lbl_target.configure(text=_('Ziel: ') + ns.Target(self.cfg()).describe())

        def done(rc, lines):
            data = None
            for l in reversed(lines):
                if l.startswith("{"):
                    try:
                        data = json.loads(l)
                    except ValueError:
                        pass
                    break
            if data is None:
                return
            self.listing = data
            self.snaps = data["snapshots"]
            rescue = bool(ns.load_config().get("rescue_mode"))
            self.buttons[0].configure(text=_('💽 Rechner sichern') if rescue else _('➕ Erstellen'))
            self.tree.delete(*self.tree.get_children())
            for s in reversed(self.snaps):
                tags = ", ".join(ns.TAG_NAMES.get(t, t) for t in s["tags"]) or "–"
                status = "OK" if s.get("status") == "ok" else (
                    _('Warnungen') if s.get("status") == "warnings" else _('unvollständig'))
                self.tree.insert("", "end", values=(s["name"], tags, s.get("os", ""),
                                                    s.get("comment", ""), status))
            free, total = data.get("free", 0), data.get("total", 0)
            mode = {"image": _('Image-Container'), "direct": _("direkt"), "ssh": "SSH"}.get(data.get("mode"), "")
            self.lbl_target.configure(text=_('Ziel: {0}  [{mode}]  –  {1} Snapshots').format(data['target'], len(self.snaps), mode=mode))
            self.lbl_free.configure(text=_('Frei: {0} von {1}').format(ns.human(free), ns.human(total)) if total else "")
            probs = data.get("health") or []
            if probs:
                self.lbl_target.configure(text="⚠ " + " · ".join(p["title"] for p in probs) + "   |   "
                                          + self.lbl_target.cget("text"), foreground="#b00020")
                for pr in probs:
                    self.write_log(f"⚠ {pr['title']}: {pr['body']}")
            else:
                self.lbl_target.configure(foreground="")
            if data.get("in_progress"):
                self.write_log(_('Hinweis: ein unterbrochener Snapshot wird beim nächsten Erstellen fortgesetzt.'))
        self.run_cli(["list", "--json"], done, msg=_('Lade Snapshots…'), log=False, capture=True)

    def create(self):
        if ns.load_config().get("rescue_mode"):
            OfflineDialog(self)
            return
        c = simpledialog.askstring(_('Snapshot erstellen'), _('Kommentar (optional):'), parent=self)
        if c is None:
            return
        self.run_cli(["create", "-c", c], lambda rc, l: self.refresh(), msg=_('Erstelle Snapshot…'))

    def delete(self):
        snaps = self.selected(single=False)
        if not snaps:
            return
        names = [s["name"] for s in snaps]
        if not messagebox.askyesno(_('Löschen'), _('Folgende Snapshots endgültig löschen?\n\n') + "\n".join(names)):
            return
        self.run_cli(["delete", "-y"] + names, lambda rc, l: self.refresh(), msg=_('Lösche…'))

    def details(self):
        s = self.selected()
        if not s:
            return
        keys = [("name", _('Name')), ("created", _('Erstellt')), ("tags", "Tags"), ("comment", _('Kommentar')),
                ("os", _('System')), ("kernel", _('Kernel')), ("host", _('Rechner')), ("duration_s", _('Dauer (s)')),
                ("link_dest", _('Basis (inkrementell zu)')), ("include_home", _('Home enthalten')),
                ("status", _('Status'))]
        txt = "\n".join(f"{lbl}: {s.get(k, '')}" for k, lbl in keys)
        new = simpledialog.askstring(_('Snapshot-Details'), txt + _('\n\nKommentar ändern:'),
                                     initialvalue=s.get("comment", ""), parent=self)
        if new is not None and new != s.get("comment", ""):
            self.run_cli(["comment", s["name"], new], lambda rc, l: self.refresh(), msg=_('Speichere…'))

    def _ensure_mounted(self):
        if self.listing and self.listing.get("mode") == "ssh":
            return None
        p = subprocess.run([sys.executable, CORE, "mount", "-q"], capture_output=True, text=True,
                           env=dict(os.environ, NETSHIFT_GUI="1"))
        if p.returncode != 0:
            messagebox.showerror(_('Fehler'), p.stdout + p.stderr)
            return None
        self.mounted_by_us = True
        path = p.stdout.strip().splitlines()[-1]
        return path if os.path.isdir(path) else None

    def browse(self):
        s = self.selected()
        if not s:
            return
        if self.listing and self.listing.get("mode") == "ssh":
            messagebox.showinfo(_('SSH-Ziel'), _("Snapshots liegen auf dem SSH-Server. Bitte 'Dateien zurückholen' verwenden."))
            return
        base = self._ensure_mounted()
        if not base:
            return
        path = os.path.join(base, s["name"], "localhost")
        if not open_as_user(path):
            messagebox.showinfo(_('Durchsuchen'), _('Snapshot eingebunden unter:\n{path}\n\n(Wird beim Schließen von netshift wieder ausgehängt.)').format(path=path))
        else:
            self.write_log(_('Geöffnet: {path}').format(path=path))

    def restore_dialog(self):
        s = self.selected()
        if s:
            RestoreDialog(self, s)

    def files_dialog(self):
        s = self.selected()
        if s:
            FilesDialog(self, s)

    def open_settings(self):
        SettingsDialog(self)

    def show_output(self, title, lines):
        w = tk.Toplevel(self)
        w.title(title)
        w.geometry("860x520")
        t = ScrolledText(w, font=("monospace", 9), wrap="none")
        t.pack(fill="both", expand=True)
        body = [l for l in lines if not l.startswith(("@@PROGRESS", "@@PHASE"))]
        t.insert("end", "".join(body) if body else _('Keine Änderungen.'))
        t.configure(state="disabled")
        ttk.Button(w, text=_('Schließen'), command=w.destroy).pack(pady=6)

    def on_close(self):
        if self.busy and not messagebox.askyesno(
                _('Beenden'), _('Ein Vorgang läuft noch. Er läuft im Hintergrund weiter. Trotzdem schließen?')):
            return
        if self.mounted_by_us and not self.busy:
            subprocess.run([sys.executable, CORE, "umount"], capture_output=True)
        if self.unlocked and not self.busy:
            subprocess.run([sys.executable, CORE, "lock"] + sorted(set(self.unlocked)), capture_output=True)
        self.destroy()


def open_as_user(path):
    """Dateimanager als der Benutzer öffnen, der netshift gestartet hat."""
    uid = os.environ.get("PKEXEC_UID") or os.environ.get("SUDO_UID")
    if not uid or not shutil.which("xdg-open"):
        return False
    try:
        user = pwd.getpwuid(int(uid)).pw_name
    except (KeyError, ValueError):
        return False
    rt = f"/run/user/{uid}"
    env = [f"DISPLAY={os.environ.get('DISPLAY', ':0')}", f"XDG_RUNTIME_DIR={rt}",
           f"DBUS_SESSION_BUS_ADDRESS=unix:path={rt}/bus"]
    for w in ("wayland-0", "wayland-1"):
        if os.path.exists(os.path.join(rt, w)):
            env.append(f"WAYLAND_DISPLAY={w}")
            break
    try:
        subprocess.Popen(["runuser", "-u", user, "--", "env"] + env + ["xdg-open", path],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        return True
    except OSError:
        return False


# ---------------------------------------------------------------------- Dialoge

def unlock_if_needed(win, app, v):
    """Verschlüsselte Partition entsperren (fragt nach dem Passwort). Liefert den nutzbaren Pfad oder None."""
    if not (v.startswith(("/dev/", "UUID=")) and ns.is_luks(v)):
        return v
    kids = ns.luks_children(v)
    if kids:
        return kids[0]
    while True:
        pw = simpledialog.askstring(_('Verschlüsselte Partition'),
                                    _('{v} ist verschlüsselt.\nPasswort zum Entsperren (wie beim Starten des Rechners):').format(v=v), show="•", parent=win)
        if not pw:
            return None
        p = subprocess.run([sys.executable, CORE, "unlock", v, "--stdin"], input=pw, capture_output=True, text=True)
        if p.returncode == 0:
            path = p.stdout.strip().splitlines()[-1]
            app.unlocked.append(path)
            return path
        if p.returncode != 3:      # 3 = falsches Passwort
            messagebox.showerror(_('Entsperren fehlgeschlagen'), p.stdout + p.stderr, parent=win)
            return None
        messagebox.showwarning(_('Falsches Passwort'), _('Das Passwort war falsch.'), parent=win)


class OfflineDialog(tk.Toplevel):
    """Festplatte des Rechners sichern, während er nicht läuft (Rettungssystem)."""

    def __init__(self, app):
        super().__init__(app)
        self.app = app
        self.title(_('Rechner sichern'))
        self.transient(app)
        f = ttk.Frame(self, padding=14)
        f.pack(fill="both", expand=True)
        ttk.Label(f, text=_('Der Rechner läuft dabei nicht – so entsteht ein besonders sauberer Snapshot. Er landet in derselben Sicherung wie die Snapshots, die der Rechner selbst erstellt.'),
                  wraplength=520, justify="left").pack(anchor="w", pady=(0, 10))
        ttk.Label(f, text=_('Systempartition des Rechners (z.B. /dev/nvme0n1p2)')).pack(anchor="w")
        self.devs = lsblk_parts()
        self.target = tk.StringVar()
        ttk.Combobox(f, textvariable=self.target, width=70,
                     values=[dev_label(d) for d in self.devs if d.get("mountpoint") != "/"]).pack(fill="x", pady=(2, 10))
        ttk.Label(f, text=_('Kommentar (optional):')).pack(anchor="w")
        self.comment = tk.StringVar()
        ttk.Entry(f, textvariable=self.comment).pack(fill="x", pady=(2, 12))
        bf = ttk.Frame(f)
        bf.pack(fill="x")
        ttk.Button(bf, text=_('Abbrechen'), command=self.destroy).pack(side="right")
        ttk.Button(bf, text=_('Sichern'), command=self.go).pack(side="right", padx=6)

    def go(self):
        v = self.target.get().strip().split(" ")[0] if self.target.get().strip() else ""
        if not v:
            messagebox.showwarning(_('Ziel fehlt'), _('Bitte Laufwerk oder Verzeichnis angeben.'), parent=self)
            return
        v = unlock_if_needed(self, self.app, v)
        if not v:
            return
        p = subprocess.run([sys.executable, CORE, "identify", v], capture_output=True, text=True)
        try:
            res = json.loads(p.stdout.strip().splitlines()[-1])
        except (ValueError, IndexError):
            res = {}
        if not (res.get("linux") and res.get("name")):
            messagebox.showwarning(_('Kein Linux-System gefunden'),
                                   _('Bitte die Systempartition des Rechners wählen (die mit /etc).'), parent=self)
            return
        name = res["name"]
        if not messagebox.askokcancel(_('Rechner sichern'), _('{os} · Rechner „{host}“').format(
                os=res.get("os") or "Linux", host=res.get("host") or name) + "\n\n"
                + _('Wird gesichert als „{0}“').format(name), parent=self):
            return
        args = ["create", "--source", v] + (["-c", self.comment.get().strip()] if self.comment.get().strip() else [])
        self.destroy()
        app = self.app

        def done(rc, lines):
            cfg = ns.load_config()
            if rc == 0 and cfg.get("rescue_mode") and name != cfg.get("machine_name"):
                cfg["machine_name"] = name           # Rettungssystem: ab jetzt diesen Rechner anzeigen
                subprocess.run([sys.executable, CORE, "config", "--set-json"], input=json.dumps(cfg),
                               capture_output=True, text=True)
            app.refresh()
        app.run_cli(args, done, msg=_('Rechner wird gesichert'))


class RestoreDialog(tk.Toplevel):
    def __init__(self, app, snap):
        super().__init__(app)
        self.app, self.snap = app, snap
        self.title(_('System wiederherstellen'))
        self.transient(app)
        self.resizable(False, False)
        f = ttk.Frame(self, padding=14)
        f.pack(fill="both", expand=True)
        ttk.Label(f, text=_('Snapshot {0}').format(snap['name']), font=("TkDefaultFont", 11, "bold")).pack(anchor="w")
        ttk.Label(f, text=_('{0} · Kernel {1} · {2}').format(snap.get('os', ''), snap.get('kernel', '?'), snap.get('comment') or _('ohne Kommentar'))).pack(anchor="w", pady=(0, 10))

        rescue = bool(ns.load_config().get("rescue_mode"))
        foreign = bool(snap.get("host")) and snap["host"] != ns.hostname()
        self.mode = tk.StringVar(value="other" if (rescue or foreign) else "live")
        rb_live = ttk.Radiobutton(f, text=_('Laufendes System wiederherstellen (wie Timeshift, danach Neustart)'),
                                  variable=self.mode, value="live", command=self.sync)
        rb_live.pack(anchor="w")
        if rescue or foreign:
            rb_live.configure(state="disabled")
            ttk.Label(f, text=_('Rettungssystem: Bitte unten die Systempartition des Rechners wählen.'),
                      foreground="#a04000").pack(anchor="w", padx=(22, 0))
        ttk.Radiobutton(f, text=_('Auf anderes Laufwerk/Verzeichnis (Neuinstallation, Live-USB, Ordner)'),
                        variable=self.mode, value="other", command=self.sync).pack(anchor="w")
        row = ttk.Frame(f)
        row.pack(fill="x", padx=(22, 0), pady=4)
        self.devs = lsblk_parts()
        self.devs = [d for d in self.devs if d.get("mountpoint") != "/"]   # laufendes System nie anbieten
        self.target = ttk.Combobox(row, width=62, values=[dev_label(d) for d in self.devs])
        self.target.pack(side="left")
        ttk.Button(row, text=_('Ordner…'), command=self.pick_dir).pack(side="left", padx=4)

        self.pre = tk.BooleanVar(value=True)
        self.reboot = tk.BooleanVar(value=True)
        self.fstab = tk.BooleanVar(value=False)
        self.grub = tk.BooleanVar(value=False)
        self.cb_pre = ttk.Checkbutton(f, text=_('Vorher Sicherheits-Snapshot des aktuellen Zustands anlegen'),
                                      variable=self.pre)
        self.cb_reboot = ttk.Checkbutton(f, text=_('Danach automatisch neu starten'), variable=self.reboot)
        self.cb_fstab = ttk.Checkbutton(f, text=_('/etc/fstab und /etc/crypttab des Ziels behalten (bei geänderten Partitionen/UUIDs)'), variable=self.fstab)
        self.cb_grub = ttk.Checkbutton(f, text=_('GRUB-Konfiguration im Ziel neu erzeugen (grub-mkconfig)'),
                                       variable=self.grub)
        for cb in (self.cb_pre, self.cb_reboot, self.cb_fstab, self.cb_grub):
            cb.pack(anchor="w", pady=1)
        note = (_('Hinweis: Home-Verzeichnisse bleiben unverändert.') if not snap.get("include_home")
                else _('Achtung: Dieser Snapshot enthält auch /home – persönliche Dateien werden zurückgesetzt!'))
        ttk.Label(f, text=note, foreground="#a04000").pack(anchor="w", pady=(8, 0))
        b = ttk.Frame(f)
        b.pack(fill="x", pady=(12, 0))
        ttk.Button(b, text=_('Abbrechen'), command=self.destroy).pack(side="right")
        ttk.Button(b, text=_('Wiederherstellen'), command=self.go).pack(side="right", padx=6)
        ttk.Button(b, text=_('Vorschau der Änderungen'), command=self.preview).pack(side="left")
        self.sync()

    def sync(self):
        live = self.mode.get() == "live"
        self.target.configure(state="disabled" if live else "normal")
        for cb, on in ((self.cb_pre, live), (self.cb_reboot, live), (self.cb_grub, not live)):
            cb.configure(state="normal" if on else "disabled")

    def pick_dir(self):
        d = filedialog.askdirectory(parent=self, title=_('Zielverzeichnis (Wurzel des Zielsystems)'))
        if d:
            self.mode.set("other")
            self.sync()
            self.target.set(d)

    def target_arg(self):
        if self.mode.get() == "live":
            return "/"
        v = self.target.get().strip()
        if not v:
            messagebox.showwarning(_('Ziel fehlt'), _('Bitte Laufwerk oder Verzeichnis angeben.'), parent=self)
            return None
        v = v.split()[0]
        if v.startswith(("/dev/", "UUID=")) and ns.is_luks(v):
            kids = ns.luks_children(v)
            if kids:
                v = kids[0]
            else:
                while True:
                    pw = simpledialog.askstring(_('Verschlüsselte Partition'),
                                                _('{v} ist verschlüsselt.\nPasswort zum Entsperren (wie beim Starten des Rechners):').format(v=v), show="•", parent=self)
                    if not pw:
                        return None
                    p = subprocess.run([sys.executable, CORE, "unlock", v, "--stdin"], input=pw,
                                       capture_output=True, text=True)
                    if p.returncode == 0:
                        v = p.stdout.strip().splitlines()[-1]
                        self.app.unlocked.append(v)
                        break
                    if p.returncode != 3:      # 3 = falsches Passwort
                        messagebox.showerror(_('Entsperren fehlgeschlagen'), p.stdout + p.stderr, parent=self)
                        return None
                    messagebox.showwarning(_('Falsches Passwort'), _('Das Passwort war falsch.'), parent=self)
            self.target.set(v)
        return v

    def args(self):
        t = self.target_arg()
        if t is None:
            return None
        a = ["--target", t]
        if self.fstab.get():
            a.append("--keep-fstab")
        return a

    def preview(self):
        a = self.args()
        if a is None:
            return
        self.app.run_cli(["diff", self.snap["name"]] + a,
                         lambda rc, lines: self.app.show_output(_('Vorschau – Änderungen bei Wiederherstellung'), lines),
                         msg=_('Berechne Änderungen…'), log=False, capture=True)

    def go(self):
        a = self.args()
        if a is None:
            return
        live = self.mode.get() == "live"
        what = _('das LAUFENDE SYSTEM') if live else a[1]
        if not messagebox.askyesno(
                _('Wirklich wiederherstellen?'),
                _('Snapshot {0} wird auf {what} zurückgespielt.\n\nSystemdateien werden auf den Stand des Snapshots gesetzt, seitdem neu hinzugekommene Dateien werden gelöscht.\n\nFortfahren?').format(self.snap['name'], what=what), icon="warning", parent=self):
            return
        a += ["--yes", "--no-reboot"]
        if live and not self.pre.get():
            a.append("--no-pre-snapshot")
        if not live and self.grub.get():
            a.append("--update-grub")
        reboot = live and self.reboot.get()
        self.destroy()

        def done(rc, lines):
            self.app.refresh()
            if rc != 0:
                return
            if reboot:
                messagebox.showinfo(_('Fertig'), _('Wiederherstellung abgeschlossen. Das System wird jetzt neu gestartet.'))
                subprocess.Popen(["systemctl", "reboot"])
            elif live:
                messagebox.showinfo(_('Fertig'), _('Wiederherstellung abgeschlossen.\nBitte so bald wie möglich neu starten!'))
            else:
                messagebox.showinfo(_('Fertig'), _('Wiederherstellung abgeschlossen.'))
        self.app.run_cli(["restore", self.snap["name"]] + a, done, msg=_('Stelle wieder her…'))


class FilesDialog(tk.Toplevel):
    def __init__(self, app, snap):
        super().__init__(app)
        self.app, self.snap = app, snap
        self.title(_('Dateien aus {0} zurückholen').format(snap['name']))
        self.transient(app)
        f = ttk.Frame(self, padding=14)
        f.pack(fill="both", expand=True)
        ttk.Label(f, text=_('Pfade (einer pro Zeile, absolut – z.B. /etc/pacman.conf oder /home/user/Dokumente):')
                  ).pack(anchor="w")
        self.paths = tk.Text(f, width=80, height=8)
        self.paths.pack(fill="both", expand=True, pady=4)
        br = ttk.Frame(f)
        br.pack(fill="x")
        ttk.Button(br, text=_('Datei im Snapshot wählen…'), command=lambda: self.pick(False)).pack(side="left")
        ttk.Button(br, text=_('Ordner im Snapshot wählen…'), command=lambda: self.pick(True)).pack(side="left", padx=6)

        self.where = tk.StringVar(value="orig")
        ttk.Radiobutton(f, text=_('An den Originalort zurückschreiben'), variable=self.where, value="orig"
                        ).pack(anchor="w", pady=(10, 0))
        r = ttk.Frame(f)
        r.pack(fill="x")
        ttk.Radiobutton(r, text=_('In anderen Ordner kopieren:'), variable=self.where, value="other").pack(side="left")
        self.to = ttk.Entry(r, width=44)
        self.to.pack(side="left", padx=4)
        ttk.Button(r, text="…", width=3, command=self.pick_to).pack(side="left")
        self.delete = tk.BooleanVar(value=False)
        ttk.Checkbutton(f, text=_('Ordner exakt angleichen (seitdem neu hinzugekommene Dateien darin löschen)'),
                        variable=self.delete).pack(anchor="w", pady=4)
        b = ttk.Frame(f)
        b.pack(fill="x", pady=(8, 0))
        ttk.Button(b, text=_('Abbrechen'), command=self.destroy).pack(side="right")
        ttk.Button(b, text=_('Zurückholen'), command=self.go).pack(side="right", padx=6)
        ttk.Button(b, text=_('Vorschau'), command=lambda: self.go(dry=True)).pack(side="left")

    def pick(self, folder):
        base = self.app._ensure_mounted()
        if not base:
            messagebox.showinfo(_('Hinweis'), _('Bei SSH-Zielen bitte Pfade direkt eintragen.'), parent=self)
            return
        root = os.path.join(base, self.snap["name"], "localhost")
        if folder:
            sel = filedialog.askdirectory(parent=self, initialdir=root, mustexist=True)
            sel = [sel] if sel else []
        else:
            sel = list(filedialog.askopenfilenames(parent=self, initialdir=root))
        for p in sel:
            p = os.path.realpath(p)
            if p.startswith(root):
                rel = os.path.relpath(p, root)
                self.paths.insert("end", ("/" if rel == "." else "/" + rel) + "\n")

    def pick_to(self):
        d = filedialog.askdirectory(parent=self)
        if d:
            self.where.set("other")
            self.to.delete(0, "end")
            self.to.insert(0, d)

    def go(self, dry=False):
        paths = [p.strip() for p in self.paths.get("1.0", "end").splitlines() if p.strip()]
        if not paths:
            messagebox.showwarning(_('Keine Pfade'), _('Bitte mindestens einen Pfad angeben.'), parent=self)
            return
        a = ["restore-files", self.snap["name"]] + paths + ["--yes"]
        if self.where.get() == "other":
            if not self.to.get().strip():
                messagebox.showwarning(_('Ziel fehlt'), _('Bitte Zielordner angeben.'), parent=self)
                return
            a += ["--to", self.to.get().strip()]
        if self.delete.get():
            a.append("--delete")
        if dry:
            a.append("--dry-run")
            self.app.run_cli(a, lambda rc, l: self.app.show_output(_('Vorschau'), l),
                             msg=_('Berechne…'), log=False, capture=True)
            return
        if self.where.get() == "orig" and not messagebox.askyesno(
                _('Überschreiben?'), _('Die aktuellen Dateien an diesen Pfaden werden überschrieben. Fortfahren?'),
                parent=self):
            return
        self.destroy()
        self.app.run_cli(a, lambda rc, l: rc == 0 and messagebox.showinfo(_('Fertig'), _('Dateien wiederhergestellt.')),
                         msg=_('Hole Dateien zurück…'))


class SettingsDialog(tk.Toplevel):
    def __init__(self, app):
        super().__init__(app)
        self.app = app
        self.cfg = ns.load_config()
        self.title(_('Einstellungen'))
        self.transient(app)
        # Fenstergröße an Bildschirm anpassen (große Schrift / kleine Bildschirme)
        sw, sh = self.winfo_screenwidth(), self.winfo_screenheight()
        self.geometry(f"{min(820, sw - 40)}x{min(720, sh - 80)}")
        self.minsize(520, 360)
        # Schaltflächen ZUERST unten anordnen – so bleiben sie immer sichtbar
        b = ttk.Frame(self, padding=8)
        b.pack(side="bottom", fill="x")
        ttk.Separator(self).pack(side="bottom", fill="x")
        ttk.Button(b, text=_('Abbrechen'), command=self.destroy).pack(side="right")
        ttk.Button(b, text=_('Speichern'), command=self.save).pack(side="right", padx=6)
        ttk.Button(b, text=_('Speichern & Verbindung testen'), command=lambda: self.save(test=True)).pack(side="left")
        nb = ttk.Notebook(self)
        nb.pack(side="top", fill="both", expand=True, padx=8, pady=8)
        self.v = {}
        nb.add(self._tab_target(nb).outer, text=_('Sicherungsziel'))
        nb.add(self._tab_schedule(nb).outer, text=_('Zeitplan'))
        nb.add(self._tab_files(nb).outer, text=_('Dateien & Ausschlüsse'))
        self.bind("<Return>", lambda e: self.save() if not isinstance(e.widget, tk.Text) else None)

    def _scrollable(self, nb):
        """Rollbarer Inhaltsbereich für einen Reiter."""
        outer = ttk.Frame(nb)
        canvas = tk.Canvas(outer, highlightthickness=0, borderwidth=0,
                           background=ttk.Style().lookup("TFrame", "background"))
        sb = ttk.Scrollbar(outer, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)
        inner = ttk.Frame(canvas, padding=12)
        win = canvas.create_window((0, 0), window=inner, anchor="nw")
        inner.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>", lambda e: canvas.itemconfigure(win, width=e.width))

        if not hasattr(self, "_canvases"):
            self._canvases = {}
            for ev in ("<Button-4>", "<Button-5>", "<MouseWheel>"):
                self.bind_all(ev, self._wheel, add="+")
            self.bind("<Destroy>", lambda e: e.widget is self and [
                self.unbind_all(ev) for ev in ("<Button-4>", "<Button-5>", "<MouseWheel>")])
        self._canvases[str(canvas)] = (canvas, inner)
        inner.outer = outer
        return inner

    def _wheel(self, e):
        try:
            w = self.winfo_containing(e.x_root, e.y_root)
        except (KeyError, tk.TclError):
            return
        while w is not None:
            if isinstance(w, (tk.Text, ttk.Combobox, ttk.Spinbox)):
                return  # eigene Scroll-Behandlung
            if str(w) in self._canvases:
                canvas, inner = self._canvases[str(w)]
                if inner.winfo_height() > canvas.winfo_height():
                    canvas.yview_scroll(-1 if (e.num == 4 or getattr(e, "delta", 0) > 0) else 1, "units")
                return
            w = w.master

    # --- Widgets-Hilfen
    def entry(self, parent, key, label, value, row, show=None, width=40):
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", pady=3, padx=(0, 8))
        var = tk.StringVar(value=str(value if value is not None else ""))
        e = ttk.Entry(parent, textvariable=var, width=width, show=show)
        e.grid(row=row, column=1, sticky="we", pady=3)
        self.v[key] = var
        return e

    def _tab_target(self, nb):
        t = self.cfg["target"]
        f = self._scrollable(nb)
        f.columnconfigure(1, weight=1)
        self.entry(f, "machine_name", _('Name dieses Rechners:'), self.cfg["machine_name"], 0)
        self.v["rescue"] = tk.BooleanVar(value=bool(self.cfg.get("rescue_mode")))
        ttk.Checkbutton(f, text=_('Rettungssystem (USB-Stick: das Stick-System selbst wird nie gesichert; Name muss wie beim Rechner lauten)'), variable=self.v["rescue"]
                        ).grid(row=4, column=0, columnspan=3, sticky="w", pady=(10, 0))
        ttk.Label(f, text=_('Zieltyp:')).grid(row=1, column=0, sticky="w")
        self.type_cb = ttk.Combobox(f, state="readonly", values=[l for _x, l in TYPE_LABELS], width=38)
        self.type_cb.current([k for k, _x in TYPE_LABELS].index(t["type"]))
        self.type_cb.grid(row=1, column=1, sticky="w", pady=3)
        self.type_cb.bind("<<ComboboxSelected>>", lambda e: self.show_type())

        self.frames = {}
        holder = ttk.Frame(f)
        holder.grid(row=2, column=0, columnspan=3, sticky="we", pady=8)
        holder.columnconfigure(0, weight=1)
        cred = ns.read_smb_credentials()

        fs = ttk.LabelFrame(holder, text="SMB / CIFS", padding=8)
        fs.columnconfigure(1, weight=1)
        self.entry(fs, "server_smb", _('Server (Name/IP):'), t["server"], 0)
        self.entry(fs, "share", _('Freigabe (z.B. backup):'), t["share"], 1)
        self.entry(fs, "smb_user", _('Benutzer (leer = Gast):'), cred.get("username", ""), 2)
        self.entry(fs, "smb_pass", _('Passwort:'), "", 3, show="•")
        ttk.Label(fs, text=_('(leer lassen = gespeichertes Passwort behalten)'), foreground="#777"
                  ).grid(row=4, column=1, sticky="w")
        self.entry(fs, "smb_domain", _('Domäne (optional):'), cred.get("domain", ""), 5)
        self.entry(fs, "smb_version", _("SMB-Version:"), t["smb_version"], 6, width=8)
        self.frames["smb"] = fs

        fn = ttk.LabelFrame(holder, text="NFS", padding=8)
        fn.columnconfigure(1, weight=1)
        self.entry(fn, "server_nfs", _('Server (Name/IP):'), t["server"], 0)
        self.entry(fn, "export", _('Export (z.B. /volume1/backup):'), t["export"], 1)
        self.frames["nfs"] = fn

        fh = ttk.LabelFrame(holder, text=_('SSH (rsync über ssh)'), padding=8)
        fh.columnconfigure(1, weight=1)
        self.entry(fh, "server_ssh", _('Server (Name/IP):'), t["server"], 0)
        self.entry(fh, "ssh_user", _('Benutzer:'), t["ssh_user"], 1)
        self.entry(fh, "ssh_port", _('Port:'), t["ssh_port"], 2, width=8)
        self.entry(fh, "ssh_path", _('Pfad auf dem Server:'), t["ssh_path"], 3)
        self.entry(fh, "ssh_key", _('Schlüsseldatei:'), t["ssh_key"], 4)
        self.v["ssh_fake_super"] = tk.BooleanVar(value=t["ssh_fake_super"])
        ttk.Checkbutton(fh, text=_('Server-Benutzer ist nicht root (Besitzer/Rechte per --fake-super speichern)'),
                        variable=self.v["ssh_fake_super"]).grid(row=5, column=0, columnspan=2, sticky="w")
        ttk.Button(fh, text=_('SSH-Schlüssel erzeugen/anzeigen…'), command=self.ssh_key
                   ).grid(row=6, column=0, columnspan=2, sticky="w", pady=4)
        self.frames["ssh"] = fh

        fl = ttk.LabelFrame(holder, text=_('Lokales Laufwerk'), padding=8)
        fl.columnconfigure(1, weight=1)
        ttk.Label(fl, text=_('Pfad, Gerät oder UUID=…:')).grid(row=0, column=0, sticky="w")
        self.devs = lsblk_parts()
        vals = [f"UUID={d['uuid']}" for d in self.devs if d.get("uuid")]
        self.v["path"] = tk.StringVar(value=t["path"])
        cb = ttk.Combobox(fl, textvariable=self.v["path"], values=vals, width=40)
        cb.grid(row=0, column=1, sticky="we")
        ttk.Button(fl, text=_('Ordner…'), command=lambda: self.v["path"].set(
            filedialog.askdirectory(parent=self) or self.v["path"].get())).grid(row=0, column=2, padx=4)
        info = "\n".join(f"UUID={d['uuid']}  →  {dev_label(d)}" for d in self.devs if d.get("uuid"))
        ttk.Label(fl, text=info or "", foreground="#555", font=("monospace", 8), justify="left"
                  ).grid(row=1, column=0, columnspan=3, sticky="w", pady=4)
        self.frames["local"] = fl

        adv = ttk.LabelFrame(f, text=_('Speicherformat'), padding=8)
        adv.grid(row=3, column=0, columnspan=3, sticky="we")
        adv.columnconfigure(1, weight=1)
        ttk.Label(adv, text=_('Ablage:')).grid(row=0, column=0, sticky="w")
        self.cont_cb = ttk.Combobox(adv, state="readonly", values=[l for _x, l in CONTAINER_LABELS], width=30)
        self.cont_cb.current([k for k, _x in CONTAINER_LABELS].index(t.get("container", "auto")))
        self.cont_cb.grid(row=0, column=1, sticky="w", pady=3)
        self.entry(adv, "image_size_gb", _('Max. Image-Größe (GiB):'), t["image_size_gb"], 1, width=8)
        self.v["encrypt"] = tk.BooleanVar(value=t["encrypt"])
        ttk.Checkbutton(adv, text=_('Image verschlüsseln (LUKS2)'), variable=self.v["encrypt"]
                        ).grid(row=2, column=0, columnspan=2, sticky="w")
        self.entry(adv, "key_file", _('Schlüsseldatei:'), t["key_file"], 3)
        self.entry(adv, "mount_options", _('Zusätzliche Mount-Optionen:'), t["mount_options"], 4)
        ttk.Label(adv, text=_('Automatisch: SMB/NFS/FAT/NTFS → Image-Container (erhält Rechte, Besitzer, Hardlinks); ext4/btrfs/SSH → direkt.'), foreground="#555", wraplength=620
                  ).grid(row=5, column=0, columnspan=2, sticky="w", pady=(4, 0))
        self.show_type()
        return f

    def cur_type(self):
        return TYPE_LABELS[self.type_cb.current()][0]

    def show_type(self):
        for k, fr in self.frames.items():
            fr.grid_forget()
        self.frames[self.cur_type()].grid(row=0, column=0, sticky="we")

    def ssh_key(self):
        key = self.v["ssh_key"].get() or os.path.join(ns.ETC_DIR, "id_ed25519")
        if not os.path.exists(key):
            os.makedirs(os.path.dirname(key), mode=0o700, exist_ok=True)
            subprocess.run(["ssh-keygen", "-t", "ed25519", "-N", "", "-C", f"netshift@{ns.hostname()}",
                            "-f", key], capture_output=True)
        try:
            pub = open(key + ".pub").read().strip()
        except OSError as e:
            messagebox.showerror(_('Fehler'), str(e), parent=self)
            return
        host = f"{self.v['ssh_user'].get()}@{self.v['server_ssh'].get()}"
        w = tk.Toplevel(self)
        w.title(_('SSH-Schlüssel'))
        t = tk.Text(w, width=90, height=9, wrap="word")
        t.pack(padx=8, pady=8)
        t.insert("end", _('Diesen öffentlichen Schlüssel auf dem Server in ~/.ssh/authorized_keys eintragen:\n\n{pub}\n\nOder im Terminal einmalig ausführen (fragt das Server-Passwort ab):\n\nsudo ssh-copy-id -i {key}.pub -p {0} {host}').format(self.v['ssh_port'].get(), pub=pub, key=key, host=host))
        ttk.Button(w, text=_('Schließen'), command=w.destroy).pack(pady=(0, 8))

    def _tab_schedule(self, nb):
        f = self._scrollable(nb)
        ttk.Label(f, text=_('Automatische Snapshots'), font=("TkDefaultFont", 10, "bold")).grid(row=0, column=0, sticky="w")
        ttk.Label(f, text=_('Behalten')).grid(row=0, column=1)
        for i, (lvl, label) in enumerate(LEVEL_LABELS, 1):
            s = self.cfg["schedule"][lvl]
            self.v[f"en_{lvl}"] = tk.BooleanVar(value=s["enabled"])
            self.v[f"keep_{lvl}"] = tk.StringVar(value=str(s["keep"]))
            ttk.Checkbutton(f, text=label, variable=self.v[f"en_{lvl}"]).grid(row=i, column=0, sticky="w", pady=2)
            ttk.Spinbox(f, from_=1, to=99, width=5, textvariable=self.v[f"keep_{lvl}"]).grid(row=i, column=1, padx=8)
        ph = self.cfg["pacman_hook"]
        ttk.Separator(f).grid(row=7, column=0, columnspan=3, sticky="we", pady=12)
        ttk.Label(f, text=_('Paketverwaltung (pacman/pamac)'), font=("TkDefaultFont", 10, "bold")
                  ).grid(row=8, column=0, sticky="w")
        self.v["ph_en"] = tk.BooleanVar(value=ph["enabled"])
        self.v["ph_keep"] = tk.StringVar(value=str(ph["keep"]))
        self.v["ph_int"] = tk.StringVar(value=str(ph["min_interval_minutes"]))
        self.v["ph_abort"] = tk.BooleanVar(value=ph["abort_on_fail"])
        ttk.Checkbutton(f, text=_('Snapshot vor jeder Installation/Aktualisierung'), variable=self.v["ph_en"]
                        ).grid(row=9, column=0, sticky="w")
        ttk.Spinbox(f, from_=1, to=99, width=5, textvariable=self.v["ph_keep"]).grid(row=9, column=1)
        ttk.Label(f, text=_('Mindestabstand (Minuten):')).grid(row=10, column=0, sticky="w", padx=(22, 0))
        ttk.Spinbox(f, from_=0, to=1440, width=5, textvariable=self.v["ph_int"]).grid(row=10, column=1)
        ttk.Checkbutton(f, text=_('Update abbrechen, wenn der Snapshot fehlschlägt'), variable=self.v["ph_abort"]
                        ).grid(row=11, column=0, sticky="w", padx=(22, 0))
        ttk.Label(f, text=_('\nManuelle Snapshots und Sicherheits-Snapshots vor einer Wiederherstellung werden nie automatisch gelöscht.\nGeplante Snapshots werden nur erstellt, wenn das Ziel erreichbar ist; verpasste Termine werden nachgeholt.'),
                  foreground="#555", wraplength=640, justify="left").grid(row=12, column=0, columnspan=3, sticky="w")
        return f

    def _tab_files(self, nb):
        f = self._scrollable(nb)
        self.v["include_home"] = tk.BooleanVar(value=self.cfg["include_home"])
        self.v["excl_cache"] = tk.BooleanVar(value=self.cfg["exclude_pacman_cache"])
        ttk.Checkbutton(f, text=_('Home-Verzeichnisse (/home, /root) mitsichern'), variable=self.v["include_home"]
                        ).pack(anchor="w")
        ttk.Label(f, text=_('Achtung: Wenn aktiviert, setzt eine System-Wiederherstellung auch persönliche Dateien zurück.'),
                  foreground="#a04000").pack(anchor="w", padx=(22, 0))
        ttk.Checkbutton(f, text=_('Pacman-Paketcache (/var/cache/pacman/pkg) auslassen'), variable=self.v["excl_cache"]
                        ).pack(anchor="w", pady=(6, 0))
        r = ttk.Frame(f)
        r.pack(anchor="w", pady=6)
        ttk.Label(r, text=_('Mindestens freier Speicher auf dem Ziel (GiB):')).pack(side="left")
        self.v["min_free"] = tk.StringVar(value=str(self.cfg["min_free_gb"]))
        ttk.Spinbox(r, from_=0, to=1000, width=6, textvariable=self.v["min_free"]).pack(side="left", padx=6)
        ttk.Label(f, text=_('Ausschlüsse (rsync-Muster, eines pro Zeile; z.B. /opt/spiele/*  oder  /home/*/Videos/*):')
                  ).pack(anchor="w", pady=(8, 2))
        self.excl = tk.Text(f, height=12, font=("monospace", 9))
        self.excl.pack(fill="both", expand=True)
        self.excl.insert("end", "\n".join(self.cfg["exclude"]))
        ttk.Label(f, text=_('Immer ausgeschlossen: ') + ", ".join(ns.FORCED_EXCLUDES) +
                  _(' sowie eingehängte Netzwerk- und Temp-Dateisysteme.'), foreground="#555", wraplength=640
                  ).pack(anchor="w", pady=4)
        return f

    def save(self, test=False):
        c = self.cfg
        t = c["target"]
        typ = self.cur_type()
        g = lambda k: self.v[k].get().strip() if isinstance(self.v[k].get(), str) else self.v[k].get()
        try:
            c["machine_name"] = ns.safe_name(g("machine_name") or ns.hostname())
            c["rescue_mode"] = bool(self.v["rescue"].get())
            t["type"] = typ
            if typ in ("smb", "nfs", "ssh"):
                t["server"] = g("server_" + typ)
            t["share"] = g("share")
            t["export"] = g("export")
            t["smb_version"] = g("smb_version")
            t["ssh_user"] = g("ssh_user")
            t["ssh_port"] = int(g("ssh_port") or 22)
            t["ssh_path"] = g("ssh_path")
            t["ssh_key"] = g("ssh_key")
            t["ssh_fake_super"] = bool(self.v["ssh_fake_super"].get())
            t["path"] = g("path")
            t["container"] = CONTAINER_LABELS[self.cont_cb.current()][0]
            t["image_size_gb"] = int(g("image_size_gb") or 100)
            t["encrypt"] = bool(self.v["encrypt"].get())
            t["key_file"] = g("key_file")
            t["mount_options"] = g("mount_options")
            for lvl, _x in LEVEL_LABELS:
                c["schedule"][lvl] = {"enabled": bool(self.v[f"en_{lvl}"].get()),
                                      "keep": max(1, int(self.v[f"keep_{lvl}"].get()))}
            c["pacman_hook"] = {"enabled": bool(self.v["ph_en"].get()), "keep": max(1, int(self.v["ph_keep"].get())),
                                "min_interval_minutes": int(self.v["ph_int"].get()),
                                "abort_on_fail": bool(self.v["ph_abort"].get())}
            c["include_home"] = bool(self.v["include_home"].get())
            c["exclude_pacman_cache"] = bool(self.v["excl_cache"].get())
            c["min_free_gb"] = float(self.v["min_free"].get())
            c["exclude"] = [l.strip() for l in self.excl.get("1.0", "end").splitlines() if l.strip()]
        except ValueError as e:
            messagebox.showerror(_('Ungültige Eingabe'), _('Bitte Zahlenfelder prüfen.\n{e}').format(e=e), parent=self)
            return
        try:
            ns.Target(c).validate()
        except ns.NetshiftError as e:
            messagebox.showerror(_('Unvollständig'), str(e).split(" 'sudo netshift setup'")[0], parent=self)
            return
        if typ == "smb":
            user = g("smb_user")
            if user:
                old = ns.read_smb_credentials()
                pw = self.v["smb_pass"].get() or (old.get("password", "") if old.get("username") == user else "")
                ns.write_smb_credentials(user, pw, g("smb_domain"))
            elif os.path.exists(ns.cred_path()):
                os.remove(ns.cred_path())
        ns.save_config(c)
        try:
            out = subprocess.run([sys.executable, CORE, "schedule-apply"], capture_output=True, text=True)
            self.app.write_log(out.stdout.strip())
        except OSError:
            pass
        self.destroy()
        if test:
            self.app.run_cli(["test"], lambda rc, l: (rc == 0 and messagebox.showinfo(
                _('Verbindung'), "".join(x[9:] for x in l if x.startswith("@@RESULT ")) or "OK"), self.app.refresh()),
                msg=_('Teste Verbindung…'))
        else:
            self.app.refresh()


def main():
    if os.geteuid() != 0:
        os.execvp("pkexec", ["pkexec", os.path.realpath(__file__)] + sys.argv[1:])
    App().mainloop()


if __name__ == "__main__":
    main()
