# Parker Remote Agent

Reach another computer (e.g. your Mac) from Parker: browse/search/**fetch files**
you forgot, read system status, and — if you enable it — run commands.

```
Parker (Windows)  ──token──►  Parker Agent  (on the Mac)
```

## 1. On the machine you want to reach (e.g. the Mac)

```bash
# From the project folder:
python remote_agent/agent.py               # files + status only (safest)
python remote_agent/agent.py --allow-exec  # ALSO allow running commands (full control)
```

First run asks you to set a **password** (10+ characters) and prints an
**AGENT ID**. Note both, and the **LAN address** it prints
(e.g. `http://192.168.1.187:8770`).

- Files are limited to your **home folder** by default (edit `roots` in
  `~/.parker_agent.json` to change).
- `--reset` recreates the password.

## 2. In Parker (config/api_keys.json)

```json
"remote_agent_url":   "http://192.168.1.187:8770",
"remote_agent_token": "AGENTID:yourpassword"
```

Now ask Parker: *"what's on my Mac"*, *"find my budget file on the Mac"*,
*"get /Users/you/Documents/report.docx from the Mac"*.
Fetched files land in `Downloads/ParkerRemote/` on the Windows PC.

## 3. Daily use — set it up once, then just leave the Mac on

### On the Mac (one-time)

```bash
# 1. Install auto-start: agent + tunnel start on login and keep the Mac awake.
bash remote_agent/install_autostart.sh
```

This keeps the Mac awake (via `caffeinate`) so the agent stays reachable, and
runs everything on login. The current public URL is always written to:

```
~/.parker_agent_url.txt
```

To stop auto-start later: `bash remote_agent/install_autostart.sh --uninstall`.

### On Windows (each time you want to use it)

Nothing to launch separately — just start **Parker as usual**. It reads
`remote_agent_url` + `remote_agent_token` from `config/api_keys.json` and talks
to the Mac on demand. Then say *"what's on my Mac"*, *"find my report on the
Mac"*, *"get <path> from the Mac"*.

### Fixed URL (recommended) — named tunnel

A quick tunnel's URL changes on every restart. To get a URL that never changes,
use a named tunnel with a domain you own on Cloudflare (one-time):

```bash
cloudflared tunnel login                       # opens the browser; pick your domain
cloudflared tunnel create parker               # creates a tunnel named 'parker'
cloudflared tunnel route dns parker mac.yourdomain.com   # your fixed hostname
```

Then create `remote_agent/tunnel.conf`:

```bash
NAMED_NAME=parker
NAMED_HOST=mac.yourdomain.com
```

Now `start_mac_agent.sh` uses the **fixed** URL `https://mac.yourdomain.com`.
Put that in Parker's `remote_agent_url` once — it never changes again.

### ⚠️ Sleep note

If the Mac **sleeps**, the agent stops. The auto-start script keeps it awake
with `caffeinate`, but if you manually put the Mac to sleep or shut it down,
Parker can't reach it until it's on again.

## ⚠️ Security

This agent is remote access to the machine. With `--allow-exec`, the token holder
can run any command. Therefore:

- Use a **long, random password**. The agent refuses short ones.
- Keep `--allow-exec` OFF unless you need it.
- Over the internet, use a tunnel with the **https URL kept private**; anyone
  with the URL + token controls the machine.
- The agent only serves files under the allowed `roots` (home by default) and
  blocks paths outside them.
