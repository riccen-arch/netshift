#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""netshift – moderne Oberfläche (GTK4 + libadwaita). Läuft als root (über pkexec)."""

import os
import sys

# ---------------------------------------------------------------------------
# pkexec entfernt Umgebungsvariablen – der Starter übergibt Display & Farbschema
# als Argumente. Muss VOR dem Import von Gtk passieren.
COLOR_SCHEME = "default"
_rest = []
for _a in sys.argv[1:]:
    if _a.startswith("--wayland-display="):
        os.environ["WAYLAND_DISPLAY"] = _a.split("=", 1)[1]
    elif _a.startswith("--display="):
        os.environ["DISPLAY"] = _a.split("=", 1)[1]
    elif _a.startswith("--scale="):
        os.environ["GDK_SCALE"] = _a.split("=", 1)[1]
    elif _a.startswith("--color-scheme="):
        COLOR_SCHEME = _a.split("=", 1)[1]
    elif _a.startswith("--lang="):
        os.environ["NETSHIFT_LANG"] = _a.split("=", 1)[1]
    else:
        _rest.append(_a)
sys.argv[1:] = _rest
if os.geteuid() == 0:
    # Eigenes Laufzeitverzeichnis für root (nie in /run/user/<uid> des Benutzers schreiben)
    _rt = "/run/netshift/xdg"
    os.makedirs(_rt, mode=0o700, exist_ok=True)
    os.environ["XDG_RUNTIME_DIR"] = _rt
    os.environ.setdefault("GSETTINGS_BACKEND", "memory")
    os.environ.setdefault("GTK_A11Y", "none")

import datetime as dt
import importlib.util
import json
import pwd
import re
import shutil
import signal
import subprocess
import threading

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, Gio, GLib, Gtk, Pango  # noqa: E402

HERE = os.path.dirname(os.path.realpath(__file__))
CORE = next((p for p in (os.path.join(HERE, "netshift.py"), "/usr/lib/netshift/netshift.py")
             if os.path.exists(p)), None)
if CORE is None:
    sys.exit("netshift.py not found")
_spec = importlib.util.spec_from_file_location("netshift_core", CORE)
ns = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ns)
_ = ns._


def fmt_int(n):
    s = f"{int(n):,}"
    return s.replace(",", ".") if ns.LANG == "de" else s

APP_ID = "org.netshift.Netshift"
TYPES = [("smb", "SMB / NAS"), ("nfs", "NFS"), ("ssh", _('SSH-Server')), ("local", _('Lokales Laufwerk'))]
CONTAINERS = [("auto", _('Automatisch (empfohlen)')), ("image", _('Image-Container (ext4)')),
              ("direct", _('Direkt im Dateisystem'))]
SMB_VERSIONS = ["3.1.1", "3.0", "2.1", "2.0", "1.0"]
LEVELS = [("boot", _('Beim Systemstart'), "system-reboot-symbolic"),
          ("hourly", _('Stündlich'), "alarm-symbolic"),
          ("daily", _('Täglich'), "x-office-calendar-symbolic"),
          ("weekly", _('Wöchentlich'), "x-office-calendar-symbolic"),
          ("monthly", _('Monatlich'), "x-office-calendar-symbolic")]
TAG_STYLE = {"O": (_('Manuell'), "accent"), "B": (_('Boot'), "success"), "H": (_('Stündlich'), "success"),
             "D": (_('Täglich'), "success"), "W": (_('Wöchentlich'), "success"), "M": (_('Monatlich'), "success"),
             "P": (_('Update'), "warning"), "R": (_('Vor Restore'), "neutral")}
TAG_ICON = {"O": "camera-photo-symbolic", "P": "system-software-install-symbolic",
            "R": "edit-undo-symbolic", "B": "system-reboot-symbolic"}
WD = [_("Mo."), _("Di."), _("Mi."), _("Do."), _("Fr."), _("Sa."), _("So.")]
MON = [_("Jan."), _("Feb."), _("März"), _("Apr."), _("Mai"), _("Juni"), _("Juli"), _("Aug."), _("Sept."),
       _("Okt."), _("Nov."), _("Dez.")]

CSS = """
.pill { padding: 1px 9px; border-radius: 999px; font-size: 0.82em; font-weight: 600; }
.pill.accent  { background: alpha(@accent_bg_color, 0.16);  color: @accent_color; }
.pill.success { background: alpha(@success_bg_color, 0.16); color: @success_color; }
.pill.warning { background: alpha(@warning_bg_color, 0.18); color: @warning_color; }
.pill.neutral { background: alpha(@window_fg_color, 0.09);  color: alpha(@window_fg_color, 0.75); }
.hero { padding: 20px 22px; }
.hero-icon { background: alpha(@accent_bg_color, 0.14); color: @accent_color;
             border-radius: 14px; padding: 12px; }
.hero-title { font-size: 1.25em; font-weight: 800; }
.stat-value { font-size: 1.7em; font-weight: 800; }
.stat-new     { color: @success_color; }
.stat-changed { color: @accent_color; }
.stat-deleted { color: @error_color; }
.op-panel { padding: 10px 14px 12px 14px; }
.op-title { font-weight: 700; }
.problem { padding: 12px 16px; }
.problem.error   { background: alpha(@error_bg_color, 0.10); }
.problem.warning { background: alpha(@warning_bg_color, 0.12); }
.problem-title { font-weight: 700; }
.mono { font-family: monospace; font-size: 0.9em; }
.change-icon-new { color: @success_color; }
.change-icon-changed { color: @accent_color; }
.change-icon-del { color: @error_color; }
levelbar block.filled { background: @accent_bg_color; }
levelbar.warn block.filled { background: @warning_bg_color; }
"""


# --------------------------------------------------------------------------- Hilfen

def fmt_when(epoch, with_time=True):
    d = dt.datetime.fromtimestamp(epoch)
    today = dt.date.today()
    t = d.strftime("%H:%M")
    if d.date() == today:
        return _('Heute, {t}').format(t=t) if with_time else _('Heute')
    if d.date() == today - dt.timedelta(days=1):
        return _('Gestern, {t}').format(t=t) if with_time else _('Gestern')
    s = _("{wd} {day}. {mon}").format(wd=WD[d.weekday()], day=d.day, mon=MON[d.month - 1])
    if d.year != today.year:
        s += f" {d.year}"
    return f"{s} · {t}" if with_time else s


def fmt_ago(epoch):
    s = int(dt.datetime.now().timestamp() - epoch)
    if s < 90:
        return _('gerade eben')
    if s < 3600:
        return _('vor {0} Minuten').format(s // 60)
    if s < 2 * 3600:
        return _('vor einer Stunde')
    if s < 86400:
        return _('vor {0} Stunden').format(s // 3600)
    if s < 2 * 86400:
        return _("gestern")
    return _('vor {0} Tagen').format(s // 86400)


def period_of(epoch):
    d = dt.datetime.fromtimestamp(epoch).date()
    days = (dt.date.today() - d).days
    if days <= 0:
        return 0, _('Heute')
    if days == 1:
        return 1, _('Gestern')
    if days < 7:
        return 2, _('Letzte 7 Tage')
    if days < 31:
        return 3, _('Letzte 30 Tage')
    return 4, _('Älter')


def lsblk_parts():
    try:
        out = subprocess.run(["lsblk", "-J", "-o", "PATH,UUID,FSTYPE,SIZE,LABEL,MOUNTPOINT,TYPE,MODEL"],
                             capture_output=True, text=True).stdout
        devs = []

        def walk(lst, parent=None):
            for d in lst:
                kids = d.get("children") or []
                if d.get("fstype") == "crypto_LUKS":
                    if not any(k.get("type") == "crypt" for k in kids):
                        devs.append(dict(d, locked=True))      # verschlossen → per Klick entsperren
                    walk(kids, d)
                    continue
                if d.get("fstype") and d.get("fstype") not in ("swap", "LVM2_member", "squashfs"):
                    if d.get("type") == "crypt" and parent is not None:
                        d = dict(d, unlocked_from=parent.get("path"), size=d.get("size") or parent.get("size"))
                    devs.append(d)
                walk(kids, d)
        walk(json.loads(out).get("blockdevices", []))
        return devs
    except Exception:
        return []


def dev_subtitle(d):
    if d.get("locked"):
        return _('verschlüsselt · {0} · zum Entsperren anklicken').format(d.get('size') or '')
    parts = [d.get("fstype") or "", d.get("size") or ""]
    if d.get("unlocked_from"):
        parts.append(_('entsperrt von {0}').format(d['unlocked_from']))
    if d.get("label"):
        parts.append(d["label"])
    if d.get("mountpoint"):
        parts.append(_('eingehängt: ') + d["mountpoint"])
    return " · ".join(p for p in parts if p)


def invoking_user():
    uid = os.environ.get("PKEXEC_UID") or os.environ.get("SUDO_UID")
    try:
        return pwd.getpwuid(int(uid)) if uid else None
    except (KeyError, ValueError):
        return None


def open_as_user(path):
    u = invoking_user()
    if not u or not shutil.which("xdg-open"):
        return False
    rt = f"/run/user/{u.pw_uid}"
    env = [f"XDG_RUNTIME_DIR={rt}", f"DBUS_SESSION_BUS_ADDRESS=unix:path={rt}/bus"]
    if os.environ.get("DISPLAY"):
        env.append(f"DISPLAY={os.environ['DISPLAY']}")
    wd = os.environ.get("WAYLAND_DISPLAY", "")
    if wd:
        env.append(f"WAYLAND_DISPLAY={os.path.basename(wd)}")
    try:
        subprocess.Popen(["runuser", "-u", u.pw_name, "--", "env"] + env + ["xdg-open", path],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        return True
    except OSError:
        return False


ITEM_RE = re.compile(r"^(\*deleting|[<>ch.*][fdLDS][.+cstpoguaxnTP?]{7,9})\s+(.*)$")


def classify(line):
    """rsync --itemize-changes → (art, pfad) mit art in new/changed/del/attr."""
    m = ITEM_RE.match(line.rstrip("\n"))
    if not m:
        return None
    code, path = m.group(1), m.group(2)
    path = "/" + path.split(" => ")[0].split(" -> ")[0].rstrip("/")
    if code.startswith("*deleting"):
        return "del", path
    if "+++++++" in code:
        return "new", path
    if code[0] in "<>h" or (code[0] == "c" and code[1] in "fLDS"):
        return "changed", path
    return "attr", path


def pill(text, style):
    lbl = Gtk.Label(label=text, valign=Gtk.Align.CENTER)
    lbl.add_css_class("pill")
    lbl.add_css_class(style)
    return lbl


def icon_button(icon, tooltip, cb, *css):
    b = Gtk.Button(icon_name=icon, tooltip_text=tooltip, valign=Gtk.Align.CENTER)
    for c in css or ("flat",):
        b.add_css_class(c)
    b.connect("clicked", lambda *_x: cb())
    return b


# --------------------------------------------------------------------------- Prozesse

class Job:
    """Startet netshift im Hintergrund und liefert Zeilen/Ergebnis im GTK-Thread."""

    def __init__(self, args, on_line=None, on_done=None, input=None):
        env = dict(os.environ, NETSHIFT_GUI="1", PYTHONUNBUFFERED="1")
        self.lines = []
        self.proc = subprocess.Popen([sys.executable, CORE] + args, stdout=subprocess.PIPE,
                                     stderr=subprocess.STDOUT, text=True, bufsize=1, env=env,
                                     start_new_session=True,
                                     stdin=subprocess.PIPE if input is not None else subprocess.DEVNULL)
        if input is not None:
            self.proc.stdin.write(input)
            self.proc.stdin.close()
        self.on_line, self.on_done = on_line, on_done
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self):
        for line in self.proc.stdout:
            self.lines.append(line)
            if self.on_line:
                GLib.idle_add(self.on_line, line)
        rc = self.proc.wait()
        if self.on_done:
            GLib.idle_add(self.on_done, rc, self.lines)

    def cancel(self):
        try:
            os.killpg(self.proc.pid, signal.SIGINT)   # rsync + netshift sauber abbrechen
        except ProcessLookupError:
            pass

    @staticmethod
    def error_of(lines):
        err = [l[len("@@ERROR "):].rstrip("\n") for l in lines if l.startswith("@@ERROR ")]
        if err:
            return "\n".join(err).strip()
        plain = [l.strip() for l in lines if l.strip() and not l.startswith("@@")]
        return plain[-1] if plain else _('Unbekannter Fehler')

    @staticmethod
    def result_of(lines):
        return next((l[len("@@RESULT "):].strip() for l in lines if l.startswith("@@RESULT ")), "")


# --------------------------------------------------------------------------- Fortschritt

class OperationPanel(Gtk.Revealer):
    """Unten einfahrende Leiste mit Fortschritt, Details und Abbrechen."""

    def __init__(self, win):
        super().__init__(transition_type=Gtk.RevealerTransitionType.SLIDE_UP, transition_duration=250)
        self.win = win
        self.job = None
        self._pulse = 0
        self._frac = 0.0
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        box.add_css_class("op-panel")
        top = Gtk.Box(spacing=12)
        self.icon_stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE)
        self.spinner = Gtk.Spinner(spinning=True, width_request=20, height_request=20)
        self.icon_stack.add_named(self.spinner, "busy")
        self.icon_stack.add_named(Gtk.Image(icon_name="emblem-ok-symbolic", css_classes=["success"]), "ok")
        self.icon_stack.add_named(Gtk.Image(icon_name="dialog-error-symbolic", css_classes=["error"]), "error")
        top.append(self.icon_stack)
        tb = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, hexpand=True)
        self.title = Gtk.Label(xalign=0, css_classes=["op-title"])
        self.sub = Gtk.Label(xalign=0, css_classes=["dim-label", "caption"], ellipsize=Pango.EllipsizeMode.END)
        tb.append(self.title)
        tb.append(self.sub)
        top.append(tb)
        self.btn_details = Gtk.ToggleButton(label=_('Details'), valign=Gtk.Align.CENTER, css_classes=["flat"])
        self.btn_cancel = Gtk.Button(label=_('Abbrechen'), valign=Gtk.Align.CENTER)
        self.btn_close = Gtk.Button(icon_name="window-close-symbolic", valign=Gtk.Align.CENTER,
                                    css_classes=["flat", "circular"], visible=False)
        self.btn_cancel.connect("clicked", self._cancel)
        self.btn_close.connect("clicked", lambda *_x: self.set_reveal_child(False))
        for b in (self.btn_details, self.btn_cancel, self.btn_close):
            top.append(b)
        box.append(top)
        self.bar = Gtk.ProgressBar()
        box.append(self.bar)
        self.log = Gtk.TextView(editable=False, cursor_visible=False, monospace=True,
                                wrap_mode=Gtk.WrapMode.WORD_CHAR, top_margin=6, bottom_margin=6,
                                left_margin=8, right_margin=8)
        sc = Gtk.ScrolledWindow(min_content_height=150, child=self.log, css_classes=["card"])
        self.log_rev = Gtk.Revealer(child=sc, transition_type=Gtk.RevealerTransitionType.SLIDE_DOWN)
        self.btn_details.bind_property("active", self.log_rev, "reveal-child")
        box.append(self.log_rev)
        self.set_child(box)
        target = Adw.CallbackAnimationTarget.new(lambda v: self.bar.set_fraction(v))
        self.anim = Adw.TimedAnimation.new(self.bar, 0, 1, 450, target)
        self.anim.set_easing(Adw.Easing.EASE_OUT_CUBIC)

    # --- Fortschritt
    def _set_fraction(self, f):
        self.anim.pause()
        self.anim.set_value_from(self.bar.get_fraction())
        self.anim.set_value_to(f)
        self.anim.play()

    def _start_pulse(self):
        if not self._pulse:
            self._pulse = GLib.timeout_add(90, lambda: (self.bar.pulse(), True)[1])

    def _stop_pulse(self):
        if self._pulse:
            GLib.source_remove(self._pulse)
            self._pulse = 0

    def append_log(self, text):
        buf = self.log.get_buffer()
        buf.insert(buf.get_end_iter(), text if text.endswith("\n") else text + "\n")
        if buf.get_line_count() > 4000:
            buf.delete(buf.get_start_iter(), buf.get_iter_at_line(1000)[1])
        self.log.scroll_to_mark(buf.get_insert(), 0, False, 0, 0)

    def start(self, title, args, on_done=None, cancellable=True):
        self.log.get_buffer().set_text("")
        self.title.set_label(title)
        self.sub.set_label(_('Verbinde mit dem Sicherungsziel…'))
        self.icon_stack.set_visible_child_name("busy")
        self.spinner.start()
        self.btn_cancel.set_visible(cancellable)
        self.btn_cancel.set_sensitive(True)
        self.btn_close.set_visible(False)
        self.btn_details.set_active(False)
        self.bar.set_fraction(0)
        self._start_pulse()
        self.set_reveal_child(True)
        self._done_cb = on_done
        self.job = Job(args, self._line, self._done)
        return self.job

    def _line(self, line):
        if line.startswith("@@PHASE"):
            self.sub.set_label(line[7:].strip())
            self._start_pulse()
        elif line.startswith("@@PROGRESS"):
            p = line.split()
            try:
                pct = int(p[1])
            except (IndexError, ValueError):
                return
            self._stop_pulse()
            self._set_fraction(pct / 100)
            if pct >= 100:
                self.sub.set_label(_('Wird abgeschlossen…'))
            else:
                self.sub.set_label(f"{pct} %  ·  {p[2]}  ·  noch {p[3]}")
        elif line.startswith("@@STEP "):
            msg = line[7:].strip()
            self.append_log(msg)
            self.sub.set_label(msg[:120])
        elif line.startswith("@@ERROR "):
            self.append_log("✖ " + line[8:].rstrip())
        elif line.startswith("@@WARN "):
            self.append_log("⚠ " + line[7:].rstrip())
        elif line.startswith("@@RESULT "):
            self.append_log(line[9:].rstrip())
        else:
            self.append_log(line)

    def _done(self, rc, lines):
        self._stop_pulse()
        self.spinner.stop()
        self.btn_cancel.set_visible(False)
        self.job = None
        ok = rc == 0
        self.icon_stack.set_visible_child_name("ok" if ok else "error")
        if ok:
            self._set_fraction(1.0)
            self.sub.set_label(_('Abgeschlossen'))
            GLib.timeout_add(1800, lambda: (self.set_reveal_child(False), False)[1])
        else:
            self.sub.set_label(Job.error_of(lines).splitlines()[0] if rc != 130 else _('Abgebrochen'))
            self.btn_close.set_visible(True)
            if rc != 130:
                self.btn_details.set_active(True)
        if self._done_cb:
            self._done_cb(rc, lines)

    def _cancel(self, *_x):
        if self.job:
            self.btn_cancel.set_sensitive(False)
            self.sub.set_label(_('Wird abgebrochen…'))
            self.job.cancel()


# --------------------------------------------------------------------------- Hauptfenster

class MainWindow(Adw.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app, title="netshift", default_width=920, default_height=700)
        self.set_size_request(380, 480)
        self.snaps = []
        self.listing = None
        self.mounted_by_us = False
        self.unlocked = []          # von netshift entsperrte Partitionen
        self.list_job = None
        self._build()
        self._actions()
        GLib.idle_add(self.refresh)

    # ---- Aufbau
    def _build(self):
        self.toasts = Adw.ToastOverlay()
        tv = Adw.ToolbarView()
        hb = Adw.HeaderBar()
        self.wtitle = Adw.WindowTitle(title="netshift", subtitle=_('System-Snapshots'))
        hb.set_title_widget(self.wtitle)
        self.btn_create = Gtk.Button(child=Adw.ButtonContent(icon_name="list-add-symbolic", label=_('Snapshot erstellen')),
                                     css_classes=["suggested-action"], action_name="win.create",
                                     tooltip_text=_('Neuen Snapshot erstellen (Strg+N)'))
        hb.pack_start(self.btn_create)
        menu = Gio.Menu()
        menu.append(_('Einstellungen'), "win.settings")
        menu.append(_('Aktualisieren'), "win.refresh")
        sec = Gio.Menu()
        sec.append(_('Über netshift'), "win.about")
        menu.append_section(None, sec)
        hb.pack_end(Gtk.MenuButton(icon_name="open-menu-symbolic", menu_model=menu, tooltip_text=_('Hauptmenü')))
        self.refresh_stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE)
        self.refresh_stack.add_named(Gtk.Button(icon_name="view-refresh-symbolic", action_name="win.refresh",
                                                tooltip_text=_('Aktualisieren (F5)')), "btn")
        self.refresh_stack.add_named(Gtk.Spinner(spinning=True, halign=Gtk.Align.CENTER), "spin")
        hb.pack_end(self.refresh_stack)
        tv.add_top_bar(hb)

        self.stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE, transition_duration=220)
        # Laden
        lb = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12, valign=Gtk.Align.CENTER)
        lb.append(Gtk.Spinner(spinning=True, width_request=32, height_request=32))
        lb.append(Gtk.Label(label=_('Verbinde mit dem Sicherungsziel…'), css_classes=["dim-label"]))
        self.stack.add_named(lb, "loading")
        # Willkommen
        sp = Adw.StatusPage(icon_name="folder-remote-symbolic", title=_('Willkommen bei netshift'),
                            description=_('Sichere dein System auf ein NAS, einen Server oder ein USB-Laufwerk und stelle es mit wenigen Klicks wieder her.'))
        sp.set_child(Gtk.Button(label=_('Sicherungsziel einrichten'), halign=Gtk.Align.CENTER,
                                css_classes=["pill", "suggested-action"], action_name="win.settings"))
        self.stack.add_named(sp, "welcome")
        # Fehler
        self.err_page = Adw.StatusPage(icon_name="network-offline-symbolic", title=_('Ziel nicht erreichbar'))
        eb = Gtk.Box(spacing=12, halign=Gtk.Align.CENTER)
        eb.append(Gtk.Button(label=_('Erneut versuchen'), css_classes=["pill", "suggested-action"],
                             action_name="win.refresh"))
        eb.append(Gtk.Button(label=_('Einstellungen'), css_classes=["pill"], action_name="win.settings"))
        self.err_page.set_child(eb)
        self.stack.add_named(self.err_page, "error")
        # Liste
        self.list_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=24,
                                margin_top=18, margin_bottom=24, margin_start=12, margin_end=12)
        clamp = Adw.Clamp(maximum_size=880, tightening_threshold=600, child=self.list_box)
        self.scroller = Gtk.ScrolledWindow(child=clamp, hscrollbar_policy=Gtk.PolicyType.NEVER)
        self.stack.add_named(self.scroller, "list")
        tv.set_content(self.stack)

        self.op = OperationPanel(self)
        tv.add_bottom_bar(self.op)
        tv.set_bottom_bar_style(Adw.ToolbarStyle.RAISED)
        self.toasts.set_child(tv)
        self.set_content(self.toasts)
        self.connect("close-request", self._on_close)

    def _actions(self):
        for name, cb in (("create", self.create), ("refresh", self.refresh), ("settings", self.open_settings),
                         ("about", self.about)):
            a = Gio.SimpleAction.new(name, None)
            a.connect("activate", lambda *_a, f=cb: f())
            self.add_action(a)
        for name, cb in (("restore", self.restore), ("files", self.files), ("browse", self.browse),
                         ("details", self.details), ("delete", self.delete)):
            a = Gio.SimpleAction.new(name, GLib.VariantType.new("s"))
            a.connect("activate", lambda _a, v, f=cb: f(self.snap(v.get_string())))
            self.add_action(a)
        app = self.get_application()
        app.set_accels_for_action("win.create", ["<Control>n"])
        app.set_accels_for_action("win.refresh", ["F5", "<Control>r"])
        app.set_accels_for_action("win.settings", ["<Control>comma"])

    def snap(self, name):
        return next((s for s in self.snaps if s["name"] == name), None)

    def toast(self, text, timeout=3):
        self.toasts.add_toast(Adw.Toast(title=text, timeout=timeout))

    def busy(self):
        return self.op.job is not None

    def set_actions_enabled(self, on):
        for n in ("create", "refresh", "restore", "files", "browse", "delete"):
            self.lookup_action(n).set_enabled(on)

    def run_op(self, title, args, on_done=None, cancellable=True):
        if self.busy():
            self.toast(_('Es läuft bereits ein Vorgang'))
            return
        self.set_actions_enabled(False)

        def done(rc, lines):
            self.set_actions_enabled(True)
            if on_done:
                on_done(rc, lines)
            self.refresh(quiet=True)
        self.op.start(title, args, done, cancellable)

    # ---- Liste laden
    def refresh(self, quiet=False):
        if self.busy() or self.list_job:
            return
        if not os.path.exists(ns.CONFIG_PATH):
            self.stack.set_visible_child_name("welcome")
            self.wtitle.set_subtitle(_('Noch nicht eingerichtet'))
            return
        cfg = ns.load_config()
        self.wtitle.set_subtitle(ns.Target(cfg).describe())
        if not self.snaps and not quiet:
            self.stack.set_visible_child_name("loading")
        self.refresh_stack.set_visible_child_name("spin")

        def done(rc, lines):
            self.list_job = None
            self.refresh_stack.set_visible_child_name("btn")
            data = None
            for l in reversed(lines):
                if l.startswith("{"):
                    try:
                        data = json.loads(l)
                    except ValueError:
                        pass
                    break
            if rc != 0 or data is None:
                self.err_page.set_description(GLib.markup_escape_text(Job.error_of(lines)))
                self.stack.set_visible_child_name("error")
                return
            self.listing = data
            self.snaps = data["snapshots"]
            self.populate(cfg)
        self.list_job = Job(["list", "--json"], None, done)

    def populate(self, cfg):
        rescue = bool(cfg.get("rescue_mode"))
        self.btn_create.set_visible(not rescue)
        self.lookup_action("create").set_enabled(not rescue)
        self.wtitle.set_title(_('netshift – Rettungssystem') if rescue else "netshift")
        child = self.list_box.get_first_child()
        while child:
            nxt = child.get_next_sibling()
            self.list_box.remove(child)
            child = nxt
        for pr in (self.listing or {}).get("health") or []:
            self.list_box.append(self._problem_card(pr, cfg))
        self.list_box.append(self._hero(cfg))
        if not self.snaps and rescue:
            self.list_box.append(Adw.StatusPage(icon_name="camera-photo-symbolic", title=_('Keine Snapshots gefunden'),
                                                description=_('In dieser Sicherung liegen keine Snapshots.')))
        elif not self.snaps:
            sp = Adw.StatusPage(icon_name="camera-photo-symbolic", title=_('Noch keine Snapshots'),
                                description=_('Erstelle jetzt den ersten Snapshot. Er überträgt das komplette System – das dauert einmalig etwas länger, danach nur noch Änderungen.'))
            sp.set_child(Gtk.Button(label=_('Ersten Snapshot erstellen'), halign=Gtk.Align.CENTER,
                                    css_classes=["pill", "suggested-action"], action_name="win.create"))
            self.list_box.append(sp)
        groups = {}
        for s in reversed(self.snaps):
            key, title = period_of(s.get("epoch") or 0)
            if key not in groups:
                g = Adw.PreferencesGroup(title=title)
                groups[key] = g
                self.list_box.append(g)
            groups[key].add(self._row(s))
        self.stack.set_visible_child_name("list")

    def _problem_card(self, pr, cfg):
        lvl = pr.get("level", "warning")
        card = Gtk.Box(spacing=14, css_classes=["card", "problem", lvl])
        card.append(Gtk.Image(icon_name="dialog-error-symbolic" if lvl == "error" else "dialog-warning-symbolic",
                              pixel_size=24, valign=Gtk.Align.CENTER, css_classes=[lvl]))
        txt = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, hexpand=True, valign=Gtk.Align.CENTER)
        txt.append(Gtk.Label(label=pr["title"], xalign=0, css_classes=["problem-title"], wrap=True))
        txt.append(Gtk.Label(label=pr["body"], xalign=0, wrap=True, css_classes=["dim-label"],
                             wrap_mode=Pango.WrapMode.WORD_CHAR, lines=3, ellipsize=Pango.EllipsizeMode.END))
        card.append(txt)
        image = (self.listing or {}).get("mode") == "image"
        if pr["kind"] == "space" and image:
            b = Gtk.Button(label=_('Vergrößern…'), valign=Gtk.Align.CENTER, css_classes=["pill"])
            b.connect("clicked", lambda *_x: self.resize_image())
            card.append(b)
        if pr["kind"] in ("overdue", "error") and not cfg.get("rescue_mode"):
            card.append(Gtk.Button(label=_('Jetzt sichern'), valign=Gtk.Align.CENTER,
                                   css_classes=["pill", "suggested-action"], action_name="win.create"))
        if pr["kind"] == "error" and len(pr["body"]) > 90:
            b = Gtk.Button(icon_name="view-more-symbolic", valign=Gtk.Align.CENTER, css_classes=["flat"],
                           tooltip_text=_('Vollständige Meldung'))
            b.connect("clicked", lambda *_x: self.show_text(pr["title"], pr["body"]))
            card.append(b)
        return card

    def show_text(self, title, body):
        d = Adw.AlertDialog(heading=title, body=body)
        d.add_response("ok", "OK")
        d.present(self)

    def resize_image(self):
        cfg = ns.load_config()
        cur = int(cfg["target"].get("image_size_gb") or 100)
        spin = Gtk.SpinButton.new_with_range(cur + 10, 100000, 10)
        spin.set_value(max(cur + 100, int(cur * 1.5)))
        box = Gtk.Box(spacing=10, halign=Gtk.Align.CENTER)
        box.append(spin)
        box.append(Gtk.Label(label="GiB"))
        d = Adw.AlertDialog(heading=_('Image vergrößern'),
                            body=_('Das Image ist zurzeit {cur} GiB groß. Es belegt auf dem NAS weiterhin nur so viel Platz, wie tatsächlich gesichert ist. Verkleinern ist später nicht möglich.').format(cur=cur))
        d.set_extra_child(box)
        d.add_response("cancel", _('Abbrechen'))
        d.add_response("ok", _('Vergrößern'))
        d.set_response_appearance("ok", Adw.ResponseAppearance.SUGGESTED)
        d.set_close_response("cancel")
        d.connect("response", lambda _d, r: r == "ok" and self.run_op(
            _('Image wird vergrößert'), ["resize-image", str(int(spin.get_value()))],
            lambda rc, l: rc == 0 and self.toast(_('Image auf {0} GiB vergrößert').format(int(spin.get_value()))),
            cancellable=False))
        d.present(self)

    def wireless(self):
        """Läuft die Verbindung zum Netzwerkziel über WLAN? (schnell, ohne das Ziel einzubinden)"""
        try:
            p = subprocess.run([sys.executable, CORE, "netcheck", "--json"], capture_output=True, text=True,
                               timeout=8)
            return bool(json.loads(p.stdout.strip().splitlines()[-1]).get("wireless"))
        except Exception:
            return False

    def _hero(self, cfg):
        d = self.listing or {}
        card = Gtk.Box(spacing=18, css_classes=["card", "hero"])
        typ = cfg["target"]["type"]
        icon = {"smb": "folder-remote-symbolic", "nfs": "folder-remote-symbolic",
                "ssh": "network-server-symbolic", "local": "drive-harddisk-symbolic"}[typ]
        ic = Gtk.Image(icon_name=icon, pixel_size=32, valign=Gtk.Align.CENTER, css_classes=["hero-icon"])
        card.append(ic)
        mid = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4, hexpand=True, valign=Gtk.Align.CENTER)
        mid.append(Gtk.Label(label=ns.Target(cfg).describe(), xalign=0, css_classes=["hero-title"],
                             ellipsize=Pango.EllipsizeMode.MIDDLE))
        valid = [s for s in self.snaps if s.get("valid")]
        if valid:
            last = valid[-1]
            info = _('Letzter Snapshot {0} · {1} Snapshots').format(fmt_ago(last['epoch']), len(self.snaps))
        else:
            info = _('Noch kein Snapshot vorhanden')
        mid.append(Gtk.Label(label=info, xalign=0, css_classes=["dim-label"]))
        chips = Gtk.Box(spacing=6, margin_top=6)
        if cfg.get("rescue_mode"):
            chips.append(pill(_('Rettungssystem · nur Wiederherstellung'), "warning"))
            chips.append(pill(_('Rechner: {0}').format(cfg['machine_name']), "neutral"))
            mid.append(chips)
            card.append(mid)
            return self._hero_space(card, d)
        sch = cfg["schedule"]
        names = [lbl for key, lbl, _x in LEVELS if sch.get(key, {}).get("enabled")]
        if cfg["pacman_hook"]["enabled"]:
            names.append(_('Vor Updates'))
        if names:
            for n in names:
                chips.append(pill(n, "success"))
        else:
            chips.append(pill(_('Keine automatischen Snapshots'), "neutral"))
        if d.get("mode") == "image":
            chips.append(pill(_('Image-Container') + (_(' · verschlüsselt') if cfg["target"]["encrypt"] else ""),
                              "neutral"))
        mid.append(chips)
        card.append(mid)
        return self._hero_space(card, d)

    def _hero_space(self, card, d):
        free, total = d.get("free", 0), d.get("total", 0)
        if total:
            right = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, valign=Gtk.Align.CENTER,
                            width_request=170)
            used = 1 - free / total
            right.append(Gtk.Label(label=_('{0} frei').format(ns.human(free)), xalign=1, css_classes=["heading"]))
            lvl = Gtk.LevelBar(value=used, min_value=0, max_value=1)
            lvl.remove_offset_value("low")
            lvl.remove_offset_value("high")
            lvl.remove_offset_value("full")
            if used > 0.9:
                lvl.add_css_class("warn")
            right.append(lvl)
            right.append(Gtk.Label(label=_('von {0}').format(ns.human(total)), xalign=1, css_classes=["dim-label", "caption"]))
            card.append(right)
        if d.get("in_progress"):
            box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
            box.append(card)
            bn = Adw.Banner(title=_('Ein unterbrochener Snapshot wird beim nächsten Erstellen fortgesetzt.'),
                            revealed=True)
            box.append(bn)
            return box
        return card

    def _row(self, s):
        name = s["name"]
        title = fmt_when(s.get("epoch") or 0)
        sub = s.get("comment") or ""
        if s.get("os"):
            sub = f"{sub} · {s['os']}" if sub else s["os"]
        if not s.get("valid"):
            sub = _('⚠ Unvollständig – ') + sub
        elif s.get("status") == "warnings":
            sub = _('⚠ Mit Warnungen – ') + sub
        row = Adw.ActionRow(title=GLib.markup_escape_text(title), subtitle=GLib.markup_escape_text(sub),
                            activatable=True, subtitle_lines=1)
        tags = s.get("tags") or []
        icon = next((TAG_ICON[t] for t in tags if t in TAG_ICON), "alarm-symbolic")
        row.add_prefix(Gtk.Image(icon_name=icon, css_classes=["dim-label"]))
        for t in tags:
            lbl, style = TAG_STYLE.get(t, (t, "neutral"))
            row.add_suffix(pill(lbl, style))
        rb = Gtk.Button(icon_name="edit-undo-symbolic", tooltip_text=_('Wiederherstellen…'),
                        valign=Gtk.Align.CENTER, css_classes=["flat"], action_name="win.restore",
                        action_target=GLib.Variant("s", name))
        row.add_suffix(rb)
        menu = Gio.Menu()
        for label, act in ((_('Dateien zurückholen…'), "files"), (_('Im Dateimanager öffnen'), "browse"),
                           (_('Details & Kommentar…'), "details")):
            it = Gio.MenuItem.new(label, None)
            it.set_action_and_target_value("win." + act, GLib.Variant("s", name))
            menu.append_item(it)
        dsec = Gio.Menu()
        it = Gio.MenuItem.new(_("Löschen…"), None)
        it.set_action_and_target_value("win.delete", GLib.Variant("s", name))
        dsec.append_item(it)
        menu.append_section(None, dsec)
        row.add_suffix(Gtk.MenuButton(icon_name="view-more-symbolic", menu_model=menu, valign=Gtk.Align.CENTER,
                                      css_classes=["flat"], tooltip_text=_('Weitere Aktionen')))
        row.connect("activated", lambda *_x: self.details(s))
        return row

    # ---- Aktionen
    def create(self):
        body = _('Optional eine kurze Beschreibung, z.B. „vor Treiber-Update“.')
        if not [x for x in self.snaps if x.get("valid")] and self.wireless():
            body += (_('\n\nHinweis: Der erste Snapshot überträgt das ganze System, und du bist per WLAN verbunden. Mit LAN-Kabel geht es schneller und zuverlässiger.'))
        d = Adw.AlertDialog(heading=_('Snapshot erstellen'), body=body)
        entry = Gtk.Entry(placeholder_text=_('Kommentar (optional)'), activates_default=True)
        d.set_extra_child(entry)
        d.add_response("cancel", _('Abbrechen'))
        d.add_response("create", _('Erstellen'))
        d.set_response_appearance("create", Adw.ResponseAppearance.SUGGESTED)
        d.set_default_response("create")
        d.set_close_response("cancel")

        def resp(_d, r):
            if r == "create":
                self.run_op(_('Snapshot wird erstellt'), ["create", "-c", entry.get_text().strip()],
                            lambda rc, l: rc == 0 and self.toast(_('Snapshot erstellt')))
        d.connect("response", resp)
        d.present(self)
        entry.grab_focus()

    def delete(self, s):
        if not s:
            return
        d = Adw.AlertDialog(heading=_('Snapshot löschen?'),
                            body=_('{0}\n{1}\n\nDer Snapshot wird endgültig entfernt.').format(fmt_when(s['epoch']), s.get('comment') or ''))
        d.add_response("cancel", _('Abbrechen'))
        d.add_response("delete", _('Löschen'))
        d.set_response_appearance("delete", Adw.ResponseAppearance.DESTRUCTIVE)
        d.set_close_response("cancel")
        d.connect("response", lambda _d, r: r == "delete" and self.run_op(
            _('Snapshot wird gelöscht'), ["delete", "-y", s["name"]],
            lambda rc, l: rc == 0 and self.toast(_('Snapshot gelöscht')), cancellable=False))
        d.present(self)

    def restore(self, s):
        if s:
            RestoreDialog(self, s).present(self)

    def files(self, s):
        if s:
            FilesDialog(self, s).present(self)

    def details(self, s):
        if s:
            DetailsDialog(self, s).present(self)

    def mount_snapshots(self, cb):
        """Snapshots einbinden (im Hintergrund), cb(pfad oder None)."""
        if self.listing and self.listing.get("mode") == "ssh":
            cb(None)
            return

        def done(rc, lines):
            path = lines[-1].strip() if lines else ""
            if rc == 0 and os.path.isdir(path):
                self.mounted_by_us = True
                cb(path)
            else:
                self.toast(_('Einbinden fehlgeschlagen: ') + Job.error_of(lines)[:120], 5)
                cb(None)
        Job(["mount", "-q"], None, done)

    def browse(self, s):
        if not s:
            return
        if self.listing and self.listing.get("mode") == "ssh":
            self.toast(_('Bei SSH-Zielen bitte „Dateien zurückholen“ verwenden'), 4)
            return

        def got(base):
            if not base:
                return
            path = os.path.join(base, s["name"], "localhost")
            if open_as_user(path):
                self.toast(_('Snapshot im Dateimanager geöffnet'))
            else:
                self.toast(_('Eingebunden unter {path}').format(path=path), 6)
        self.mount_snapshots(got)

    def open_settings(self):
        SettingsDialog(self).present(self)

    def about(self):
        a = Adw.AboutDialog(application_name="netshift", version=ns.VERSION,
                            application_icon="drive-harddisk-system-symbolic",
                            comments=_('System-Snapshots wie Timeshift – lokal, auf SMB-, NFS- und SSH-Zielen.'),
                            license_type=Gtk.License.GPL_3_0, developer_name="netshift")
        a.present(self)

    def ask_reboot(self, seconds=10):
        d = Adw.AlertDialog(heading=_('Wiederherstellung abgeschlossen'))
        d.add_response("later", _('Später'))
        d.add_response("now", _('Jetzt neu starten'))
        d.set_response_appearance("now", Adw.ResponseAppearance.SUGGESTED)
        d.set_default_response("now")
        d.set_close_response("later")
        state = {"left": seconds, "src": 0}

        def tick():
            d.set_body(_('Das System wird in {0} Sekunden neu gestartet, damit alle Änderungen wirksam werden.').format(state['left']))
            if state["left"] <= 0:
                d.close()
                subprocess.Popen(["systemctl", "reboot"])
                return False
            state["left"] -= 1
            return True

        def resp(_d, r):
            if state["src"]:
                GLib.source_remove(state["src"])
                state["src"] = 0
            if r == "now":
                subprocess.Popen(["systemctl", "reboot"])
            else:
                self.toast(_('Bitte bald neu starten, damit alle Änderungen wirksam werden'), 6)
        tick()
        state["src"] = GLib.timeout_add_seconds(1, lambda: tick() or (state.update(src=0) or False))
        d.connect("response", resp)
        d.present(self)

    def _on_close(self, *_x):
        if self.busy():
            d = Adw.AlertDialog(heading=_('Vorgang läuft noch'),
                                body=_('Beim Beenden wird der laufende Vorgang abgebrochen. Ein abgebrochener Snapshot wird beim nächsten Mal fortgesetzt.'))
            d.add_response("stay", _('Weiterlaufen lassen'))
            d.add_response("quit", _('Abbrechen und beenden'))
            d.set_response_appearance("quit", Adw.ResponseAppearance.DESTRUCTIVE)
            d.set_close_response("stay")

            def resp(_d, r):
                if r == "quit":
                    self.op.job.cancel()
                    GLib.timeout_add(1500, lambda: (self._final_quit(), False)[1])
            d.connect("response", resp)
            d.present(self)
            return True
        self._final_quit()
        return True

    def _final_quit(self):
        if self.mounted_by_us:
            subprocess.run([sys.executable, CORE, "umount"], capture_output=True)
        if self.unlocked:
            subprocess.run([sys.executable, CORE, "lock"] + sorted(set(self.unlocked)), capture_output=True)
        self.get_application().quit()


# --------------------------------------------------------------------------- Dialoge

def dialog_shell(title, width=560, height=640):
    """Adw.Dialog mit Kopfzeile, rollbarem Inhalt und Fußleiste."""
    d = Adw.Dialog(title=title, content_width=width, content_height=height)
    tv = Adw.ToolbarView()
    tv.add_top_bar(Adw.HeaderBar())
    page = Adw.PreferencesPage()
    tv.set_content(page)
    foot = Gtk.Box(spacing=10, halign=Gtk.Align.END, margin_top=10, margin_bottom=12,
                   margin_start=12, margin_end=12)
    tv.add_bottom_bar(foot)
    toasts = Adw.ToastOverlay(child=tv)
    d.set_child(toasts)
    return d, page, foot, toasts


def radio_row(title, subtitle, group=None):
    cb = Gtk.CheckButton(valign=Gtk.Align.CENTER)
    if group:
        cb.set_group(group)
    row = Adw.ActionRow(title=title, subtitle=subtitle, activatable_widget=cb)
    row.add_prefix(cb)
    return row, cb


def device_menu_button(on_pick):
    """Knopf mit Liste der Laufwerke."""
    mb = Gtk.MenuButton(icon_name="drive-harddisk-symbolic", valign=Gtk.Align.CENTER,
                        css_classes=["flat"], tooltip_text=_('Laufwerk auswählen'))
    pop = Gtk.Popover()
    lb = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE, css_classes=["boxed-list"],
                     margin_top=6, margin_bottom=6, margin_start=6, margin_end=6)
    devs = lsblk_parts()
    if not devs:
        lb.append(Adw.ActionRow(title=_('Keine Laufwerke gefunden')))
    for d in devs:
        running = d.get("mountpoint") == "/"
        sub = dev_subtitle(d) + (_(' · dieses laufende System') if running else "")
        r = Adw.ActionRow(title=d["path"], subtitle=GLib.markup_escape_text(sub), activatable=not running,
                          sensitive=not running)
        if d.get("locked"):
            r.add_prefix(Gtk.Image(icon_name="channel-secure-symbolic"))
        elif d.get("unlocked_from"):
            r.add_prefix(Gtk.Image(icon_name="channel-insecure-symbolic"))
        if not running:
            r.connect("activated", lambda _r, dd=d: (on_pick(dd), pop.popdown()))
        lb.append(r)
    pop.set_child(Gtk.ScrolledWindow(child=lb, propagate_natural_height=True, max_content_height=360,
                                     min_content_width=380))
    mb.set_popover(pop)
    return mb


def pick_folder(parent, title, cb, initial=None):
    fd = Gtk.FileDialog(title=title, modal=True)
    if initial and os.path.isdir(initial):
        fd.set_initial_folder(Gio.File.new_for_path(initial))

    def done(dlg, res):
        try:
            f = dlg.select_folder_finish(res)
            if f:
                cb(f.get_path())
        except GLib.Error:
            pass
    fd.select_folder(parent.get_root() if hasattr(parent, "get_root") else parent, None, done)


class RestoreDialog:
    def __init__(self, win, snap):
        self.win, self.snap = win, snap
        self.d, page, foot, self.toasts = dialog_shell(_('System wiederherstellen'), 600, 680)

        g0 = Adw.PreferencesGroup()
        r = Adw.ActionRow(title=fmt_when(snap["epoch"]),
                          subtitle=GLib.markup_escape_text(" · ".join(x for x in (
                              snap.get("comment"), snap.get("os"),
                              f"Kernel {snap['kernel']}" if snap.get("kernel") else "") if x)))
        r.add_prefix(Gtk.Image(icon_name="camera-photo-symbolic"))
        g0.add(r)
        page.add(g0)

        g1 = Adw.PreferencesGroup(title=_('Wohin?'))
        self.r_live, self.cb_live = radio_row(_('Dieses System'),
                                              _('Systemdateien auf den Stand des Snapshots setzen, danach Neustart'))
        self.r_other, self.cb_other = radio_row(_('Anderes Laufwerk oder Ordner'),
                                                _('Live-USB, neue Festplatte oder ein Verzeichnis'), self.cb_live)
        cfg = ns.load_config()
        self.rescue = bool(cfg.get("rescue_mode"))
        foreign = bool(snap.get("host")) and snap["host"] != ns.hostname()
        if self.rescue or foreign:
            self.r_live.set_visible(False)
            self.cb_other.set_active(True)
            self.r_other.set_title(_('Festplatte des Rechners'))
            self.r_other.set_subtitle(GLib.markup_escape_text(
                _('Systempartition von „{0}“ auswählen – das Laufwerk-Symbol rechts listet alle Partitionen (Größe, Dateisystem, Name)').format(snap.get('host') or cfg['machine_name'])))
            self.r_other.set_activatable(False)
            self.cb_other.set_visible(False)
        else:
            self.cb_live.set_active(True)
        g1.add(self.r_live)
        g1.add(self.r_other)
        self.target = Adw.EntryRow(title=_('Gerät oder Ordner (z.B. /dev/sda2)'), visible=False)
        self.target.add_suffix(device_menu_button(self.pick_device))
        self.target.add_suffix(icon_button("folder-open-symbolic", _('Ordner wählen'),
                                           lambda: pick_folder(self.d, _('Wurzel des Zielsystems'),
                                                               self.target.set_text)))
        g1.add(self.target)
        page.add(g1)

        g2 = Adw.PreferencesGroup(title=_('Optionen'))
        self.sw_pre = Adw.SwitchRow(title=_('Sicherheits-Snapshot anlegen'),
                                    subtitle=_('Aktuellen Zustand vorher sichern – falls du zurück willst'), active=True)
        self.sw_reboot = Adw.SwitchRow(title=_('Danach neu starten'), subtitle=_('Dringend empfohlen'), active=True)
        self.sw_fstab = Adw.SwitchRow(title=_('fstab und crypttab behalten'),
                                      subtitle=_('Bei geänderten Partitionen oder UUIDs'))
        self.sw_grub = Adw.SwitchRow(title=_('GRUB-Konfiguration neu erzeugen'),
                                     subtitle=_('grub-mkconfig im wiederhergestellten System'), visible=False)
        for w in (self.sw_pre, self.sw_reboot, self.sw_fstab, self.sw_grub):
            g2.add(w)
        page.add(g2)

        g3 = Adw.PreferencesGroup()
        self.wlan_row = Adw.ActionRow(title=_('Verbindung über WLAN'),
                                      subtitle=_('Für die Wiederherstellung besser ein LAN-Kabel verwenden – bei Aussetzern bricht sie sonst ab.'), visible=False)
        self.wlan_row.add_prefix(Gtk.Image(icon_name="network-wireless-symbolic", css_classes=["warning"]))
        g3.add(self.wlan_row)
        GLib.idle_add(lambda: self.wlan_row.set_visible(self.win.wireless()) and False)
        home = snap.get("include_home")
        note = Adw.ActionRow(
            title=_('Persönliche Dateien werden zurückgesetzt') if home else _('Persönliche Dateien bleiben unverändert'),
            subtitle=_('Dieser Snapshot enthält /home.') if home else
            _('/home und /root sind nicht Teil dieses Snapshots und werden nicht angefasst.'))
        note.add_prefix(Gtk.Image(icon_name="dialog-warning-symbolic" if home else "emblem-ok-symbolic",
                                  css_classes=["warning" if home else "success"]))
        g3.add(note)
        page.add(g3)

        self.cb_live.connect("toggled", lambda *_x: self.sync())
        bprev = Gtk.Button(label=_('Vorschau der Änderungen'), css_classes=["pill"])
        bprev.connect("clicked", lambda *_x: self.preview())
        bgo = Gtk.Button(label=_('Wiederherstellen'), css_classes=["pill", "destructive-action"])
        bgo.connect("clicked", lambda *_x: self.go())
        foot.append(bprev)
        foot.append(bgo)
        self.sync()

    def present(self, parent):
        self.d.present(parent)

    def live(self):
        return self.cb_live.get_active()

    def sync(self):
        live = self.live()
        self.target.set_visible(not live)
        self.sw_pre.set_visible(live)
        self.sw_reboot.set_visible(live)
        self.sw_grub.set_visible(not live)

    def pick_device(self, d):
        if d.get("locked"):
            self.ask_unlock(d["path"])
        else:
            self.target.set_text(d["path"])

    def ask_unlock(self, dev, retry=False, then=None):
        d = Adw.AlertDialog(heading=_('Verschlüsselte Partition entsperren'),
                            body=((_('Das Passwort war falsch. ') if retry else "")
                                  + _('{dev} ist verschlüsselt. Gib das Passwort ein, mit dem du den Rechner beim Starten entsperrst.').format(dev=dev)))
        pw = Gtk.PasswordEntry(show_peek_icon=True, activates_default=True)
        d.set_extra_child(pw)
        d.add_response("cancel", _('Abbrechen'))
        d.add_response("unlock", _('Entsperren'))
        d.set_response_appearance("unlock", Adw.ResponseAppearance.SUGGESTED)
        d.set_default_response("unlock")
        d.set_close_response("cancel")

        def resp(_d, r):
            if r != "unlock" or not pw.get_text():
                return
            self.target.set_text(dev)
            self.target.set_sensitive(False)
            self.toasts.add_toast(Adw.Toast(title=_('Entsperre…'), timeout=2))

            def done(rc, lines):
                self.target.set_sensitive(True)
                path = lines[-1].strip() if lines else ""
                if rc == 0 and path.startswith("/dev/"):
                    self.target.set_text(path)
                    if path.startswith("/dev/mapper/netshift-"):
                        self.win.unlocked.append(path)
                    self.toasts.add_toast(Adw.Toast(title=_('Entsperrt – Ziel ist jetzt {path}').format(path=path), timeout=4))
                    if then:
                        then()
                elif rc == 3:          # falsches Passwort
                    self.ask_unlock(dev, retry=True, then=then)
                else:
                    self.toasts.add_toast(Adw.Toast(title=Job.error_of(lines)[:150], timeout=6))
            Job(["unlock", dev, "--stdin"], None, done, input=pw.get_text())
        d.connect("response", resp)
        d.present(self.d)
        pw.grab_focus()

    def args(self, then=None):
        if self.live():
            t = "/"
        else:
            t = self.target.get_text().strip()
            if not t:
                self.toasts.add_toast(Adw.Toast(title=_('Bitte Laufwerk oder Ordner angeben')))
                self.target.grab_focus()
                return None
            # von Hand eingetragene, noch verschlossene Partition → erst entsperren
            if t.startswith(("/dev/", "UUID=")) and ns.is_luks(t):
                kids = ns.luks_children(t)
                if kids:
                    t = kids[0]
                    self.target.set_text(t)
                else:
                    self.ask_unlock(t, then=then)
                    return None
        a = ["--target", t]
        if self.sw_fstab.get_active():
            a.append("--keep-fstab")
        return a

    def preview(self):
        a = self.args(then=self.preview)
        if a is None:
            return
        PreviewDialog(self.win, _('Vorschau: {0}').format(fmt_when(self.snap['epoch'])),
                      ["diff", self.snap["name"]] + a).present(self.d)

    def go(self):
        a = self.args(then=self.go)
        if a is None:
            return
        live = self.live()
        what = _('das laufende System') if live else a[1]
        body = (_('Der Snapshot vom {0} wird auf {what} zurückgespielt. Seitdem hinzugekommene Systemdateien werden entfernt.').format(fmt_when(self.snap['epoch']), what=what))
        wlan = self.win.wireless()
        if wlan:
            body += (_('\n\n⚠ Du bist per WLAN verbunden. Bricht die Verbindung ab, ist das Ziel nur teilweise wiederhergestellt. Am besten jetzt ein LAN-Kabel anschließen.'))
        d = Adw.AlertDialog(heading=_('Wirklich wiederherstellen?'), body=body)
        d.add_response("cancel", _('Abbrechen'))
        d.add_response("go", _('Trotzdem wiederherstellen') if wlan else _('Wiederherstellen'))
        d.set_response_appearance("go", Adw.ResponseAppearance.DESTRUCTIVE)
        d.set_close_response("cancel")

        def resp(_d, r):
            if r != "go":
                return
            args = ["restore", self.snap["name"]] + a + ["--yes", "--no-reboot"]
            if live and not self.sw_pre.get_active():
                args.append("--no-pre-snapshot")
            if not live and self.sw_grub.get_active():
                args.append("--update-grub")
            reboot = live and self.sw_reboot.get_active()
            self.d.close()

            def done(rc, lines):
                if rc != 0:
                    return
                if reboot:
                    self.win.ask_reboot()
                elif live:
                    self.win.toast(_('Wiederhergestellt – bitte bald neu starten'), 6)
                elif self.rescue:
                    d2 = Adw.AlertDialog(heading=_('Wiederherstellung abgeschlossen'),
                                         body=_('Fahre den Rechner jetzt herunter, ziehe den USB-Stick ab und starte ihn wieder – er bootet dann mit dem zurückgespielten System.'))
                    d2.add_response("later", _('Später'))
                    d2.add_response("off", _('Herunterfahren'))
                    d2.set_response_appearance("off", Adw.ResponseAppearance.SUGGESTED)
                    d2.set_close_response("later")
                    d2.connect("response", lambda _d, r: r == "off" and subprocess.Popen(["systemctl", "poweroff"]))
                    d2.present(self.win)
                else:
                    self.win.toast(_('Wiederherstellung abgeschlossen'))
            self.win.run_op(_('System wird wiederhergestellt'), args, done, cancellable=False)
        d.connect("response", resp)
        d.present(self.d)


class PreviewDialog:
    """Zeigt rsync-Änderungen übersichtlich: Zähler + filterbare Liste."""

    def __init__(self, win, title, args):
        self.win = win
        self.d = Adw.Dialog(title=title, content_width=760, content_height=640)
        tv = Adw.ToolbarView()
        hb = Adw.HeaderBar()
        tv.add_top_bar(hb)
        self.stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE)
        lb = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12, valign=Gtk.Align.CENTER)
        lb.append(Gtk.Spinner(spinning=True, width_request=32, height_request=32))
        lb.append(Gtk.Label(label=_('Vergleiche Snapshot mit dem Ziel…'), css_classes=["dim-label"]))
        self.stack.add_named(lb, "loading")
        self.err = Adw.StatusPage(icon_name="dialog-error-symbolic", title=_('Vorschau fehlgeschlagen'))
        self.stack.add_named(self.err, "error")
        self.none = Adw.StatusPage(icon_name="emblem-ok-symbolic", title=_('Keine Änderungen'),
                                   description=_('Das Ziel entspricht bereits diesem Snapshot.'))
        self.stack.add_named(self.none, "none")

        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12, margin_top=12, margin_bottom=12,
                       margin_start=12, margin_end=12)
        stats = Gtk.Box(spacing=12, homogeneous=True)
        self.stat = {}
        for key, label, css in (("new", _('Neu'), "stat-new"), ("changed", _('Geändert'), "stat-changed"),
                                ("del", _('Gelöscht'), "stat-deleted")):
            c = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, css_classes=["card"], margin_top=2)
            v = Gtk.Label(label="0", css_classes=["stat-value", css], margin_top=12)
            c.append(v)
            c.append(Gtk.Label(label=label, css_classes=["dim-label"], margin_bottom=12))
            stats.append(c)
            self.stat[key] = v
        body.append(stats)
        fb = Gtk.Box(spacing=8)
        self.search = Gtk.SearchEntry(placeholder_text=_('Pfad filtern…'), hexpand=True)
        fb.append(self.search)
        seg = Gtk.Box(css_classes=["linked"])
        self.kind = "all"
        first = None
        for key, label in (("all", _('Alle')), ("new", _('Neu')), ("changed", _('Geändert')), ("del", _('Gelöscht'))):
            b = Gtk.ToggleButton(label=label)
            if first:
                b.set_group(first)
            else:
                first = b
                b.set_active(True)
            b.connect("toggled", lambda btn, k=key: btn.get_active() and self._set_kind(k))
            seg.append(b)
        fb.append(seg)
        body.append(fb)

        self.model = Gtk.StringList()
        self.filter = Gtk.CustomFilter.new(self._match)
        fm = Gtk.FilterListModel(model=self.model, filter=self.filter)
        fac = Gtk.SignalListItemFactory()
        fac.connect("setup", self._setup)
        fac.connect("bind", self._bind)
        lv = Gtk.ListView(model=Gtk.NoSelection(model=fm), factory=fac, css_classes=["navigation-sidebar"])
        body.append(Gtk.ScrolledWindow(child=lv, vexpand=True, css_classes=["card"]))
        self.hint = Gtk.Label(css_classes=["dim-label", "caption"], xalign=0, wrap=True)
        body.append(self.hint)
        self.stack.add_named(body, "list")
        self.search.connect("search-changed", lambda *_x: self.filter.changed(Gtk.FilterChange.DIFFERENT))
        tv.set_content(self.stack)
        self.d.set_child(tv)
        self.job = Job(args, None, self._done)
        self.d.connect("closed", lambda *_x: self.job.proc.poll() is None and self.job.cancel())

    def present(self, parent):
        self.d.present(parent)

    def _set_kind(self, k):
        self.kind = k
        self.filter.changed(Gtk.FilterChange.DIFFERENT)

    def _match(self, item):
        kind, path = item.get_string().split("\t", 1)
        if self.kind != "all" and kind != self.kind:
            return False
        q = self.search.get_text().strip().lower()
        return not q or q in path.lower()

    def _setup(self, _f, li):
        b = Gtk.Box(spacing=10, margin_start=6, margin_end=6)
        b.append(Gtk.Image())
        b.append(Gtk.Label(xalign=0, ellipsize=Pango.EllipsizeMode.START, hexpand=True, css_classes=["mono"]))
        li.set_child(b)

    def _bind(self, _f, li):
        kind, path = li.get_item().get_string().split("\t", 1)
        img = li.get_child().get_first_child()
        lbl = img.get_next_sibling()
        icon, css, tip = {"new": ("list-add-symbolic", "change-icon-new", _('wird neu angelegt')),
                          "changed": ("document-edit-symbolic", "change-icon-changed", _('wird überschrieben')),
                          "del": ("list-remove-symbolic", "change-icon-del", _('wird gelöscht'))}[kind]
        img.set_from_icon_name(icon)
        for c in ("change-icon-new", "change-icon-changed", "change-icon-del"):
            img.remove_css_class(c)
        img.add_css_class(css)
        img.set_tooltip_text(tip)
        lbl.set_label(path)
        lbl.set_tooltip_text(path)

    def _done(self, rc, lines):
        if rc != 0:
            self.err.set_description(GLib.markup_escape_text(Job.error_of(lines)))
            self.stack.set_visible_child_name("error")
            return
        items, counts = [], {"new": 0, "changed": 0, "del": 0}
        attr = 0
        for l in lines:
            c = classify(l)
            if not c:
                continue
            if c[0] == "attr":
                attr += 1
                continue
            counts[c[0]] += 1
            items.append(f"{c[0]}\t{c[1]}")
        if not items:
            self.stack.set_visible_child_name("none")
            return
        self.model.splice(0, 0, items)
        for k, v in counts.items():
            self.stat[k].set_label(fmt_int(v))
        self.hint.set_label(
            _('„Neu“ = fehlt auf dem Ziel und wird angelegt · „Geändert“ = wird mit der Version aus dem Snapshot überschrieben · „Gelöscht“ = ist seit dem Snapshot hinzugekommen und wird entfernt.')
            + (_(' Außerdem werden bei {attr} Einträgen nur Zeitstempel/Rechte angepasst.').format(attr=attr) if attr else ""))
        self.stack.set_visible_child_name("list")


class FilesDialog:
    def __init__(self, win, snap):
        self.win, self.snap = win, snap
        self.paths = []
        self.d, page, foot, self.toasts = dialog_shell(_('Dateien zurückholen'), 600, 640)
        self.ssh = bool(win.listing and win.listing.get("mode") == "ssh")

        self.g_paths = Adw.PreferencesGroup(title=_('Was soll zurückgeholt werden?'),
                                            description=_('Aus dem Snapshot vom {0}').format(fmt_when(snap['epoch'])))
        bb = Gtk.Box(spacing=6)
        bb.append(Gtk.Button(child=Adw.ButtonContent(icon_name="document-open-symbolic", label=_('Dateien')),
                             css_classes=["flat"], sensitive=not self.ssh))
        bb.append(Gtk.Button(child=Adw.ButtonContent(icon_name="folder-open-symbolic", label=_('Ordner')),
                             css_classes=["flat"], sensitive=not self.ssh))
        bb.get_first_child().connect("clicked", lambda *_x: self.pick(False))
        bb.get_last_child().connect("clicked", lambda *_x: self.pick(True))
        self.g_paths.set_header_suffix(bb)
        self.entry = Adw.EntryRow(title=_('Pfad eingeben, z.B. /etc/pacman.conf'), show_apply_button=True)
        self.entry.connect("apply", lambda *_x: self.add_path(self.entry.get_text()))
        self.entry.connect("entry-activated", lambda *_x: self.add_path(self.entry.get_text()))
        self.g_paths.add(self.entry)
        page.add(self.g_paths)

        g = Adw.PreferencesGroup(title=_('Wohin?'))
        self.r_orig, self.cb_orig = radio_row(_('An den Originalort'), _('Aktuelle Version wird überschrieben'))
        u = invoking_user()
        default = os.path.join(u.pw_dir if u else "/root", "netshift-wiederhergestellt")
        self.r_other, self.cb_other = radio_row(_('In einen anderen Ordner'), default, self.cb_orig)
        self.cb_other.set_active(True)
        self.other_dir = default
        self.r_other.add_suffix(icon_button("folder-open-symbolic", _('Ordner wählen'), self.pick_dest))
        g.add(self.r_orig)
        g.add(self.r_other)
        if ns.load_config().get("rescue_mode"):
            self.r_orig.set_visible(False)
        self.sw_delete = Adw.SwitchRow(title=_('Ordner exakt angleichen'),
                                       subtitle=_('Seitdem neu hinzugekommene Dateien darin löschen'))
        g.add(self.sw_delete)
        page.add(g)

        bprev = Gtk.Button(label=_('Vorschau'), css_classes=["pill"])
        bprev.connect("clicked", lambda *_x: self.go(dry=True))
        bgo = Gtk.Button(label=_('Zurückholen'), css_classes=["pill", "suggested-action"])
        bgo.connect("clicked", lambda *_x: self.go())
        foot.append(bprev)
        foot.append(bgo)

    def present(self, parent):
        self.d.present(parent)

    def add_path(self, p):
        p = p.strip()
        if not p:
            return
        if not p.startswith("/"):
            self.toasts.add_toast(Adw.Toast(title=_('Bitte einen absoluten Pfad angeben (beginnt mit /)')))
            return
        if p in self.paths:
            return
        self.paths.append(p)
        row = Adw.ActionRow(title=GLib.markup_escape_text(p))
        row.add_prefix(Gtk.Image(icon_name="text-x-generic-symbolic"))
        row.add_suffix(icon_button("user-trash-symbolic", _('Entfernen'),
                                   lambda: (self.paths.remove(p), self.g_paths.remove(row))))
        self.g_paths.add(row)
        self.entry.set_text("")

    def pick(self, folder):
        def got(base):
            if not base:
                return
            root = os.path.join(base, self.snap["name"], "localhost")
            fd = Gtk.FileDialog(title=_('Im Snapshot auswählen'), modal=True,
                                initial_folder=Gio.File.new_for_path(root))

            def done(dlg, res):
                try:
                    lst = dlg.select_multiple_folders_finish(res) if folder else dlg.open_multiple_finish(res)
                except GLib.Error:
                    return
                for i in range(lst.get_n_items()):
                    p = os.path.realpath(lst.get_item(i).get_path())
                    if p == root or p.startswith(root + "/"):
                        rel = os.path.relpath(p, root)
                        self.add_path("/" if rel == "." else "/" + rel)
                    else:
                        self.toasts.add_toast(Adw.Toast(title=_('Bitte nur innerhalb des Snapshots auswählen')))
            if folder:
                fd.select_multiple_folders(self.d.get_root(), None, done)
            else:
                fd.open_multiple(self.d.get_root(), None, done)
        self.win.mount_snapshots(got)

    def pick_dest(self):
        def set_dir(p):
            self.other_dir = p
            self.r_other.set_subtitle(GLib.markup_escape_text(p))
            self.cb_other.set_active(True)
        pick_folder(self.d, _('Zielordner'), set_dir)

    def go(self, dry=False):
        if self.entry.get_text().strip():
            self.add_path(self.entry.get_text())
        if not self.paths:
            self.toasts.add_toast(Adw.Toast(title=_('Bitte mindestens eine Datei oder einen Ordner angeben')))
            return
        a = ["restore-files", self.snap["name"]] + self.paths + ["--yes"]
        if self.cb_other.get_active():
            a += ["--to", self.other_dir]
        if self.sw_delete.get_active():
            a.append("--delete")
        if dry:
            PreviewDialog(self.win, _('Vorschau'), a + ["--dry-run"]).present(self.d)
            return
        dest = _('den Originalort') if self.cb_orig.get_active() else self.other_dir

        def run():
            self.d.close()
            self.win.run_op(_('Dateien werden zurückgeholt'), a,
                            lambda rc, l: rc == 0 and self.win.toast(_('{0} Element(e) wiederhergestellt').format(len(self.paths))))
        if self.cb_orig.get_active():
            d = Adw.AlertDialog(heading=_('Aktuelle Dateien überschreiben?'),
                                body=_('{0} Element(e) werden an {dest} zurückgeschrieben.').format(len(self.paths), dest=dest))
            d.add_response("cancel", _('Abbrechen'))
            d.add_response("go", _('Überschreiben'))
            d.set_response_appearance("go", Adw.ResponseAppearance.DESTRUCTIVE)
            d.set_close_response("cancel")
            d.connect("response", lambda _d, r: r == "go" and run())
            d.present(self.d)
        else:
            run()


class DetailsDialog:
    def __init__(self, win, s):
        self.win, self.s = win, s
        self.d, page, foot, self.toasts = dialog_shell(_('Snapshot-Details'), 540, 680)
        g = Adw.PreferencesGroup()
        self.comment = Adw.EntryRow(title=_('Kommentar'), text=s.get("comment") or "", show_apply_button=True)
        self.comment.connect("apply", lambda *_x: self.save_comment())
        g.add(self.comment)
        page.add(g)
        self._size_group(page, s)
        gi_ = Adw.PreferencesGroup(title=_('Informationen'))
        tags = ", ".join(TAG_STYLE.get(t, (t,))[0] for t in s.get("tags") or []) or "–"
        dur = s.get("duration_s")
        rows = [(_('Erstellt'), fmt_when(s.get("epoch") or 0)), (_('Typ'), tags), (_('System'), s.get("os")),
                (_('Kernel'), s.get("kernel")), (_('Rechner'), s.get("host")),
                (_('Dauer'), f"{int(dur // 60)} min {int(dur % 60)} s" if dur else None),
                (_('Basis'), fmt_when(dt.datetime.strptime(s["link_dest"], ns.TIME_FMT).timestamp())
                 if s.get("link_dest") else _('Vollständiger Snapshot')),
                (_('Home enthalten'), _('Ja') if s.get("include_home") else _('Nein')),
                (_('Status'), {"ok": _('In Ordnung'), "warnings": _('Mit Warnungen')}.get(s.get("status"), _("Unvollständig"))),
                (_('Name'), s["name"])]
        for k, v in rows:
            if v:
                r = Adw.ActionRow(title=k, subtitle=GLib.markup_escape_text(str(v)), subtitle_selectable=True)
                gi_.add(r)
        page.add(gi_)
        for label, act, css in ((_('Löschen'), "delete", ["pill", "destructive-action"]),
                                (_('Dateien zurückholen'), "files", ["pill"]),
                                (_('Wiederherstellen'), "restore", ["pill", "suggested-action"])):
            b = Gtk.Button(label=label, css_classes=css)
            b.connect("clicked", lambda *_a, a=act: (self.d.close(), getattr(self.win, a)(self.s)))
            foot.append(b)

    def present(self, parent):
        self.d.present(parent)
        GLib.idle_add(lambda: self.d.set_focus(None) and False)

    # ---- Speicherplatz
    def _size_group(self, page, s):
        mode = (self.win.listing or {}).get("mode")
        where = _('im Image') if mode == "image" else _('auf dem Ziel')
        g = Adw.PreferencesGroup(title=_('Speicherplatz {where}').format(where=where))
        self.btn_recalc = icon_button("view-refresh-symbolic", _('Neu berechnen'), lambda: self.load_size(True))
        self.btn_recalc.set_sensitive(False)
        g.set_header_suffix(self.btn_recalc)
        self.size_rows = {}
        for key, title, sub in (("exclusive", _('Belegt nur durch diesen Snapshot'), _('Wird beim Löschen frei')),
                                ("total", _('Gesamtumfang'), _('Vollständiges System zu diesem Zeitpunkt – unveränderte Dateien teilt er sich per Hardlink mit anderen Snapshots')),
                                ("files", _('Dateien'), None)):
            r = Adw.ActionRow(title=title, subtitle=sub or "")
            st = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE)
            st.add_named(Gtk.Spinner(spinning=True), "busy")
            lbl = Gtk.Label(css_classes=["heading" if key == "exclusive" else "body"])
            st.add_named(lbl, "value")
            r.add_suffix(st)
            g.add(r)
            self.size_rows[key] = (st, lbl)
        self.size_note = Gtk.Label(css_classes=["dim-label", "caption"], xalign=0, wrap=True, margin_top=6)
        g.add(self.size_note)
        page.add(g)
        self.size_job = None
        if not s.get("valid"):
            self._show_size(None, _('Für unvollständige Snapshots nicht verfügbar.'))
            return
        if s.get("size"):
            self._show_size(s["size"], _('Zuletzt berechnet – wird geprüft…'))
        GLib.idle_add(self.load_size)

    def _show_size(self, data, note=""):
        for key, (st, lbl) in self.size_rows.items():
            if data is None:
                lbl.set_label("–")
            elif key == "files":
                lbl.set_label(fmt_int(data.get('files', 0)))
            else:
                lbl.set_label(ns.human(data.get(key, 0)))
            st.set_visible_child_name("value")
        if data and data.get("total"):
            pct = 100 * data.get("exclusive", 0) / data["total"]
            st, lbl = self.size_rows["exclusive"]
            lbl.set_label(f"{ns.human(data['exclusive'])}  ({pct:.0f} %)")
        self.size_note.set_label(note)
        self.size_note.set_visible(bool(note))

    def load_size(self, refresh=False):
        if self.win.busy():
            self.size_note.set_label(_('Während eines laufenden Vorgangs nicht verfügbar.'))
            self.size_note.set_visible(True)
            for st, lbl in self.size_rows.values():
                if st.get_visible_child_name() == "busy":
                    lbl.set_label("–")
                    st.set_visible_child_name("value")
            return False
        if self.win.list_job:                     # Liste lädt noch – kurz warten
            GLib.timeout_add(300, lambda: self.load_size(refresh) and False)
            return False
        if not self.s.get("size") or refresh:
            for st, _x in self.size_rows.values():
                st.set_visible_child_name("busy")
            self.size_note.set_label(_('Wird berechnet – bei vielen Dateien über das Netzwerk kann das eine Minute dauern…'))
            self.size_note.set_visible(True)
        self.btn_recalc.set_sensitive(False)

        def done(rc, lines):
            self.size_job = None
            self.btn_recalc.set_sensitive(True)
            data = None
            for l in reversed(lines):
                if l.startswith("{"):
                    try:
                        data = json.loads(l).get(self.s["name"])
                    except ValueError:
                        pass
                    break
            if rc != 0 or not data:
                self._show_size(self.s.get("size"), _('Berechnung fehlgeschlagen: ') + Job.error_of(lines)[:160])
                return
            self.s["size"] = data
            when = data.get("measured", "")
            self._show_size(data, _('Berechnet {0}').format(fmt_when(dt.datetime.fromisoformat(when).timestamp()))
                            if when else "")
        args = ["size", self.s["name"], "--json"] + (["--refresh"] if refresh else [])
        self.size_job = Job(args, None, done)
        return False

    def save_comment(self):
        if self.size_job:                          # Sperre frei werden lassen
            GLib.timeout_add(400, lambda: (self.save_comment(), False)[1])
            return

        def done(rc, lines):
            if rc == 0:
                self.toasts.add_toast(Adw.Toast(title=_('Kommentar gespeichert')))
                self.win.refresh(quiet=True)
            else:
                self.toasts.add_toast(Adw.Toast(title=Job.error_of(lines)[:120]))
        Job(["comment", self.s["name"], self.comment.get_text()], None, done)


class SettingsDialog:
    def __init__(self, win):
        self.win = win
        self.cfg = ns.load_config()
        self.existed = os.path.exists(ns.CONFIG_PATH)
        t = self.cfg["target"]
        self.d = Adw.PreferencesDialog(title=_('Einstellungen'), search_enabled=False)
        self.d.set_content_height(680)
        self.d.add(self._page_target(t))
        self.d.add(self._page_storage(t))
        self.p_sched = self._page_schedule()
        self.d.add(self.p_sched)
        self.d.add(self._page_files())
        self.d.connect("closed", lambda *_x: self.save(final=True))
        self.sync_rescue()
        self.sync_type()

    def present(self, parent):
        self.d.present(parent)

    # ---- Seiten
    def _page_target(self, t):
        p = Adw.PreferencesPage(title=_('Ziel'), icon_name="folder-remote-symbolic")
        g = Adw.PreferencesGroup(title=_('Dieser Rechner'))
        self.e_name = Adw.EntryRow(title=_('Name im Backup'), text=self.cfg["machine_name"])
        g.add(self.e_name)
        self.sw_rescue = Adw.SwitchRow(title=_('Rettungssystem – nur wiederherstellen'),
                                       subtitle=_('Für den USB-Stick: Hier wird nie gesichert. Die Snapshots des Rechners werden nur gelesen und auf seine Festplatte zurückgespielt.'),
                                       active=bool(self.cfg.get("rescue_mode")))
        g.add(self.sw_rescue)
        self.name_hint = Gtk.Label(wrap=True, xalign=0, css_classes=["dim-label", "caption"], margin_top=6,
                                   label=_('Im Rettungssystem muss der Name genau so lauten wie beim gesicherten Rechner (z.B. meinrechner) – sonst findet netshift dessen Sicherung nicht.'))
        g.add(self.name_hint)
        self.sw_rescue.connect("notify::active", lambda *_x: self.sync_rescue())
        p.add(g)

        g = Adw.PreferencesGroup(title=_('Sicherungsziel'))
        self.c_type = Adw.ComboRow(title=_('Art'), model=Gtk.StringList.new([l for _x, l in TYPES]))
        self.c_type.set_selected([k for k, _x in TYPES].index(t["type"]))
        self.c_type.connect("notify::selected", lambda *_x: self.sync_type())
        g.add(self.c_type)
        p.add(g)

        cred = ns.read_smb_credentials()
        self.g_smb = Adw.PreferencesGroup(title=_('SMB / Windows-Freigabe / NAS'),
                                          description=_('z.B. smb://192.168.1.10/Backup → Server 192.168.1.10, Freigabe Backup'))
        self.e_smb_server = Adw.EntryRow(title=_('Server (Name oder IP)'), text=t["server"])
        self.e_share = Adw.EntryRow(title=_('Freigabe'), text=t["share"].strip("/"))
        self.e_user = Adw.EntryRow(title=_('Benutzer (leer = Gast)'), text=cred.get("username", ""))
        self.e_pass = Adw.PasswordEntryRow(title=_('Passwort') + (_(' (gespeichert – leer lassen zum Behalten)')
                                                                if cred.get("password") else ""))
        self.e_domain = Adw.EntryRow(title=_('Domäne / Arbeitsgruppe (optional)'), text=cred.get("domain", ""))
        vers = SMB_VERSIONS if t["smb_version"] in SMB_VERSIONS else SMB_VERSIONS + [t["smb_version"]]
        self.c_smbver = Adw.ComboRow(title=_("SMB-Version"), model=Gtk.StringList.new(vers))
        self.c_smbver.set_selected(vers.index(t["smb_version"]) if t["smb_version"] in vers else 1)
        self._smb_versions = vers
        for w in (self.e_smb_server, self.e_share, self.e_user, self.e_pass, self.e_domain, self.c_smbver):
            self.g_smb.add(w)
        p.add(self.g_smb)

        self.g_nfs = Adw.PreferencesGroup(title="NFS")
        self.e_nfs_server = Adw.EntryRow(title=_('Server (Name oder IP)'), text=t["server"])
        self.e_export = Adw.EntryRow(title=_('Export, z.B. /volume1/backup'), text=t["export"])
        self.g_nfs.add(self.e_nfs_server)
        self.g_nfs.add(self.e_export)
        p.add(self.g_nfs)

        self.g_ssh = Adw.PreferencesGroup(title="SSH", description=_('Linux-Server oder NAS mit rsync'))
        self.e_ssh_server = Adw.EntryRow(title=_('Server (Name oder IP)'), text=t["server"])
        self.e_ssh_user = Adw.EntryRow(title=_('Benutzer'), text=t["ssh_user"])
        self.s_port = Adw.SpinRow.new_with_range(1, 65535, 1)
        self.s_port.set_title(_('Port'))
        self.s_port.set_value(int(t["ssh_port"] or 22))
        self.e_ssh_path = Adw.EntryRow(title=_('Pfad auf dem Server'), text=t["ssh_path"])
        self.e_ssh_key = Adw.EntryRow(title=_('Schlüsseldatei'), text=t["ssh_key"])
        self.sw_fake = Adw.SwitchRow(title=_('Benutzer ist nicht root'),
                                     subtitle=_('Besitzer und Rechte per --fake-super speichern'),
                                     active=t["ssh_fake_super"])
        btn = Gtk.Button(label=_('Anzeigen…'), valign=Gtk.Align.CENTER)
        btn.connect("clicked", lambda *_x: self.show_ssh_key())
        kr = Adw.ActionRow(title=_('SSH-Schlüssel'), subtitle=_('Erzeugen und auf dem Server hinterlegen'))
        kr.add_suffix(btn)
        for w in (self.e_ssh_server, self.e_ssh_user, self.s_port, self.e_ssh_path, self.e_ssh_key,
                  self.sw_fake, kr):
            self.g_ssh.add(w)
        p.add(self.g_ssh)

        self.g_local = Adw.PreferencesGroup(title=_('Lokales Laufwerk'),
                                            description=_('Ordner, Gerät (/dev/sdb1) oder UUID=… – UUID ist am zuverlässigsten bei USB-Laufwerken'))
        self.e_path = Adw.EntryRow(title=_('Pfad, Gerät oder UUID'), text=t["path"])
        self.e_path.add_suffix(device_menu_button(
            lambda d: self.e_path.set_text(f"UUID={d['uuid']}" if d.get("uuid") else d["path"])))
        self.e_path.add_suffix(icon_button("folder-open-symbolic", _('Ordner wählen'),
                                           lambda: pick_folder(self.d, _('Zielordner'), self.e_path.set_text)))
        self.g_local.add(self.e_path)
        p.add(self.g_local)

        g = Adw.PreferencesGroup()
        self.test_row = Adw.ActionRow(title=_('Verbindung testen'), subtitle=_('Speichert und prüft das Ziel'))
        self.test_icon = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE)
        self.test_icon.add_named(Gtk.Image(icon_name="network-transmit-receive-symbolic"), "idle")
        self.test_icon.add_named(Gtk.Spinner(spinning=True), "busy")
        self.test_icon.add_named(Gtk.Image(icon_name="emblem-ok-symbolic", css_classes=["success"]), "ok")
        self.test_icon.add_named(Gtk.Image(icon_name="dialog-error-symbolic", css_classes=["error"]), "error")
        self.test_row.add_prefix(self.test_icon)
        tb = Gtk.Button(label=_('Testen'), valign=Gtk.Align.CENTER, css_classes=["suggested-action"])
        tb.connect("clicked", lambda *_x: self.test())
        self.test_row.add_suffix(tb)
        g.add(self.test_row)
        p.add(g)
        return p

    def _page_storage(self, t):
        p = Adw.PreferencesPage(title=_('Speicher'), icon_name="drive-harddisk-symbolic")
        g = Adw.PreferencesGroup(title=_('Ablage'),
                                 description=_('„Automatisch“ nutzt auf SMB, NFS, FAT und NTFS einen ext4-Image-Container – nur so bleiben Besitzer, Rechte und Hardlinks erhalten.'))
        self.c_cont = Adw.ComboRow(title=_('Speicherformat'), model=Gtk.StringList.new([l for _x, l in CONTAINERS]))
        self.c_cont.set_selected([k for k, _x in CONTAINERS].index(t.get("container", "auto")))
        self.s_size = Adw.SpinRow.new_with_range(5, 100000, 10)
        self.s_size.set_title(_('Maximale Image-Größe (GiB)'))
        self.s_size.set_subtitle(_('Belegt nur so viel, wie tatsächlich gesichert ist'))
        self.s_size.set_value(int(t["image_size_gb"]))
        g.add(self.c_cont)
        g.add(self.s_size)
        p.add(g)
        g = Adw.PreferencesGroup(title=_('Verschlüsselung'))
        self.ex_enc = Adw.ExpanderRow(title=_('Image verschlüsseln (LUKS2)'),
                                      subtitle=_('Schützt die Sicherung, falls jemand Zugriff auf das NAS hat'),
                                      show_enable_switch=True, enable_expansion=bool(t["encrypt"]))
        self.e_key = Adw.EntryRow(title=_('Schlüsseldatei'), text=t["key_file"])
        self.ex_enc.add_row(self.e_key)
        warn = Adw.ActionRow(title=_('Schlüsseldatei unbedingt zusätzlich sichern'),
                             subtitle=_('Ohne sie ist keine Wiederherstellung vom Live-USB möglich'))
        warn.add_prefix(Gtk.Image(icon_name="dialog-warning-symbolic", css_classes=["warning"]))
        self.ex_enc.add_row(warn)
        g.add(self.ex_enc)
        p.add(g)
        g = Adw.PreferencesGroup(title=_('Erweitert'))
        self.e_mopts = Adw.EntryRow(title=_('Zusätzliche Mount-Optionen'), text=t["mount_options"])
        self.s_minfree = Adw.SpinRow.new_with_range(0, 10000, 1)
        self.s_minfree.set_title(_('Mindestens frei lassen (GiB)'))
        self.s_minfree.set_value(float(self.cfg["min_free_gb"]))
        self.sw_cleanup = Adw.SwitchRow(title=_('Bei Platzmangel automatisch aufräumen'),
                                        subtitle=_('Löscht die ältesten automatischen Snapshots – nur wenn es wirklich hilft. Manuelle Snapshots bleiben immer erhalten.'),
                                        active=bool(self.cfg.get("auto_cleanup", True)))
        g.add(self.sw_cleanup)
        g.add(self.e_mopts)
        g.add(self.s_minfree)
        p.add(g)
        return p

    def _page_schedule(self):
        p = Adw.PreferencesPage(title=_('Zeitplan'), icon_name="alarm-symbolic")
        g = Adw.PreferencesGroup(title=_('Automatische Snapshots'),
                                 description=_('Verpasste Termine werden nachgeholt, sobald das Ziel erreichbar ist. Manuelle Snapshots werden nie automatisch gelöscht.'))
        self.sched = {}
        for key, label, icon in LEVELS:
            s = self.cfg["schedule"][key]
            ex = Adw.ExpanderRow(title=label, show_enable_switch=True, enable_expansion=s["enabled"])
            ex.add_prefix(Gtk.Image(icon_name=icon))
            sp = Adw.SpinRow.new_with_range(1, 99, 1)
            sp.set_title(_('Anzahl behalten'))
            sp.set_value(s["keep"])
            ex.add_row(sp)
            self._keep_subtitle(ex, sp)
            sp.connect("notify::value", lambda *_a, e=ex, s_=sp: self._keep_subtitle(e, s_))
            ex.connect("notify::enable-expansion", lambda *_a, e=ex, s_=sp: self._keep_subtitle(e, s_))
            g.add(ex)
            self.sched[key] = (ex, sp)
        p.add(g)
        ph = self.cfg["pacman_hook"]
        g = Adw.PreferencesGroup(title=_('Software-Updates'),
                                 description=_('Snapshot vor jeder Installation, Aktualisierung oder Entfernung mit pacman oder pamac'))
        self.ex_ph = Adw.ExpanderRow(title=_('Snapshot vor Updates'), show_enable_switch=True,
                                     enable_expansion=ph["enabled"])
        self.ex_ph.add_prefix(Gtk.Image(icon_name="system-software-install-symbolic"))
        self.s_phkeep = Adw.SpinRow.new_with_range(1, 99, 1)
        self.s_phkeep.set_title(_('Anzahl behalten'))
        self.s_phkeep.set_value(ph["keep"])
        self.s_phint = Adw.SpinRow.new_with_range(0, 1440, 5)
        self.s_phint.set_title(_('Mindestabstand (Minuten)'))
        self.s_phint.set_subtitle(_('Mehrere Updates kurz hintereinander lösen nur einen Snapshot aus'))
        self.s_phint.set_value(ph["min_interval_minutes"])
        self.sw_phabort = Adw.SwitchRow(title=_('Update abbrechen, wenn der Snapshot fehlschlägt'),
                                        subtitle=_('Z.B. wenn das NAS nicht erreichbar ist'), active=ph["abort_on_fail"])
        for w in (self.s_phkeep, self.s_phint, self.sw_phabort):
            self.ex_ph.add_row(w)
        g.add(self.ex_ph)
        p.add(g)
        nt = self.cfg.get("notify") or {}
        g = Adw.PreferencesGroup(title=_('Benachrichtigungen'),
                                 description=_('Meldung auf dem Desktop, wenn eine automatische Sicherung fehlschlägt oder zu lange keine erfolgreiche Sicherung lief. Ist das NAS nur nicht erreichbar (z.B. unterwegs), wird erst nach der eingestellten Zeit gewarnt.'))
        self.ex_notify = Adw.ExpanderRow(title=_('Bei Problemen benachrichtigen'), show_enable_switch=True,
                                         enable_expansion=bool(nt.get("enabled", True)))
        self.ex_notify.add_prefix(Gtk.Image(icon_name="preferences-system-notifications-symbolic"))
        self.s_overdue = Adw.SpinRow.new_with_range(1, 60, 1)
        self.s_overdue.set_title(_('Warnen, wenn die letzte Sicherung älter ist als (Tage)'))
        self.s_overdue.set_value(float(nt.get("overdue_days", 3) or 3))
        self.ex_notify.add_row(self.s_overdue)
        g.add(self.ex_notify)
        p.add(g)
        return p

    @staticmethod
    def _keep_subtitle(ex, sp):
        ex.set_subtitle(_('Die letzten {0} behalten').format(int(sp.get_value())) if ex.get_enable_expansion() else _('Aus'))

    def _page_files(self):
        p = Adw.PreferencesPage(title=_('Ausschlüsse'), icon_name="edit-clear-all-symbolic")
        g = Adw.PreferencesGroup(title=_('Umfang'))
        self.sw_home = Adw.SwitchRow(title=_('Home-Verzeichnisse mitsichern'),
                                     subtitle=_('Achtung: Eine Wiederherstellung setzt dann auch persönliche Dateien zurück'), active=self.cfg["include_home"])
        self.sw_cache = Adw.SwitchRow(title=_('Paketcache auslassen'), subtitle="/var/cache/pacman/pkg",
                                      active=self.cfg["exclude_pacman_cache"])
        g.add(self.sw_home)
        g.add(self.sw_cache)
        p.add(g)
        self.g_ex = Adw.PreferencesGroup(title=_('Eigene Ausschlüsse'),
                                         description=_('rsync-Muster, z.B. /opt/spiele/* oder /home/*/Videos/*'))
        self.e_add = Adw.EntryRow(title=_('Muster hinzufügen'), show_apply_button=True)
        self.e_add.connect("apply", lambda *_x: self._add_ex(self.e_add.get_text()))
        self.e_add.connect("entry-activated", lambda *_x: self._add_ex(self.e_add.get_text()))
        self.g_ex.add(self.e_add)
        self.ex_rows = []
        for e in self.cfg["exclude"]:
            self._add_ex(e)
        p.add(self.g_ex)
        g = Adw.PreferencesGroup(title=_('Immer ausgeschlossen'))
        r = Adw.ActionRow(title=_('Systemverzeichnisse'), subtitle=", ".join(ns.FORCED_EXCLUDES), subtitle_lines=3)
        r2 = Adw.ActionRow(title=_('Eingehängte Netzwerk- und Temp-Dateisysteme'),
                           subtitle=_('Werden beim Erstellen automatisch erkannt (Mountpunkt bleibt erhalten)'))
        g.add(r)
        g.add(r2)
        p.add(g)
        return p

    def _add_ex(self, pattern):
        pattern = pattern.strip()
        if not pattern or any(r[0] == pattern for r in self.ex_rows):
            return
        row = Adw.ActionRow(title=GLib.markup_escape_text(pattern), css_classes=["monospace"])
        entry = [pattern, row]
        row.add_suffix(icon_button("user-trash-symbolic", _('Entfernen'),
                                   lambda: (self.ex_rows.remove(entry), self.g_ex.remove(row))))
        self.g_ex.add(row)
        self.ex_rows.append(entry)
        self.e_add.set_text("")

    # ---- Logik
    def cur_type(self):
        return TYPES[self.c_type.get_selected()][0]

    def sync_rescue(self):
        on = self.sw_rescue.get_active()
        self.name_hint.set_visible(on)
        # Zeitplan & Update-Snapshots sind im Rettungssystem bedeutungslos
        for child in (self.p_sched,):
            child.set_sensitive(not on)

    def sync_type(self):
        typ = self.cur_type()
        for k, g in (("smb", self.g_smb), ("nfs", self.g_nfs), ("ssh", self.g_ssh), ("local", self.g_local)):
            g.set_visible(k == typ)

    def collect(self):
        c, t = self.cfg, self.cfg["target"]
        typ = self.cur_type()
        c["machine_name"] = ns.safe_name(self.e_name.get_text().strip() or ns.hostname())
        c["rescue_mode"] = self.sw_rescue.get_active()
        t["type"] = typ
        t["server"] = {"smb": self.e_smb_server, "nfs": self.e_nfs_server, "ssh": self.e_ssh_server}.get(
            typ, self.e_smb_server).get_text().strip()
        t["share"] = self.e_share.get_text().strip().strip("/")
        t["smb_version"] = self._smb_versions[self.c_smbver.get_selected()]
        t["export"] = self.e_export.get_text().strip()
        t["ssh_user"] = self.e_ssh_user.get_text().strip()
        t["ssh_port"] = int(self.s_port.get_value())
        t["ssh_path"] = self.e_ssh_path.get_text().strip()
        t["ssh_key"] = self.e_ssh_key.get_text().strip()
        t["ssh_fake_super"] = self.sw_fake.get_active()
        t["path"] = self.e_path.get_text().strip()
        t["container"] = CONTAINERS[self.c_cont.get_selected()][0]
        t["image_size_gb"] = int(self.s_size.get_value())
        t["encrypt"] = self.ex_enc.get_enable_expansion()
        t["key_file"] = self.e_key.get_text().strip()
        t["mount_options"] = self.e_mopts.get_text().strip()
        c["min_free_gb"] = self.s_minfree.get_value()
        for key, (ex, sp) in self.sched.items():
            c["schedule"][key] = {"enabled": ex.get_enable_expansion(), "keep": int(sp.get_value())}
        c["pacman_hook"] = {"enabled": self.ex_ph.get_enable_expansion(), "keep": int(self.s_phkeep.get_value()),
                            "min_interval_minutes": int(self.s_phint.get_value()),
                            "abort_on_fail": self.sw_phabort.get_active()}
        c["notify"] = {"enabled": self.ex_notify.get_enable_expansion(),
                       "overdue_days": int(self.s_overdue.get_value())}
        c["auto_cleanup"] = self.sw_cleanup.get_active()
        c["include_home"] = self.sw_home.get_active()
        c["exclude_pacman_cache"] = self.sw_cache.get_active()
        c["exclude"] = [r[0] for r in self.ex_rows]
        return c

    def save(self, final=False):
        c = self.collect()
        complete = True
        try:
            ns.Target(c).validate()
        except ns.NetshiftError:
            complete = False
        if not complete and final and not self.existed:
            return  # Ersteinrichtung abgebrochen – nichts anlegen
        if self.cur_type() == "smb":
            user = self.e_user.get_text().strip()
            if user:
                old = ns.read_smb_credentials()
                pw = self.e_pass.get_text() or (old.get("password", "") if old.get("username") == user else "")
                ns.write_smb_credentials(user, pw, self.e_domain.get_text().strip())
            elif os.path.exists(ns.cred_path()):
                os.remove(ns.cred_path())
        ns.save_config(c)
        self.existed = True
        if final:
            subprocess.Popen([sys.executable, CORE, "schedule-apply"], stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL)
            self.win.snaps = []
            self.win.refresh()
            if not complete:
                self.win.toast(_('Sicherungsziel ist noch unvollständig'), 4)

    def test(self):
        try:
            ns.Target(self.collect()).validate()
        except ns.NetshiftError as e:
            self.d.add_toast(Adw.Toast(title=str(e).split(".")[0], timeout=4))
            return
        self.save()
        self.test_icon.set_visible_child_name("busy")
        self.test_row.set_subtitle(_('Verbinde…'))

        def done(rc, lines):
            if rc == 0:
                msg = Job.result_of(lines)
                self.test_icon.set_visible_child_name("ok")
                self.test_row.set_subtitle(GLib.markup_escape_text(msg))
            else:
                self.test_icon.set_visible_child_name("error")
                self.test_row.set_subtitle(GLib.markup_escape_text(Job.error_of(lines)[:300]))
        Job(["test"], None, done)

    def show_ssh_key(self):
        key = self.e_ssh_key.get_text().strip() or os.path.join(ns.ETC_DIR, "id_ed25519")
        if not os.path.exists(key):
            os.makedirs(os.path.dirname(key), mode=0o700, exist_ok=True)
            subprocess.run(["ssh-keygen", "-t", "ed25519", "-N", "", "-C", f"netshift@{ns.hostname()}",
                            "-f", key], capture_output=True)
        try:
            with open(key + ".pub") as f:
                pub = f.read().strip()
        except OSError as e:
            self.d.add_toast(Adw.Toast(title=str(e)))
            return
        host = f"{self.e_ssh_user.get_text().strip()}@{self.e_ssh_server.get_text().strip()}"
        cmd = f"sudo ssh-copy-id -i {key}.pub -p {int(self.s_port.get_value())} {host}"
        d = Adw.AlertDialog(heading=_('SSH-Schlüssel hinterlegen'),
                            body=_('Einmalig im Terminal ausführen (fragt das Server-Passwort ab) – oder den Schlüssel auf dem Server in ~/.ssh/authorized_keys eintragen.'))
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        for text in (cmd, pub):
            lbl = Gtk.Label(label=text, selectable=True, wrap=True, wrap_mode=Pango.WrapMode.CHAR,
                            xalign=0, css_classes=["mono", "card"], margin_top=2)
            box.append(lbl)
        d.set_extra_child(box)
        d.add_response("copy", _('Befehl kopieren'))
        d.add_response("ok", _('Fertig'))
        d.set_default_response("ok")
        d.connect("response", lambda _d, r: r == "copy" and self.d.get_clipboard().set(cmd))
        d.present(self.d)


# --------------------------------------------------------------------------- App

class App(Adw.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.NON_UNIQUE)
        self.connect("activate", self.on_activate)

    def on_activate(self, *_x):
        sm = Adw.StyleManager.get_default()
        sm.set_color_scheme({"dark": Adw.ColorScheme.FORCE_DARK, "light": Adw.ColorScheme.FORCE_LIGHT}
                            .get(COLOR_SCHEME, Adw.ColorScheme.DEFAULT))
        prov = Gtk.CssProvider()
        prov.load_from_data(CSS.encode())
        Gtk.StyleContext.add_provider_for_display(Gdk.Display.get_default(), prov,
                                                  Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        win = self.get_active_window() or MainWindow(self)
        win.present()


def main():
    if os.geteuid() != 0:
        os.execvp("netshift-gui", ["netshift-gui"] + sys.argv[1:])
    GLib.set_prgname("netshift")
    GLib.set_application_name("netshift")
    return App().run(sys.argv[:1])


if __name__ == "__main__":
    sys.exit(main())
