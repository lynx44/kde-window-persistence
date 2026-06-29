#!/usr/bin/env python3
"""
window-persistence — place browser windows by tab content onto KDE virtual
desktops + custom-tiling zones, per monitor-dock profile.

Pipeline (all verified on Plasma 6.6 / KWin 6.6, Wayland):
  1. Detect connected outputs from /sys/class/drm  -> pick a profile.
  2. For each rule, ask the Plasma Browser-Integration "Tabs" KRunner for tabs
     matching the rule string  (org.kde.plasma.browser_integration /TabsRunner
     org.kde.krunner1.Match) -> (tabId, title, browserIcon, ...).
  3. Run(tabId, "") to switch that browser to the tab AND raise its window
     (a browser window's title only reflects its *active* tab, so this is the
     only way to find a background tab's window).
  4. Load a generated KWin script via org.kde.KWin /Scripting that moves the
     now-active window to the target virtual desktop and tile (or maximizes).

Usage:
  wp.py apply [--profile NAME] [--dry-run] [--config PATH]
  wp.py list-tabs [QUERY]
  wp.py status
  wp.py watch [--interval SEC]      # re-apply automatically on dock/undock
"""
import argparse, os, sys, time, glob, tempfile, json
import dbus

BI_SERVICE = "org.kde.plasma.browser_integration"
BI_PATH = "/TabsRunner"
KRUNNER_IFACE = "org.kde.krunner1"
KWIN_SERVICE = "org.kde.KWin"
KWIN_SCRIPTING = "/Scripting"
KWIN_SCRIPTING_IFACE = "org.kde.kwin.Scripting"

HERE = os.path.dirname(os.path.realpath(__file__))  # realpath so a ~/.local/bin symlink still finds config.yaml
DEFAULT_CONFIG = os.path.join(HERE, "config.yaml")


# ---------- monitor / profile detection ----------

def connected_outputs():
    """Return set of connected output names, e.g. {'eDP-1', 'DP-2'}."""
    out = set()
    for path in glob.glob("/sys/class/drm/card*-*/status"):
        try:
            with open(path) as f:
                if f.read().strip() == "connected":
                    # /sys/class/drm/card1-DP-2 -> DP-2  (strip 'cardN-')
                    base = os.path.basename(os.path.dirname(path))
                    name = base.split("-", 1)[1] if "-" in base else base
                    out.add(name)
        except OSError:
            pass
    return out


def pick_profile(cfg, forced=None):
    profiles = cfg.get("profiles", [])
    if forced:
        for p in profiles:
            if p.get("name") == forced:
                return p
        sys.exit(f"no profile named {forced!r}")
    conn = connected_outputs()
    for p in profiles:
        need = set(p.get("when_connected", []))
        if need.issubset(conn):
            return p
    return profiles[-1] if profiles else None


# ---------- browser tabs ----------

def browser_services():
    """Every browser-integration TabsRunner service. Each browser instance/profile
    registers its OWN org.kde.plasma.browser_integration[-<pid>] — KRunner queries
    them all, so we must too (the bare name is just one of them)."""
    bus = dbus.SessionBus()
    return sorted(str(n) for n in bus.list_names()
                  if str(n).startswith(BI_SERVICE))   # BI_SERVICE uses '_' ; excludes the dotted kded module


def match_tabs(query):
    """Return (tabId, title, browser, service) across ALL browser services."""
    bus = dbus.SessionBus()
    out = []
    for svc in browser_services():
        try:
            rows = bus.get_object(svc, BI_PATH).Match(query, dbus_interface=KRUNNER_IFACE)
        except dbus.DBusException:
            continue
        for r in rows:
            out.append((str(r[0]), str(r[1]), str(r[2]), svc))
    return out


def find_rule_tabs(rule):
    """Tabs that genuinely contain the rule's substring in their TITLE.

    The runner is fuzzy and also searches URLs, so we re-filter on the title to
    keep placement precise; browser is filtered when the rule specifies one."""
    sub = rule["match"].lower()
    want_browser = rule.get("browser")
    hits = []
    for tab_id, title, browser, service in match_tabs(rule["match"]):
        if sub not in title.lower():
            continue
        if want_browser and browser != want_browser:
            continue
        hits.append((tab_id, title, browser, service))
    return hits


def raise_tab(tab_id, service):
    """Focus a tab (and raise its window) on the service that owns it."""
    dbus.SessionBus().get_object(service, BI_PATH).Run(tab_id, "", dbus_interface=KRUNNER_IFACE)


# ---------- KWin placement ----------

KWIN_TMPL = """
(function () {
  var w = workspace.activeWindow;
  if (!w) { throw new Error("WP: no active window to place"); }
  // virtual desktop
  var vds = workspace.desktops;
  var di = %DESKTOP_INDEX%;
  var vd = (di >= 0 && di < vds.length) ? vds[di] : workspace.currentDesktop;
  w.desktops = [vd];
  // target output
  var screenName = "%SCREEN%";
  var out = null, ss = workspace.screens;
  for (var i = 0; i < ss.length; i++) { if (ss[i].name === screenName) out = ss[i]; }
  if (!out) { throw new Error("WP: output not found: " + screenName); }
  %PLACEMENT%
})();
"""

def gen_kwin_script(desktop_index, screen, zone):
    if zone == "maximize":
        placement = (
            "w.tile = null;\n"
            "  w.frameGeometry = out.geometry;\n"
            "  if (w.setMaximize) { w.setMaximize(true, true); }"
        )
    else:
        idx = int(zone)
        placement = (
            f"var root = workspace.rootTile(out, vd);\n"  # tiling is per (output, desktop)
            f"  var tiles = root ? root.tiles : [];\n"
            f"  if ({idx} >= 0 && {idx} < tiles.length) {{ w.tile = tiles[{idx}]; }}\n"
            f"  else {{ throw new Error('WP: zone {idx} out of range on ' + screenName); }}"
        )
    return (KWIN_TMPL
            .replace("%DESKTOP_INDEX%", str(desktop_index))
            .replace("%SCREEN%", screen)
            .replace("%PLACEMENT%", placement))


def run_kwin_script(js):
    # KWin's loadScript is overloaded (s / ss); dbus-python can't disambiguate,
    # so call with explicit signatures via call_blocking.
    bus = dbus.SessionBus()
    name = f"wp-{int(time.time()*1000)}"
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as f:
        f.write(js)
        path = f.name

    def call(method, signature, *args):
        return bus.call_blocking(KWIN_SERVICE, KWIN_SCRIPTING,
                                 KWIN_SCRIPTING_IFACE, method, signature, args)
    try:
        call("loadScript", "ss", path, name)
        call("start", "")
        time.sleep(0.2)
    finally:
        try:
            call("unloadScript", "s", name)
        except dbus.DBusException:
            pass
        os.unlink(path)


# ---------- snapshot / restore ----------
#
# Snapshot the live window layout for the current monitor set, and restore it
# later. On dock/undock KWin instantly reshuffles windows off the removed
# output, so we cannot capture a state *after* leaving it -> the watcher keeps
# a fresh snapshot of the current state and restores the snapshot of the state
# it enters. Windows survive plug/unplug, so we key on KWin's stable
# `internalId` (good within a session; ignored after a reboot regenerates ids).

STATE_DIR = os.path.expanduser("~/.local/state/window-persistence")
SINK_BUS = "org.wp.Sink"

_TILE_HELPERS = """
  function leaves(out, vd) {
    var root = workspace.rootTile(out, vd); var res = [];
    (function walk(t){ if(!t) return; var k=t.tiles||[];
      if(k.length===0){res.push(t);return;} for(var i=0;i<k.length;i++) walk(k[i]); })(root);
    return res;
  }
"""

CAPTURE_JS = """
(function(){
  %TILE%
  var vds = workspace.desktops;
  function dIndex(d){ for(var i=0;i<vds.length;i++) if(vds[i]===d) return i; return -1; }
  var wins = workspace.windowList();
  var arr = [];
  for (var i=0;i<wins.length;i++){
    var w = wins[i];
    if (!w.normalWindow || w.skipTaskbar) continue;
    var wd = w.desktops || [];
    var desks = [];
    for (var j=0;j<wd.length;j++){ var di=dIndex(wd[j]); if(di>=0) desks.push(di); }
    var outName = w.output ? w.output.name : null;
    var tileIdx = null;
    if (w.tile && w.output){
      var vd = wd.length ? wd[0] : workspace.currentDesktop;
      var lv = leaves(w.output, vd);
      for (var k=0;k<lv.length;k++){ if(lv[k]===w.tile){ tileIdx=k; break; } }
    }
    var g = w.frameGeometry;
    arr.push({ id:String(w.internalId), cls:w.resourceClass, caption:w.caption, output:outName,
               desktops:desks, tile:tileIdx, max:w.maximizeMode, minimized:w.minimized,
               geom:[Math.round(g.x),Math.round(g.y),Math.round(g.width),Math.round(g.height)] });
  }
  callDBus("%SINK%", "/", "%SINK%", "Report", JSON.stringify(arr));
})();
""".replace("%TILE%", _TILE_HELPERS).replace("%SINK%", SINK_BUS)

RESTORE_JS = """
(function(){
  %TILE%
  function findOut(name){ var ss=workspace.screens; for(var i=0;i<ss.length;i++) if(ss[i].name===name) return ss[i]; return null; }
  var vds = workspace.desktops;
  var byId = {}; var wins = workspace.windowList();
  for (var i=0;i<wins.length;i++) byId[String(wins[i].internalId)] = wins[i];
  var DATA = %DATA%;
  var done=0, miss=0;
  for (var j=0;j<DATA.length;j++){
    var e = DATA[j]; var w = byId[e.id];
    if (!w){ miss++; continue; }
    if (e.desktops && e.desktops.length){
      var ds=[]; for(var k=0;k<e.desktops.length;k++){ var di=e.desktops[k]; if(di>=0&&di<vds.length) ds.push(vds[di]); }
      if (ds.length) w.desktops = ds;
    } else {
      w.desktops = [];   // captured empty == shown on all desktops (sticky)
    }
    var out = e.output ? findOut(e.output) : null;
    // Attempt tile membership (nice for tile-managed dragging), but always also
    // enforce the saved geometry: tile reassignment is unreliable for some
    // (esp. XWayland) windows, whereas the rectangle reproduces the layout.
    if (e.tile !== null && out){
      var vd = (e.desktops && e.desktops.length && e.desktops[0] < vds.length) ? vds[e.desktops[0]] : workspace.currentDesktop;
      var lv = leaves(out, vd);
      if (e.tile < lv.length){ w.tile = lv[e.tile]; }
    } else {
      w.tile = null;
    }
    if (e.max === 3 && w.setMaximize) { w.setMaximize(true, true); }
    else { w.frameGeometry = { x:e.geom[0], y:e.geom[1], width:e.geom[2], height:e.geom[3] }; }
    done++;
  }
  throw new Error("WPRESTORE done=" + done + " miss=" + miss + " total=" + DATA.length);
})();
""".replace("%TILE%", _TILE_HELPERS)


def state_key():
    outs = sorted(connected_outputs())
    return "+".join(outs) if outs else "none"


def state_path(key):
    return os.path.join(STATE_DIR, "layout-" + key.replace("/", "_") + ".json")


def capture_layout(timeout=4.0):
    """Run the capture KWin script; it pushes the layout JSON back via callDBus."""
    import dbus.service
    import dbus.mainloop.glib
    from gi.repository import GLib
    # Private connection attached to a mainloop, so it works regardless of whether
    # the shared SessionBus was already created (e.g. by match_tabs) without one.
    bus = dbus.SessionBus(private=True, mainloop=dbus.mainloop.glib.DBusGMainLoop())
    holder = {}
    loop = GLib.MainLoop()

    class Sink(dbus.service.Object):
        @dbus.service.method(SINK_BUS, in_signature="s")
        def Report(self, s):
            holder["data"] = str(s)
            GLib.idle_add(loop.quit)

    busname = dbus.service.BusName(SINK_BUS, bus)
    sink = Sink(bus, "/")
    try:
        GLib.timeout_add(int(timeout * 1000), loop.quit)
        run_kwin_script(CAPTURE_JS)
        loop.run()
    finally:
        sink.remove_from_connection()
        del busname
        bus.close()
    return json.loads(holder["data"]) if "data" in holder else None


def snapshot(key=None, verbose=True):
    key = key or state_key()
    layout = capture_layout()
    if layout is None:
        if verbose:
            print("snapshot: no response from KWin")
        return False
    os.makedirs(STATE_DIR, exist_ok=True)
    with open(state_path(key), "w") as f:
        json.dump({"key": key, "windows": layout}, f)
    if verbose:
        print(f"snapshot: saved {len(layout)} windows for state {key!r} -> {state_path(key)}")
    return True


def restore(key=None, verbose=True):
    key = key or state_key()
    p = state_path(key)
    if not os.path.exists(p):
        if verbose:
            print(f"restore: no saved layout for state {key!r}")
        return False
    with open(p) as f:
        data = json.load(f).get("windows", [])
    run_kwin_script(RESTORE_JS.replace("%DATA%", json.dumps(data)))
    if verbose:
        print(f"restore: applied {len(data)} windows for state {key!r}")
    return True


# ---------- preferred (golden) layout ----------
#
# Unlike a snapshot (keyed on per-window internalId, valid only within a
# session), a "preferred" layout is keyed on app class + browser tab, so it
# survives reboots and freshly-launched windows. Workflow: arrange windows the
# way you like once, `save-preferred` to capture an editable template, then bind
# a hotkey to `apply-preferred` to tile everything back into that template.

PREF_DIR = os.path.expanduser("~/.config/window-persistence")
BROWSERS = {"brave-browser", "firefox", "org.mozilla.firefox", "chromium", "google-chrome"}
_TITLE_SUFFIXES = [" - Brave", " — Mozilla Firefox", " - Mozilla Firefox",
                   " - Google Chrome", " - Chromium"]

# Shared JS: leaves() + findOut() + placeWin(w, e) used by both preferred scripts.
_PLACE_FUNCS = _TILE_HELPERS + """
  function findOut(name){ var ss=workspace.screens; for(var i=0;i<ss.length;i++) if(ss[i].name===name) return ss[i]; return null; }
  function placeWin(w, e){
    var vds = workspace.desktops;
    if (e.desktops && e.desktops.length){
      var ds=[]; for(var k=0;k<e.desktops.length;k++){ var di=e.desktops[k]; if(di>=0&&di<vds.length) ds.push(vds[di]); }
      if (ds.length) w.desktops = ds;
    } else { w.desktops = []; }
    var out = e.screen ? findOut(e.screen) : null;
    var z = e.zone || {};
    if (z.type === "tile" && out){
      var vd = (e.desktops && e.desktops.length && e.desktops[0] < vds.length) ? vds[e.desktops[0]] : workspace.currentDesktop;
      var lv = leaves(out, vd);
      if (z.index < lv.length){
        var tl = lv[z.index]; w.tile = tl; var g = tl.absoluteGeometry;
        w.frameGeometry = { x:Math.round(g.x), y:Math.round(g.y), width:Math.round(g.width), height:Math.round(g.height) };
      }
    } else if (z.type === "maximize" && out){
      w.tile = null; var og = out.geometry;
      w.frameGeometry = { x:Math.round(og.x), y:Math.round(og.y), width:Math.round(og.width), height:Math.round(og.height) };
      if (w.setMaximize) w.setMaximize(true, true);
    } else if (z.type === "free" && z.geom){
      w.tile = null;
      w.frameGeometry = { x:z.geom[0], y:z.geom[1], width:z.geom[2], height:z.geom[3] };
    }
  }
"""

# Place specific windows (matched in Python) by their internalId.
# DATA = [{ id, desktops, screen, zone }]
PREF_PLACE_BY_ID_JS = ("(function(){" + _PLACE_FUNCS + """
  var byId = {}; var wins = workspace.windowList();
  for (var i=0;i<wins.length;i++) byId[String(wins[i].internalId)] = wins[i];
  var DATA = %DATA%;
  var placed = 0, miss = 0;
  for (var j=0;j<DATA.length;j++){
    var e = DATA[j], w = byId[e.id];
    if (!w){ miss++; continue; }
    placeWin(w, e); placed++;
  }
  throw new Error("WPPREF placed=" + placed + " miss=" + miss);
})();""")

# Place the currently-active window (used right after a browser tab is focused/raised).
PREF_ACTIVE_JS = ("(function(){" + _PLACE_FUNCS + """
  var w = workspace.activeWindow;
  if (!w) throw new Error("WPPREF1: no active window");
  placeWin(w, %ENTRY%);
  throw new Error("WPPREF1 placed " + w.resourceClass);
})();""")


def pref_path(key):
    return os.path.join(PREF_DIR, "preferred-" + key.replace("/", "_") + ".json")


def _strip_browser_suffix(caption):
    for suf in _TITLE_SUFFIXES:
        if caption.endswith(suf):
            return caption[:-len(suf)]
    return caption


def _zone_of(w):
    if w["tile"] is not None:
        return {"type": "tile", "index": w["tile"]}
    if w["max"] == 3:
        return {"type": "maximize"}
    return {"type": "free", "geom": w["geom"]}


def save_preferred(key=None):
    key = key or state_key()
    layout = capture_layout()
    if not layout:
        print("save-preferred: no response from KWin")
        return False
    entries = []
    for w in layout:
        e = {"app": w["cls"], "desktops": w["desktops"], "screen": w["output"], "zone": _zone_of(w)}
        cap = w.get("caption") or ""
        if w["cls"] in BROWSERS and cap:
            e["tab"] = _strip_browser_suffix(cap)      # identify this browser window by a tab it holds
        elif cap:
            e["title_hint"] = cap                       # disambiguate multiple windows of one app
        entries.append(e)
    os.makedirs(PREF_DIR, exist_ok=True)
    with open(pref_path(key), "w") as f:
        json.dump({"key": key, "place": entries}, f, indent=2)
    print(f"save-preferred: wrote {len(entries)} entries for state {key!r} -> {pref_path(key)}")
    print("  (editable — tweak desktops/screen/zone or delete entries you don't care about)")
    return True


def _match_entries(entries, live):
    """Map each template entry to a live window, all via KWin (no tab runner).

    Match by app class; if the entry has a `tab` substring it is REQUIRED to be
    in the window's caption (its active-tab title) — this is how we pick the right
    browser/window. `title_hint` is a soft preference among same-class windows.
    Returns (placements, skips) where placements are (entry, window)."""
    used = set()
    placements, skips = [], []
    for e in entries:
        required = e.get("tab")
        pref = e.get("title_hint")
        cands = [w for w in live if w["cls"] == e["app"] and w["id"] not in used]
        if required:
            cands = [w for w in cands if required.lower() in (w.get("caption") or "").lower()]
        if not cands:
            why = f"no open {e['app']} window" + (f" with caption containing {required!r}" if required else "")
            skips.append((e, why))
            continue
        if pref:
            cands.sort(key=lambda w: 0 if pref.lower() in (w.get("caption") or "").lower() else 1)
        chosen = cands[0]
        used.add(chosen["id"])
        placements.append((e, chosen))
    return placements, skips


def _zone_str(z):
    return z["type"] + (f":{z['index']}" if z.get("type") == "tile" else "")


def _browser_matches(icon, app):
    """The runner reports the browser by icon name ('firefox', 'brave-browser')
    while a template entry's `app` is the window class ('org.mozilla.firefox').
    Match them tolerantly."""
    icon = icon.lower()
    key = icon.replace("-browser", "").replace("-", "")     # firefox / brave / chromium / googlechrome
    return icon == app.lower() or key in app.lower().replace(".", "").replace("-", "")


def _find_browser_tab(entry):
    """Best tab (across all browser services) matching a browser entry's `tab`
    fragment. Returns (tabId, title, browser, service) or None."""
    frag = entry.get("tab", "").lower()
    for tab_id, title, browser, service in match_tabs(entry["tab"]):
        if frag in title.lower() and _browser_matches(browser, entry["app"]):
            return (tab_id, title, browser, service)
    return None


def apply_preferred(key=None, dry_run=False, settle_ms=350):
    key = key or state_key()
    p = pref_path(key)
    if not os.path.exists(p):
        print(f"apply-preferred: no preferred layout for state {key!r} (run: wp.py save-preferred)")
        return False
    with open(p) as f:
        entries = json.load(f).get("place", [])

    browser_entries = [e for e in entries if "tab" in e]
    app_entries = [e for e in entries if "tab" not in e]

    # Browsers: locate the tab across ALL services; Run() will focus it + raise its window.
    browser_plan, browser_skips = [], []
    for e in browser_entries:
        hit = _find_browser_tab(e)
        (browser_plan if hit else browser_skips).append((e, hit))

    # Apps: match an open window by class (+ optional title_hint) via KWin.
    live = capture_layout() or []
    app_placements, app_skips = _match_entries(app_entries, live)

    for e, hit in browser_plan:
        print(f"  ✓ {e['app']:22} focus tab -> desk {e['desktops']} / {e['screen']} / {_zone_str(e['zone'])}   [{hit[1][:45]}]")
    for e, _ in browser_skips:
        print(f"  · skip {e['app']:20} (no open tab matching {e['tab']!r})")
    for e, w in app_placements:
        print(f"  ✓ {e['app']:22}           -> desk {e['desktops']} / {e['screen']} / {_zone_str(e['zone'])}   [{(w.get('caption') or '')[:45]}]")
    for e, why in app_skips:
        print(f"  · skip {e['app']:20} ({why})")

    total = len(browser_plan) + len(app_placements)
    if dry_run:
        print(f"[dry-run] would place {total} / {len(entries)} entries for {key!r}")
        return True

    # Execute: focus each browser tab (raises its window) then place the active window.
    settle = settle_ms / 1000.0
    for e, hit in browser_plan:
        raise_tab(hit[0], hit[3])
        time.sleep(settle)
        run_kwin_script(PREF_ACTIVE_JS.replace("%ENTRY%", json.dumps(e)))

    # Place app windows by internalId in one pass.
    payload = [{"id": w["id"], "desktops": e["desktops"], "screen": e["screen"], "zone": e["zone"]}
               for e, w in app_placements]
    if payload:
        run_kwin_script(PREF_PLACE_BY_ID_JS.replace("%DATA%", json.dumps(payload)))

    print(f"apply-preferred: placed {total} / {len(entries)} entries for {key!r}")
    return True


def watch_snapshot(interval=2.0, settle=1.5, snap_every=4.0):
    """Re-snapshot the current state periodically; restore on dock/undock."""
    last = None
    last_snap = 0.0
    print(f"watch(snapshot): interval={interval}s snap_every={snap_every}s settle={settle}s")
    while True:
        key = state_key()
        now = time.time()
        if last is None:
            last = key
            snapshot(key, verbose=False)
            last_snap = now
            print(f"[{time.strftime('%H:%M:%S')}] learning state {key!r}")
        elif key != last:
            print(f"[{time.strftime('%H:%M:%S')}] state change {last!r} -> {key!r}")
            time.sleep(settle)              # let KWin finish its own reshuffle
            restore(key)
            last = key
            last_snap = time.time()
        elif now - last_snap >= snap_every:
            snapshot(key, verbose=False)
            last_snap = now
        time.sleep(interval)


# ---------- apply ----------

def apply(cfg, profile, dry_run=False):
    settle = cfg.get("settle_ms", 350) / 1000.0
    print(f"profile: {profile.get('name')}  (connected: {sorted(connected_outputs())})")
    for rule in profile.get("rules", []):
        hits = find_rule_tabs(rule)
        if not hits:
            print(f"  · no tab matches {rule['match']!r}")
            continue
        for tab_id, title, browser, service in hits:
            dest = f"desktop {rule['desktop']} / {rule['screen']} / zone {rule['zone']}"
            print(f"  → [{browser}] {title!r} -> {dest}")
            if dry_run:
                continue
            raise_tab(tab_id, service)
            time.sleep(settle)
            js = gen_kwin_script(int(rule["desktop"]) - 1, rule["screen"], rule["zone"])
            run_kwin_script(js)


# ---------- cli ----------

def load_cfg(path):
    import yaml
    with open(path) as f:
        return yaml.safe_load(f)


def main():
    ap = argparse.ArgumentParser(prog="wp.py")
    ap.add_argument("--config", default=DEFAULT_CONFIG)
    sub = ap.add_subparsers(dest="cmd", required=True)

    a = sub.add_parser("apply", help="place windows for the active (or named) profile")
    a.add_argument("--profile")
    a.add_argument("--dry-run", action="store_true")

    lt = sub.add_parser("list-tabs", help="dump matching tabs (debug)")
    lt.add_argument("query", nargs="?", default="")

    sub.add_parser("status", help="show connected outputs + chosen profile + state key")

    sub.add_parser("snapshot", help="save current window layout for the current monitor set")
    sub.add_parser("restore", help="restore the saved layout for the current monitor set")

    sub.add_parser("save-preferred", help="capture current layout as the reusable preferred template")
    apf = sub.add_parser("apply-preferred", help="tile everything into the preferred template (hotkey target)")
    apf.add_argument("--dry-run", action="store_true", help="show what would match/move, change nothing")

    w = sub.add_parser("watch", help="auto snapshot/restore (or apply rules) on dock/undock")
    w.add_argument("--mode", choices=["snapshot", "rules"], default="snapshot",
                   help="snapshot: remember+restore layouts (default); rules: apply config.yaml")
    w.add_argument("--interval", type=float, default=2.0)
    w.add_argument("--settle", type=float, default=1.5)
    w.add_argument("--snap-every", type=float, default=4.0)

    args = ap.parse_args()

    if args.cmd == "list-tabs":
        for tab_id, title, browser, service in match_tabs(args.query):
            tag = service.split("integration")[-1] or "(main)"
            print(f"{browser:16} {tag:9} id={tab_id:12}  {title}")
        return

    if args.cmd == "snapshot":
        snapshot()
        return

    if args.cmd == "restore":
        restore()
        return

    if args.cmd == "save-preferred":
        save_preferred()
        return

    if args.cmd == "apply-preferred":
        apply_preferred(dry_run=args.dry_run)
        return

    if args.cmd == "watch" and args.mode == "snapshot":
        watch_snapshot(args.interval, args.settle, args.snap_every)
        return

    cfg = load_cfg(args.config)

    if args.cmd == "status":
        prof = pick_profile(cfg)
        print(f"connected: {sorted(connected_outputs())}")
        print(f"state key: {state_key()}")
        print(f"profile:   {prof.get('name') if prof else '<none>'}")
        return

    if args.cmd == "apply":
        prof = pick_profile(cfg, args.profile)
        if not prof:
            sys.exit("no profile selected")
        apply(cfg, prof, dry_run=args.dry_run)
        return

    if args.cmd == "watch":  # --mode rules
        last = None
        print(f"watching for dock changes every {args.interval}s …")
        while True:
            conn = frozenset(connected_outputs())
            if conn != last:
                last = conn
                prof = pick_profile(cfg)
                if prof:
                    print(f"[{time.strftime('%H:%M:%S')}] outputs changed -> {sorted(conn)}")
                    try:
                        apply(cfg, prof)
                    except dbus.DBusException as e:
                        print(f"  apply failed: {e}")
            time.sleep(args.interval)


if __name__ == "__main__":
    main()
