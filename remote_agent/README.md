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

## 3. Over the internet (two different locations)

Don't port-forward. Run an authenticated tunnel to the agent's port instead —
this is the AnyDesk-style approach (no open router port):

```bash
# Example with cloudflared (free):
cloudflared tunnel --url http://localhost:8770
# It prints an https URL — use THAT as remote_agent_url in Parker.
```

## ⚠️ Security

This agent is remote access to the machine. With `--allow-exec`, the token holder
can run any command. Therefore:

- Use a **long, random password**. The agent refuses short ones.
- Keep `--allow-exec` OFF unless you need it.
- Over the internet, use a tunnel with the **https URL kept private**; anyone
  with the URL + token controls the machine.
- The agent only serves files under the allowed `roots` (home by default) and
  blocks paths outside them.
