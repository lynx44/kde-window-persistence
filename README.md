# window-persistence

Keep your window layout consistent across **docking / undocking** an external
monitor, on KDE Plasma (Wayland). Two independent models:

1. **Snapshot / restore** (default) — remember the actual window layout for each
   monitor configuration and snap it back when you plug/unplug. No rules to
   write; it just learns your arrangement.
2. **Rules** — declaratively place *browser* windows onto a virtual desktop +
   tiling zone based on which **tab** they contain (e.g. "the window with a
   Calendar tab → Desktop 3, left zone").

Built for and verified on: Fedora 44, Plasma 6.6.5, KWin 6.6.5, Wayland,
Brave + Firefox with the **Plasma Browser Integration** extension installed.

## Snapshot / restore (the "snap back where they were" model)

```bash
./wp.py snapshot      # save the current layout for the current monitor set
./wp.py restore       # restore the saved layout for the current monitor set
./wp.py watch         # daemon: keep a fresh snapshot, auto-restore on dock/undock
```

How `watch` works: when you unplug, KWin instantly yanks windows off the removed
output, so a snapshot can't be taken *after* the change. Instead the watcher
keeps a rolling snapshot of the current state (every few seconds) and, on a
dock-state change, **restores the snapshot of the state you just entered**. So
re-docking restores your last docked arrangement; unplugging restores your last
laptop arrangement. Snapshots are per monitor set, in
`~/.local/state/window-persistence/layout-<outputs>.json`.

Identity uses KWin's stable per-window `internalId`, so it works for the life of
a session (windows survive plug/unplug). After a full reboot the ids change and
old snapshots are ignored — the watcher just re-learns.

**Fidelity:** verified by scramble→restore→compare — **16/17** windows snap back
to their exact rectangle, output, and desktop. Wayland-native windows restore
perfectly; some **XWayland** windows (and self-positioning tray apps like
Trayscale) can resist programmatic geometry and may not move. Tile *membership*
is reattempted but, because reassigning it is unreliable on Plasma 6.6, the saved
**geometry** is what guarantees the position.

## Fit (rescue off-screen / oversized windows)

When you undock, KWin moves windows off the removed monitor but keeps their
*size* — so a window sized for a big external can hang off the smaller laptop
screen. New windows also open "pseudo-maximized" to the full screen height and
spill behind the panel. `fit` clamps any floating window to its screen's
**maximize area** (which excludes panels — e.g. 1600×954 on a 1600×1000 laptop):

```bash
wp fit            # resize/move anything off-screen or oversized back into view
wp fit --dry-run  # show what it would change, touch nothing
```

It leaves tiled, fullscreen, and minimized windows alone. The `watch` daemon
runs `fit` automatically right after each dock change, so this is mostly
hands-off; the manual command is for when you open something new and it spills.

Windows that are *flagged* maximized but still sized for the monitor you left are
re-maximized (see below) rather than clamped, so they stay genuinely maximized
instead of becoming a plain window that merely looks like one. A maximized window
that **under**-fills the area is left alone — that's an app honouring size hints
(KRDC, terminals with cell increments), and forcing it would never converge.

## Maximizing the way the OS does

Placing a window "maximized" — via a `maximize` zone, `restore`, or `fit` — goes
through one helper that reproduces exactly what the titlebar button does. Two
rules, both of which used to be broken and are the reason maximized windows came
back oversized from a dock change:

1. **Maximize to the panel-aware area, not the output rect.** `output.geometry` is
   the whole screen *including* the panel (1600×1000 on this laptop);
   `workspace.clientArea(MaximizeArea, output, desktop)` excludes it (1600×954).
   Using the former left every maximized window 46px too tall, with its bottom
   edge hidden behind the taskbar.
2. **Never write `frameGeometry` while the window is still maximized.** KWin treats
   that write as the *new* maximized rectangle, so the window keeps the oversized
   geometry **and** stays flagged maximized — which in turn makes
   `setMaximize(true, true)` a no-op. That is precisely why such a window would
   only snap to the right size when you clicked maximize by hand. Unmaximizing
   first (the flag flips synchronously, even though the client-side resize is
   async) makes the write safe and the whole operation idempotent.

## How it works

| Step | Mechanism |
|------|-----------|
| Which layout? | Reads connected outputs from `/sys/class/drm/*/status` and picks the first matching `profile` in `config.yaml`. |
| Which window has tab "X"? | Asks the browser-integration **Tabs KRunner** over D-Bus: `org.kde.plasma.browser_integration /TabsRunner org.kde.krunner1.Match "X"` → returns `(tabId, title, browser…)` for **every** open tab (not just active ones). |
| Raise that window | `…/TabsRunner …Run <tabId> ""` switches the browser to that tab **and** raises its window — the only reliable way to locate a *background* tab's window, since a window's title only reflects its active tab. |
| Move it | A generated KWin script loaded via `org.kde.KWin /Scripting` sets `window.desktops` and `window.tile` (custom-tiling zone) on the now-active window — or maximizes it. |

## Preferred layout (the "reset everything to my desired state" hotkey)

For the common case — keep whatever you had day-to-day, but after a reboot or a
fresh dock switch, hit one key to tile everything into your canonical layout.
Unlike a snapshot (keyed on per-window ids, session-only), the preferred layout
is keyed on **app class** (and, for browsers, a **tab**), so it survives reboots
and freshly-launched windows.

```bash
# 1. arrange your windows exactly how you like them, once
./wp.py save-preferred         # writes an editable template for the current monitor set
                               #   -> ~/.config/window-persistence/preferred-<outputs>.json
# 2. bind a hotkey to:
./wp.py apply-preferred        # re-match live windows and tile them into the template
```

Reliability, by window type:
- **Non-browser apps** (Konsole, Kate, IDEs, KRDC, Signal, …): matched by app
  class via KWin and tiled exactly — rock solid.
- **Browser windows**: identified by **any tab they contain** (`"tab"` field) —
  *including background tabs*. On apply, that tab is **focused** (which also
  raises its window), then the window is placed. So your browser windows get set
  up on the right tab *and* the right zone.

How browser tabs are found: each browser instance/profile registers its **own**
`org.kde.plasma.browser_integration[-<pid>]` D-Bus service (a KRunner "Tabs"
provider). The catch that took a while to find: you must query **all** of them —
the bare service name is just one instance, so querying only it misses other
windows/profiles (and Firefox). `wp list-tabs` aggregates across all services;
the second column shows which service a tab came from.

Tips for the `"tab"` field: use a short, stable fragment that's always in that
tab's title (`support@standardedge.com`, not `Inbox (3,658) - support@… - …`).
`save-preferred` defaults it to the full title, so trim volatile bits like unread
counts. Use `wp apply-preferred --dry-run` to see exactly what matches, what's
skipped, and why — without moving anything.

The template is plain JSON — edit `desktops`/`screen`/`zone` or delete entries
for windows you don't want the hotkey to touch. `zone` is one of
`{"type":"tile","index":N}`, `{"type":"maximize"}`, or `{"type":"free","geom":[x,y,w,h]}`.

## Rules model (tab → desktop/zone) usage

```bash
./wp.py status                 # connected outputs + state key + active profile
./wp.py list-tabs [QUERY]      # debug: dump matching tabs (tabId / browser / title)
./wp.py apply --dry-run        # show what WOULD move, move nothing
./wp.py apply                  # actually place the windows per config.yaml
./wp.py apply --profile laptop # force a specific profile
./wp.py watch --mode rules     # loop: re-apply rules whenever you dock/undock
```

## Configuration (rules model)

Edit `config.yaml`. Profiles match top-to-bottom; first whose `when_connected`
outputs are all connected wins (put a bare `when_connected: []` fallback last).

A `zone` is a **tile index** into that screen's custom-tiling layout (the tiles
you defined with **Meta+T**), counted left-to-right, or the literal `maximize`.
Inspect the live tile geometry any time with `./wp.py` + the probe, or just open
the Meta+T editor.

Your current live tiles (docked):
- `eDP-1` (laptop): 2 tiles (left/right halves)
- `DP-2` (ultrawide): 2 tiles (left/right halves)

Add more tiles in the Meta+T editor to get more zones; their indices follow the
editor's left-to-right order.

## Auto-run on dock/undock

```bash
cp systemd/window-persistence.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now window-persistence.service
journalctl --user -u window-persistence -f      # watch it react
```

The service runs `watch --mode snapshot` by default (the snapshot/restore model).
For the rules model instead, change `ExecStart` to `… watch --mode rules`.
It polls the monitor set every 2s and acts on change — no root, no udev rule
needed. (A udev rule is possible for instant reaction but needs sudo and is
fiddlier across the user/session-bus boundary; polling is simpler and reliable.)

## Bind to a hotkey

System Settings → Shortcuts → Custom Shortcuts → add command shortcuts running:

```
/home/matt/git/window-persistence/wp.py restore     # snap to saved layout
/home/matt/git/window-persistence/wp.py apply        # apply tab rules
```

(You already drive scripts this way — see your existing `firefox_tab_search` /
`focus_app` shortcuts.)

## How the internals were verified (KWin 6.6 specifics)

- Tabs come from the browser-integration **Tabs KRunner**: `Match(query)` returns
  `(tabId, title, browser…)` for every open tab; `Run(tabId, "")` raises the
  window. There is **no `GetTabs`** method (a common wrong guess).
- Window moves use a generated KWin script via `org.kde.KWin /Scripting`. Note:
  `loadScript` is **overloaded** (`s` / `ss`) — call it with an explicit `ss`
  D-Bus signature or dbus-python picks the wrong one.
- Tiling is **per (output, virtual desktop)**: `workspace.rootTile(output, desktop)`
  (the no-arg / single-arg forms throw "Insufficient arguments"). `tilingForScreen`
  is deprecated.
- KWin scripts can't do file I/O; the snapshotter exfiltrates layout JSON back to
  Python via `callDBus(...)` to a small session-bus sink. `print()` is swallowed
  by journald's log level — throwing an `Error` surfaces debug output at warning
  level.
- `window.frameGeometry` accepts a plain `{x,y,width,height}` object; reads of it
  *within the same script* are stale (changes apply after the script returns).

## Notes / limits

- Some **XWayland** windows and self-positioning tray apps resist programmatic
  geometry; they may not snap back. Wayland-native windows restore exactly.
- (Rules model) if several open tabs match one rule, every matching tab's window
  is placed — make `match:` strings specific.
- (Rules model) `Run` switches the matched browser window to the matched tab;
  that's required to locate and raise it, and isn't avoidable via this API.
