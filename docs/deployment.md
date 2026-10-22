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
sudo chown -R 10001:10001 data && sudo chmod 600 data/.env   # patchbay runs as uid 10001, not root
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

In `data/.env` (`sudoedit data/.env`; the file is private to uid 10001), find the three lines (they ship commented out), uncomment
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

If you leave sign-in off (`PATCHBAY_AUTH=none`), also set
`PATCHBAY_ALLOWED_HOSTS` in `data/.env` to the names you open patchbay by,
for example `PATCHBAY_ALLOWED_HOSTS=patchbay.example.net`, and restart the
`patchbay` service. Otherwise a malicious web page can reach the UI through
your browser, and `/ops` shows a warning saying so.
[configuration.md](configuration.md#allowed-hosts) covers reverse proxies.

## 7. Verify snapshot delivery

A snapshot earns its keep only when it sits somewhere the tool host's
failure can't reach. Setting `PATCHBAY_SNAPSHOT_DELIVER_DIR` is half of that.
The other half is proving that the copy arrives and that you can open it with
the stack down. Do this once at install and again after any change to the
share or the sync.

1. Mount the off-host share into **both** patchbay services (the nightly
   snapshot runs in the poller, the on-demand one in the web UI), then name
   it in `data/.env` by the path the container sees:

   ```yaml
   volumes:
     - ./data:/data
     - /mnt/offsite:/offsite       # NAS share, cloud-synced folder, …
   ```

   ```
   PATCHBAY_SNAPSHOT_AT=03:30
   PATCHBAY_SNAPSHOT_DELIVER_DIR=/offsite/patchbay
   ```

   Restart `patchbay` so the web UI rereads the file.

2. Trigger a snapshot now instead of waiting for the nightly run: click
   **snapshot now** on `/snapshots`, or run
   `docker compose -f docker-compose.stack.yml exec patchbay patchbay snapshot`.
   Success ends with `delivered to <dir>` (the CLI prints `-> <dir>`). A
   `written locally, delivery failed` line names the cause, usually a mount
   that isn't there or a directory the container can't write. The local
   snapshot exists either way.

3. Look at the share from another machine, not from inside the container.
   Expect `patchbay-latest.html` and a timestamped copy, and no `.part`
   file: each copy lands under a temporary name and is renamed, so a
   lingering `.part` is an interrupted copy.

4. If a sync client carries the share onward, wait for it to report the
   file and confirm the cloud copy too. Sync is the step this check exists
   for: a share that received the file is not yet a copy that survives the
   site.

5. Prove the done-when. Stop the stack with
   `docker compose -f docker-compose.stack.yml down`, open the off-host
   `patchbay-latest.html` in a browser, and check that the map, a device
   page, and a port's detail all render. Then `up -d`.

Repeat step 3 the morning after the first scheduled run. The nightly path
is the poller's, with its own mount and its own `TZ`, so a working
on-demand snapshot doesn't prove it.

### Size the share for retention

Each snapshot prunes both the local directory and the share by the tiers in
`PATCHBAY_SNAPSHOT_KEEP`. The default, `30,12m,3y,first`, settles at about
46 files per directory: 30 nightlies, the first of each of the last 12
months and 3 years, and the first snapshot ever taken. At about 5 MB each,
leave the share at least 250 MB. If your sync client keeps deleted files in
a trash or version history, pruned nightlies count against that quota too.
With `PATCHBAY_ALERT_SNAPSHOT` on, alert snapshots add up to their own
keep count (`PATCHBAY_ALERT_SNAPSHOT_KEEP`, default 10) on top.

Check the tiers on `/snapshots`: each file shows the tier that keeps it, and
the page states the effective spec. A spec that fails to parse prunes
nothing and shows a warning there and on `/ops`; see
[Snapshot retention](configuration.md#snapshot-retention) for the syntax.

## 8. Notifications

patchbay tells you when something goes wrong through *transports*: webhooks
that you configure on **Alerts → Transports**. No environment variable is
involved. The database stores each transport, and the page is the only place
to edit it. Two presets ship:

| Preset | What patchbay sends | Use it for |
|---|---|---|
| `kuma` | `GET` to an Uptime Kuma push URL every poll: `status=down` while any active alert routed to it meets the route's minimum severity, `status=up` otherwise | Paging. Kuma owns the notification providers (email, chat, phone push), and the monitor also goes down if the poller stops running. |
| `generic` | A JSON `POST` when an alert raises, reminds, or clears, and for each one-shot event | n8n, Home Assistant, or a script of your own |

A *route* is a transport plus a minimum severity. The first transport you
add becomes the default route for warnings and above, so one transport is
enough to start. You can pick another default or route an individual rule
elsewhere under **Routes** on the same tab. `slow-link` routes nowhere by
default: it stays on the attention list without paging anyone.

A transport URL is a credential: Kuma's push token is part of the path.
patchbay stores the URL in its database, shows only the scheme and host
after you save it, and keeps it out of logs, poll output, alert history,
and snapshots. To change a URL, paste a new one in the transport's edit
form; leaving the field empty keeps the stored URL.

### Repeat a notification while an alert stays active

By default, patchbay notifies when an alert raises, when it escalates to a
higher severity, and when it clears. To hear about an alert that stays active, open **Alerts → Rules** and
set **remind every (minutes, 0 = never)** on the rule. patchbay then sends
the notification again each time that many minutes pass since the last notification for that alert
(a raise, an escalation, or an earlier remind), until the alert clears. The default is 0, which never repeats. Event rules
(`config-changed`, `snapshot-failed`) have no field, because they fire once.

Reminds reach `generic` transports. A Kuma monitor needs none: patchbay
pushes its state on every poll, so it stays down for as long as an alert
holds it. A silenced alert sends no reminds. Reminds go out on a poll, so
an interval shorter than your poll interval sends one per poll.

### Set up an Uptime Kuma push monitor

1. In Uptime Kuma, click **Add New Monitor** and set **Monitor Type** to
   **Push**.
2. Set **Heartbeat Interval** to at least twice the poll cycle. The poller
   pushes once per cycle, and a cycle is the poll itself plus
   `PATCHBAY_POLL_INTERVAL` (default 300 seconds), so `600` suits the
   default. A shorter interval reports the poller down between healthy
   pushes.
3. Attach the notification providers that should hear about patchbay's
   alerts, then save, and copy the **Push URL**. It looks like
   `https://kuma.example.com/api/push/<token>?status=up&msg=OK&ping=`.
   Paste it as Kuma shows it; patchbay replaces the `status` and `msg`
   parameters on each push.
4. In patchbay, open **Alerts → Transports → add a transport**. Enter a
   name, choose **Uptime Kuma push monitor**, paste the push URL, and click
   **add**.
5. Click **test**. The transport's last result reads `test: HTTP 200` and
   Kuma records a heartbeat whose message starts with `patchbay test:`. The
   test pushes the monitor's current state, so it never flips a down
   monitor up.

Kuma now shows patchbay's state on every poll. While the monitor is down,
its message names the alert count and the worst alert, with its severity,
rule, text, how long it has fired, and a link. For the link to be absolute,
set **link base** under **Routes** to the address your browser uses for
patchbay.

One Kuma monitor serves one route. To page on critical alerts and only log
warnings, add two push monitors as two transports, and route each rule, or
the default, to the transport that should hear it.

### When delivery fails

Every request times out after 5 seconds, and the transports together get at
most 20 seconds per poll, so a slow receiver delays the poll's exit, never
its data. A generic notification that fails waits for the next poll and is
sent then. Each attempt is recorded in **History** as `notified` or
`delivery_failed`. After three consecutive failed polls, the transport
itself becomes a `sources` item on the attention list, showing its last
error, until a delivery succeeds. That item follows the default route like
any other rule. When the failing transport is the default, the attention
list still shows the item, and a Kuma monitor that stops receiving pushes
goes down on its own.

## Container user and upgrading

The image runs as an unprivileged user, uid/gid `10001`, and never changes
file ownership. A fresh named volume works as is. A bind-mounted `./data`
must be writable by the container's uid: the quickstart's
`sudo chown -R 10001:10001 data && sudo chmod 600 data/.env` does that
for a new install, and a `./data`
from an earlier release (which ran as root, so `patchbay.db` and
`snapshots/` are root-owned) needs the same treatment. Without it the
container refuses to start and prints the fix.

**One-time step when upgrading** from a release that ran as root:

```sh
docker compose down
sudo chown -R 10001:10001 data
sudo chmod 600 data/.env
docker compose pull && docker compose up -d
```

Use your compose file's `-f` flag where you need it. `data/.env` holds
every collector credential, so it stays mode 600 and owned by 10001; edit it
with `sudoedit data/.env`. patchbay only reads that file: the declarations
you save on /ops go to the database, so the file never needs to be writable
by the container.

**Alternative: run as your own user.** To keep `./data` owned by you, set
`user:` on both patchbay services (web and poller) to your uid:gid, and
re-own the files the old image created as root:

```sh
docker compose down
sudo chown -R "$(id -u):$(id -g)" data
chmod 600 data/.env
```

```yaml
    user: "1000:1000"      # the output of: id -u; id -g
```

For a new install the `chown` is unnecessary when `user:` matches the
owner of the `data` you just created.

`user: "0:0"` keeps the old behavior of running as root, so nothing needs
re-owning. It also drops the hardening: a code-execution bug in the web UI
then runs as root in the container, and `docker compose exec` creates
root-owned files. Use it as a stopgap, not a destination.

**Rootless Docker and Podman.** The container's root maps to your host
user, so `user: "0:0"` is the right setting there: files in `./data` stay
owned by you and the process has no real privilege on the host. The
default 10001 maps to a subordinate uid you cannot edit without
`podman unshare`.

**Synology and other hosts with ACLs.** On a Synology share, `ls -ld`
shows a `+` after the mode: an ACL decides access, and it grants only DSM
users, so uid 10001 can't write even after the `chown`. The startup error
says so when the owner already matches. Remove the ACL, then set plain
modes (run from the folder that holds your data directory, stack stopped):

```sh
sudo find data -exec synoacltool -del {} \;    # other hosts: sudo setfacl -bR data
sudo chown -R 10001:10001 data
sudo find data -type d -exec chmod 700 {} \;
sudo find data -type f -exec chmod 600 {} \;
```

Files the container creates afterward inherit no ACL. A host-side job that
reads the snapshots, such as an off-host push, must then run as root or as
uid 10001.

**Other mounts.** Anything else you mount into the patchbay services must be
writable by the same uid: a custom `PATCHBAY_SNAPSHOT_DIR`, and the off-host
snapshot share of [step 7](#7-verify-snapshot-delivery). If delivery fails
with `Permission denied` after an upgrade, `chown` the share to that uid or
loosen its mode. The startup check covers `/data` and the snapshot
directory only.

The stack publishes Oxidized's REST port on `127.0.0.1:8888` only, for
debugging from the host. patchbay never needs it (it uses
`http://oxidized:8888` on the compose network), and oxidized-web has no
authentication, so keep it off other interfaces. An upgrade that changes
this recreates the `oxidized` container; if you reached its API from another
machine, put an authenticating reverse proxy in front or use an SSH tunnel
to the host.

## Back up and restore

`./data/patchbay.db` holds state that nothing can regenerate: your
notification webhook URLs, silences, alert rules, the key that redacts config
history, and the history itself. The next poll rebuilds the device model, but
not these. Back the file up with `patchbay backup`, not with `cp`: while the
poller writes, a plain copy can capture a half-finished transaction, but
`backup` uses SQLite's backup API and always produces a consistent database.

**The backup file contains secrets.** Treat it like `data/.env`. `patchbay
backup` creates it with mode 600 and refuses to write inside
`PATCHBAY_SNAPSHOT_DELIVER_DIR`, because the off-host share is meant to be
read by a sync client. Encrypt a backup before it leaves your control.

### Back up

Run the command inside the container. It runs as uid 10001, the owner of
`/data`, and the stack keeps running:

```sh
docker compose -f docker-compose.stack.yml exec patchbay \
  patchbay backup /data/backups/patchbay-$(date +%F).db
```

The command creates `/data/backups` (mode 700) if needed, prints the path and
size, and exits non-zero on failure. Pass a directory instead of a file to get
a timestamped name, and `--force` to overwrite an existing file.

On the host, the file appears at `data/backups/` owned by 10001 with mode 600,
so copy it with `sudo`. The `cp` keeps mode 600, which keeps the copy private,
and the `chown` makes it yours to read:

```sh
sudo cp data/backups/patchbay-2026-01-01.db /safe/place/
sudo chown "$USER" /safe/place/patchbay-2026-01-01.db
```

A backup of `patchbay.db` doesn't cover everything. Also back up
`data/.env` (collector credentials) and `oxidized/repo/` (device config
history), per [Where the state lives](#where-the-state-lives).

To schedule backups, run `exec` from the host's cron or a systemd timer and
prune old files yourself. Give it the directory form, which supplies the
timestamped name. A `%` has a special meaning in a crontab, so the dated
filename above doesn't work there:

```sh
0 4 * * * docker compose -f /srv/patchbay/docker-compose.stack.yml exec -T patchbay patchbay backup /data/backups/
```

A trailing slash makes `backup` treat the path as a directory and create it
if needed. Use `-T` because cron has no terminal.

If you use `docker-compose.example.yml` (patchbay alone), drop the `-f ...`
flag from the commands in this section; run them from the directory that holds
the compose file.

### Restore

These steps assume the bind-mounted `./data` of the quickstart. Stop **both**
patchbay services first, so nothing holds the database open:

```sh
docker compose -f docker-compose.stack.yml stop patchbay patchbay-poller
sudo cp /safe/place/patchbay-2026-01-01.db data/patchbay.db
sudo rm -f data/patchbay.db-wal data/patchbay.db-shm data/patchbay.db-journal
sudo chown 10001:10001 data/patchbay.db
sudo chmod 600 data/patchbay.db
docker compose -f docker-compose.stack.yml start patchbay patchbay-poller
```

Removing the `-wal`, `-shm`, and `-journal` files matters: a leftover one
from the replaced database can corrupt the restored file. If you run
as your own user instead of 10001, use that uid in the `chown`
([Container user](#container-user-and-upgrading)).

Open `/ops`. The next poll refreshes the device model, so the restored
database is only as old as its backup for the state that polling can't
rebuild.

## Upgrade

1. Back up, as in [Back up](#back-up). If the running image predates
   `patchbay backup` (0.22.0 and earlier), stop both services and copy the
   file instead, keeping its mode:

   ```sh
   docker compose -f docker-compose.stack.yml stop patchbay patchbay-poller
   sudo cp -p data/patchbay.db /safe/place/patchbay-before-upgrade.db
   ```

   Restart them with `up -d` in step 3.
1. Read the **Upgrade notes** in the [CHANGELOG](../CHANGELOG.md) for every
   release between your version and the new one. Some releases change the
   container user or migrate stored data on first start.
1. Pull the new image and recreate the containers:

   ```sh
   docker compose -f docker-compose.stack.yml pull
   docker compose -f docker-compose.stack.yml up -d
   ```

1. Open `/ops`: the header shows the running version, and every source
   should report `[ok]` after the first poll. If something is wrong, roll back
   by pinning the previous image tag and [restoring](#restore) the backup.

## Where the state lives

| Data | Where | Survives `compose down`? |
|---|---|---|
| LibreNMS database + RRD graphs | named volumes `dbdata`, `lnms_data` | yes — destroyed only by `down -v` |
| Device config history (git) | `./oxidized/repo/` | yes — and nothing can regenerate it; back it up |
| patchbay model, silences, rules, webhooks + snapshots | `./data/` | yes — `patchbay.db` can't be regenerated; [back it up](#back-up-and-restore) |
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
