# Night-Shift Desktop

Night-Shift Desktop is a Windows app that runs your phased software plans overnight without needing administrator rights. It coordinates your AI coding tools with automatic failover, checks continuous integration, merges verified pull requests, and leaves a plain-English morning digest.

## Install on Windows (no administrator rights needed)

1. **Download** `NightShift-windows.zip` from the latest GitHub Release or the `nightshift-windows` build artifact.
2. **Unzip** the contents into `%LOCALAPPDATA%\NightShift` (you can paste `%LOCALAPPDATA%\NightShift` into the File Explorer address bar and press Enter).
3. **Run** `NightShift.exe` inside that folder.
4. **Pin to taskbar**: right-click the Night-Shift icon on your Windows taskbar and select **Pin to taskbar**.

Night-Shift does not require an installer, system services, registry changes, or administrator approval.

## First run

When you open Night-Shift for the first time with no projects connected:

- If you have a `queue.json` file from the older Night-Shift script, click **Import legacy queue.json** on Mission Control to bring in your project, phases, and failover ladders automatically.
- Otherwise, click **Start fresh** and add a project in **Settings**.

## System tray and autostart

- **System tray icon**: right-click the Night-Shift icon in the Windows system tray (near the clock) to **Open** the window, toggle **Stop after current phase**, or **Quit** cleanly.
- **Start on Windows sign-in**: open **Settings** and turn on **Start Night-Shift on Windows sign-in (per-user)**. This creates a per-user Windows Task Scheduler entry with limited rights and starts Night-Shift minimized to the tray.

## Where your data is stored

All Night-Shift data (SQLite database, logs, lock files, and Windows DPAPI-encrypted API keys) is stored strictly inside `%LOCALAPPDATA%\NightShift`. Nothing is written to system folders. You can open this folder at any time from **Settings -> Open data folder**.

## Verifying the package

To confirm a packaged build starts cleanly without opening a window, run:

```powershell
& "$env:LOCALAPPDATA\NightShift\NightShift.exe" --self-test
```

It boots the engine, checks core services, prints `OK`, and exits with code `0`.

## For contributors and AI agents

Read [AGENTS.md](AGENTS.md), [docs/BLUEPRINT.md](docs/BLUEPRINT.md), and [docs/DESIGN_SYSTEM.md](docs/DESIGN_SYSTEM.md) before making changes.
