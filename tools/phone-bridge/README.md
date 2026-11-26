# Night-Shift Phone Status Bridge Setup

Check an overnight run from your phone in one Google Doc (`NIGHTSHIFT_LIVE_STATUS`) without RDP, Google Cloud credentials, or admin rights.

## How it works

1. Night-Shift Desktop sends a compact, redacted summary (project, current phase, needs-you items, done tonight, next up) to your own Google Apps Script web app.
2. No log lines, file paths, prompts, or diffs are ever sent.
3. Your Apps Script web app checks a shared secret, writes one dark status card per project into `NIGHTSHIFT_LIVE_STATUS`, and marks a running project **SILENT** if no update arrives for 25 minutes.

## One-time setup (about 3 minutes)

1. Open [script.google.com](https://script.google.com/) and click **New project**.
2. Delete the starter code in `Code.gs`, paste the contents of `tools/phone-bridge/Code.gs`, and save (`Ctrl+S`).
3. Open **Project Settings** (gear icon on the left) -> **Script Properties** -> **Add script property**:
   - **Property:** `NIGHTSHIFT_BRIDGE_SECRET`
   - **Value:** choose any long random passphrase (you will paste this same secret into Night-Shift Desktop).
4. Click **Deploy** -> **New deployment**:
   - **Select type:** Web app
   - **Execute as:** Me
   - **Who has access:** Anyone
   - Click **Deploy**, authorize Drive/Docs access for your own account, and copy the **Web app URL** (ends with `/exec`).
5. *(Optional)* To mark a project **SILENT** automatically even if your PC loses power or sleeps, open **Triggers** (clock icon) -> **Add Trigger** -> function `refreshSilentCards` -> **Time-driven** -> **Every 10 minutes**.
6. In Night-Shift Desktop, open **Settings** -> **Phone Status Bridge**:
   - Paste the **Web app URL** and **Shared secret**.
   - Toggle **Enable phone status bridge** on.
   - Click **Send test update** and open `NIGHTSHIFT_LIVE_STATUS` in Google Docs on your phone.
