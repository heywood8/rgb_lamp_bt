# Tray indicator — installation

## Requirements

- GNOME with the **AppIndicator and KStatusNotifierItem Support** extension enabled  
  (`appindicatorsupport@rgcjonas.gmail.com` — available in GNOME Extensions or your distro's package manager)
- Python packages: `bleak`, `numpy` (already needed by the main scripts)
- `python3-gi` with `AyatanaAppIndicator3` typelib — on Fedora/Bazzite this is `libayatana-appindicator-gtk3`

## Install

Copy the desktop entry to the right places:

```bash
# App launcher
cp desktop/rgb-lamp-tray.desktop ~/.local/share/applications/

# Autostart on login
cp desktop/rgb-lamp-tray.desktop ~/.config/autostart/
```

The `Exec` line in the file assumes the repo lives at  
`/var/home/heywood8/rgb_lamp_bt`. If yours is elsewhere, edit it first:

```bash
sed -i "s|/var/home/heywood8/rgb_lamp_bt|$(pwd)|g" \
    ~/.local/share/applications/rgb-lamp-tray.desktop \
    ~/.config/autostart/rgb-lamp-tray.desktop
```

## Run now (without relogging)

```bash
python3 lamp_tray.py &
```

A moon icon appears in the status bar. Click it to pick a mode:

| Menu item | Behaviour |
|-----------|-----------|
| **Live**    | Tracks every frame — alpha 0.80, 50 ms poll, no dead zone |
| **Fast**    | Fast transitions — alpha 0.50, 100 ms poll, 3° dead zone |
| **Regular** | Moderate — alpha 0.25, 300 ms poll, 8° dead zone |
| **Slow**    | Desk work — alpha 0.10, 1 s poll, 15° dead zone |
| **Off**     | Stops the lamp script |

Switching modes kills the running process and starts a new one automatically.
