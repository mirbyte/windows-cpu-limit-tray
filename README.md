# Windows CPU Limit Tray

<img width="500" height="340" alt="gui" src="https://github.com/user-attachments/assets/2c83bc8b-22b8-40fb-83f8-cc8cc11d119c" />

<br>
<br>

A small Windows tray utility for quickly changing the active power plan's **CPU maximum processor state** while plugged in.

Useful when a desktop or laptop CPU does not lower clocks properly at idle or light use. Instead of relying on the minimum processor state, this app lets you quickly cap the CPU's maximum allowed state from the tray, then restore it to 100% when needed.

## Status

Beta / initial public release.

This app changes a real Windows power setting. Test it on your own machine before relying on it.

## Features

* Change the **AC / plugged-in** CPU maximum processor state
* Preset buttons from 5% to 100%
* Optional small overlay when the CPU limit is below 100%
* System tray menu
* Warning before quitting while below 100%
* **Restore 100% and Quit** option

## What it does not do

* Does not change battery/DC CPU settings
* Does not automatically request administrator permissions (shouldn't require admin)
* Does not directly control BIOS, firmware, undervolting, TDP, or thermal limits

## Requirements

* Windows 11 (Windows 10 should work too)
* Python 3.10 or newer recommended
* PySide6

## Install

```bash
pip install -r requirements.txt
```

## Run

```bash
python windows_cpu_limit_tray.pyw
```

Or double-click `windows_cpu_limit_tray.pyw` if Python is associated with `.pyw` files.

## Restore CPU to 100%

Use the **100%** button in the app, or right-click the tray icon and choose **Restore 100% and Quit**.

Manual restore command:

```bash
powercfg -setacvalueindex SCHEME_CURRENT SUB_PROCESSOR PROCTHROTTLEMAX 100
powercfg -setactive SCHEME_CURRENT
```

If Windows blocks the change, try closing the app and running it as Administrator. Some managed devices may block power setting changes by policy.

## Logs

The app writes `windows_cpu_limit_tray.log` next to the script when possible, or in the system temporary directory as a fallback.


<br>
<br>
<br>

<img width="1277" height="332" alt="tray" src="https://github.com/user-attachments/assets/6867424d-7c90-4453-b4d9-41c6a351973d" />

