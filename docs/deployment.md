# Deploying the full stack

patchbay is an aggregator: it renders what LibreNMS, Oxidized, and your
platform APIs already collect. If you run none of those yet, this guide
stands up the whole thing — data layer and patchbay — with
[docker-compose.stack.yml](../docker-compose.stack.yml). If you already run
LibreNMS or Oxidized, skip this and use the
[quick start](../README.md#quick-start), which runs patchbay alone against
your existing instances.

The stack is seven services: MariaDB and Redis (LibreNMS's storage), LibreNMS
and its dispatcher (SNMP polling, LLDP discovery, graphs), Oxidized (config
backup to git), and the patchbay web UI and poller. This guide covers wiring
them together; for operating LibreNMS and Oxidized themselves, see the
[LibreNMS docs](https://docs.librenms.org/) and
[Oxidized docs](https://github.com/ytti/oxidized).

## 1. Prepare the directory

```sh
curl -O https://raw.githubusercontent.com/dsmorgan/patchbay/main/docker-compose.stack.yml
curl -O https://raw.githubusercontent.com/dsmorgan/patchbay/main/.env.example
mkdir -p data oxidized
cp .env.example data/.env
printf 'TZ=UTC\nMYSQL_PASSWORD=%s\n' "$(openssl rand -hex 16)" > .env
```

Set `TZ` to your zone — it's not cosmetic. `PATCHBAY_SNAPSHOT_AT` and
LibreNMS's graphs both use the container's local time.

Two files are named `.env` and they never share keys: `./.env` configures
docker compose itself (`TZ`, `MYSQL_PASSWORD`); `./data/.env` is patchbay's
site configuration, read inside the container.

## 2. Configure Oxidized

Oxidized needs two files in `./oxidized/` before it can back up configs: a
`config` naming your credentials and device models, and a `router.db` listing
devices. A minimal `config` that works with this stack:

```yaml
username: backup-user
password: backup-pass
model: junos                # default; router.db overrides per device
interval: 3600
rest: 0.0.0.0:8888          # patchbay reads config history over this API
input:
  default: ssh
output:
  default: git
  git:
    user: oxidized
    email: oxidized@example.net
    repo: "/home/oxidized/.config/oxidized/repo/configs.git"
source:
  default: csv
  csv:
    file: "/home/oxidized/.config/oxidized/router.db"
    delimiter: !!str ":"
    map:
      name: 0
      ip: 1
      model: 2
```

And a `router.db` of `name:ip:model` lines listing your real devices:

```
core-switch:192.168.1.2:powerconnect
edge-switch:192.168.1.3:ios
```

Two behaviors to know about, both verified against the current image:

- **Oxidized exits if no line yields a usable device** (`source returns no
  usable nodes`), and a name that doesn't resolve is skipped — so
  placeholder entries copied verbatim crash-loop the container. List real
  devices, and the `ip` column means the name doesn't need DNS.
- It logs that `rest:` is deprecated in favor of `extensions.oxidized-web`.
  Harmless — the API patchbay reads serves the same either way.

Use read-only device accounts — the git repo in `oxidized/repo/` is your
config history and the one thing in this stack that cannot be regenerated,
so back it up.

## 3. Start the stack

```sh
docker compose -f docker-compose.stack.yml up -d
```

First boot takes a few minutes while LibreNMS initializes its database. The
patchbay poller waits for it (up to five minutes, then polls without it), so
early `[fail]` lines for sources you haven't configured yet are expected, not
broken.

## 4. Set up LibreNMS and onboard devices

1. Open http://localhost:8000 and create the admin account.
2. Add your switches, firewall, and other SNMP-speaking devices (**Devices →
   Add Device**, or `lnms device:add` in the container). SNMPv3 where the
   gear supports it.
3. Enable LLDP (or CDP) on your switches — neighbor discovery is where the
   topology map's strongest evidence comes from.
4. Generate an API token for patchbay at http://localhost:8000/api-access.

Give LibreNMS two poll cycles (about 10 minutes) before judging the results;
port graphs and LLDP links populate on its schedule, not patchbay's.

## 5. Point patchbay at the containers

In `data/.env`, find the three lines (they ship commented out), uncomment
them, and set — edit in place rather than adding new lines, because on a
duplicate key the *last* occurrence silently wins:

```
LIBRENMS_URL=http://librenms:8000
LIBRENMS_TOKEN=<the token from step 4>
OXIDIZED_URL=http://oxidized:8888
```

Use the **service names**, not `localhost` or the host's LAN name. patchbay
runs on the same compose network, where `librenms` and `oxidized` resolve
directly — while a port published on the host's own address is typically
unreachable from inside a container, and fails as `Connection refused` one
poll later.

Add whichever platform APIs you run — UniFi, OPNsense, vSphere, phpIPAM — the
same way; those are real hosts on your LAN, so they keep their real names.
Every variable is documented in [configuration.md](configuration.md), and
nothing is required: each collector activates only when its variables are
set.

Then restart so the web UI rereads the file (the poller picks it up on its
next cycle by itself):

```sh
docker compose -f docker-compose.stack.yml restart patchbay
```

## 6. Verify

```sh
docker compose -f docker-compose.stack.yml logs -f patchbay-poller
```

Every configured source should report `[ok]`. If LibreNMS reports `401`, the
token is wrong; `Connection refused` means a URL still points at the host
instead of the service name. The same check works from inside the container's
own view:

```sh
docker compose -f docker-compose.stack.yml exec patchbay-poller python -c "
from patchbay.config import load_settings
import httpx
s = load_settings()
r = httpx.get(f'{s.librenms_url}/api/v0/devices',
              headers={'X-Auth-Token': s.librenms_token or ''}, timeout=5)
print(s.librenms_url, '->', r.status_code)"
```

Open http://localhost:8013 — the dashboard lists every device, and the
topology map draws what the first poll saw. Declare what no protocol can
discover (cables to dumb devices, patch panels, WAN ports) in `data/.env` as
you go; the [README](../README.md#operator-declarations) lists the
declarations.

## Where the state lives

| Data | Where | Survives `compose down`? |
|---|---|---|
| LibreNMS database + RRD graphs | named volumes `dbdata`, `lnms_data` | yes — destroyed only by `down -v` |
| Device config history (git) | `./oxidized/repo/` | yes — and nothing can regenerate it; back it up |
| patchbay model + snapshots | `./data/` | yes |
| LibreNMS API token | inside `dbdata` | destroyed by `down -v` — regenerate and update `data/.env` after |

## Tearing down

`docker compose -f docker-compose.stack.yml down` stops everything and keeps
all data — `up -d` resumes with history intact. Adding `-v` destroys the
LibreNMS database and graph history, **including the API token** (regenerate
it and update `data/.env` on the next install). `./data/` and `./oxidized/`
are plain directories that no compose command touches; delete them yourself,
remembering what the table above says about `oxidized/repo/`.

One deployment principle worth stealing: run this stack on a host *outside*
the failure domain it monitors. A map of the network matters most while the
network is broken, which is exactly when a VM on the affected fabric is
unreachable. Pair that with `patchbay snapshot` shipped somewhere off-host
and you can troubleshoot with everything down.
