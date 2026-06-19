# Running Claude Code inside the dev container

Goal: one environment — Claude Code runs **inside** the ha-core dev container, with
the whole project + agent context + (optionally) live homelab access. This removes
the ssh/scp/`/tmp` friction of driving the container from the Windows host.

## Starting point (current `.devcontainer/devcontainer.json`)
- `workspaceFolder`: `/workspaces/ha-core`
- mounts: **only the package** —
  `…/homeassitant/necromancer/custom_components/necromancer` → `config/custom_components/necromancer`
- so the repo's `tests/`, `docs/`, `.git` and the homelab context (`CLAUDE.md`,
  `network_detail.md`, `ha_detail.md` under the parent `homeassitant/`) are **not**
  in the container, and there's no outbound SSH key (LAN is reachable though).

## Changes

### 1. Mount the whole project (not just the package)
Replace the package mount with the parent project, and persist Claude's config:
```jsonc
"mounts": [
  // whole project: necromancer repo (tests/docs/.git) + homelab context
  "source=/mnt/c/Users/thoma/claude code projects/homeassitant,target=/workspaces/homeassitant,type=bind,consistency=consistent",
  // persist `claude login` / settings across rebuilds
  "source=necromancer-claude-config,target=/home/vscode/.claude,type=volume"
]
```

### 2. Symlink the integration into HA's config + install Claude Code
```jsonc
"postCreateCommand": "git config --global --add safe.directory ${containerWorkspaceFolder} && git config --global --add safe.directory /workspaces/homeassitant/necromancer && ln -sfn /workspaces/homeassitant/necromancer/custom_components/necromancer /workspaces/ha-core/config/custom_components/necromancer && npm i -g @anthropic-ai/claude-code && script/setup"
```
HA keeps loading the integration via the symlink; the repo (tests/docs/git) is now
at `/workspaces/homeassitant/necromancer`, the homelab docs at `/workspaces/homeassitant`.

### 3. Auth
- **`claude login`** once — persisted by the `.claude` volume above (recommended), **or**
- set `ANTHROPIC_API_KEY` (e.g. `"remoteEnv": { "ANTHROPIC_API_KEY": "${localEnv:ANTHROPIC_API_KEY}" }`).

### 4. (Optional) Live homelab access for deploys
LAN already reachable (`192.168.1.8:8123` → 200), so the HA **REST API + a token**
works now. For `ssh`/`scp` to live (`ha.meg`, `pve`, `synology`) you also need keys:
```jsonc
// add to mounts — separate path so it doesn't clobber the container's own authorized_keys
"source=/mnt/c/Users/thoma/.ssh,target=/home/vscode/.ssh-homelab,type=bind,readonly"
```
Then use `ssh -F ~/.ssh-homelab/config …`. **Caveat:** container DNS won't resolve
`.meg`/`.local` — use IPs in that config (e.g. `HostName 192.168.1.8`), or skip ssh
and deploy via the Supervisor REST proxy. Leaving this out keeps the dev container
unable to touch live (safer); add it only if you want live deploys from inside.

## Using it
1. VS Code → open `homeassitant/` (or the ha-core folder) → **Reopen in Container**
   (Rebuild after editing `devcontainer.json`).
2. In the integrated terminal: `cd /workspaces/homeassitant && claude` — CWD there so
   Claude reads the homelab `CLAUDE.md` **and** sees `necromancer/AGENTS.md`.
3. Dev HA + tests + gates per [../../AGENTS.md](../../AGENTS.md). No ssh/scp needed.

## Note on memory
The Windows auto-memory is path-hashed to the Windows project path and does **not**
transfer. Durable context is therefore kept as versioned files —
[AGENTS.md](../../AGENTS.md) + [PROJECT_STATE.md](PROJECT_STATE.md) — which travel
with the mount. Keep them updated instead of relying on auto-memory.
