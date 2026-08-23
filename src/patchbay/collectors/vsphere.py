"""vSphere collector (pyVmomi against vCenter).

Pulls ESXi hosts, VMs (with their host placement), and each host's physical
NIC LLDP/CDP network hints — the data that ties hypervisor uplinks to
physical switch ports (link source 'vsphere-hint').

Also records each virtual NIC's port group VLAN. A VM's own OS can't see that
tag — the vSwitch adds and strips it — so the hypervisor is the only source
that knows which VLAN a guest interface sits on.
"""

from __future__ import annotations

import sqlite3
import ssl
from typing import Any

from ..config import Settings
from .. import db
from . import register

NAME = "vsphere"

# ESXi VLAN ID conventions on a port group
VGT_TRUNK = 4095   # the guest tags its own frames; every VLAN passes
NO_VLAN = 0        # the vSwitch doesn't tag — the physical port decides


def _dvpg_vlan(pg: Any) -> int | None:
    """VLAN of a distributed port group, or None when it isn't a plain
    single-VLAN group. A full 0-4094 trunk is VGT by another name."""
    spec = getattr(getattr(pg, "config", None), "defaultPortConfig", None)
    vlan = getattr(spec, "vlan", None)
    vid = getattr(vlan, "vlanId", None)
    if isinstance(vid, int):
        return vid
    if isinstance(vid, list):  # trunk range(s)
        return VGT_TRUNK if vid else None
    return None


class VsphereCollector:
    name = NAME

    def configured(self, settings: Settings) -> bool:
        return bool(settings.vsphere_host and settings.vsphere_user and settings.vsphere_pass)

    def collect(self, settings: Settings, conn: sqlite3.Connection) -> str:
        from pyVim.connect import Disconnect, SmartConnect
        from pyVmomi import vim

        verify = settings.vsphere_tls_verify
        if verify is True:
            ctx = ssl.create_default_context()
        elif verify is False:
            ctx = ssl._create_unverified_context()
        else:
            ctx = ssl.create_default_context(cafile=verify)

        # pyVmomi has no timeout parameter and http.client defaults to none —
        # a wedged vCenter would otherwise hang the poll (and launchd won't
        # start a new one while this one lives)
        import socket
        prev_timeout = socket.getdefaulttimeout()
        socket.setdefaulttimeout(30)
        si = SmartConnect(host=settings.vsphere_host, user=settings.vsphere_user,
                          pwd=settings.vsphere_pass, sslContext=ctx)
        n_hosts = n_vms = n_hints = n_tags = 0
        try:
            content = si.RetrieveContent()
            # port group -> VLAN, the map that gives every virtual NIC its tag.
            # Standard-vSwitch groups are named per host, so they're keyed by
            # host; distributed ones are keyed by their global port group key.
            pg_vlan: dict[tuple[str, str], int] = {}
            dvpg_vlan: dict[str, int] = {}
            dv_view = content.viewManager.CreateContainerView(
                content.rootFolder, [vim.dvs.DistributedVirtualPortgroup], True)
            for pg in dv_view.view:
                vid = _dvpg_vlan(pg)
                if vid is not None:
                    dvpg_vlan[pg.key] = vid

            def tag(mac: str | None, vid: int | None, pgname: str | None) -> None:
                """Record a NIC's port group VLAN. VLAN 0 means the vSwitch
                doesn't tag, which is the absence of evidence, not evidence of
                VLAN 0 — the physical switch port decides, so say nothing."""
                nonlocal n_tags
                if not mac or vid is None or vid == NO_VLAN:
                    return
                conn.execute(
                    "INSERT OR REPLACE INTO vnic_vlans (mac, vid, portgroup, source) "
                    "VALUES (?, ?, ?, ?)", (mac.lower(), vid, pgname, NAME))
                n_tags += 1

            host_view = content.viewManager.CreateContainerView(
                content.rootFolder, [vim.HostSystem], True)
            for host in host_view.view:
                status = "up" if host.runtime.connectionState == "connected" else \
                         str(host.runtime.connectionState)
                vnics = (host.config.network.vnic or []) if host.config else []
                for pg in ((host.config.network.portgroup or []) if host.config else []):
                    if pg.spec:
                        pg_vlan[(host.name, pg.spec.name)] = pg.spec.vlanId
                dev_id = db.upsert_device(
                    conn, name=host.name, source=NAME, role="hypervisor",
                    vendor=host.hardware.systemInfo.vendor if host.hardware else None,
                    model=host.hardware.systemInfo.model if host.hardware else None,
                    os=f"esxi {host.config.product.version}" if host.config else "esxi",
                    mgmt_ip=(vnics[0].spec.ip.ipAddress if vnics else None),
                    status=status,
                )
                n_hosts += 1
                live = host.runtime.connectionState == "connected"

                # Interface IDENTITY (names, MACs, addresses) is recorded even
                # for a disconnected host: vCenter keeps its last-known config,
                # MACs don't change, and those MACs are how a switch port full
                # of this host's traffic gets identified. Interface LIVENESS
                # (speed, oper status) is only trustworthy while connected —
                # omitted otherwise, and omitted means "no opinion", so the
                # last good values stand rather than being overwritten by
                # stale ones.
                for pnic in (host.config.network.pnic or []) if host.config else []:
                    db.upsert_interface(
                        conn, device_id=dev_id, name=pnic.device, mac=pnic.mac,
                        speed_bps=(pnic.linkSpeed.speedMb * 1_000_000
                                   if live and pnic.linkSpeed else None),
                        oper_status=(("up" if pnic.linkSpeed else "down")
                                     if live else None),
                    )
                # vmkernel ports (vmk0 management, vMotion, storage...). Their
                # MACs — not the pnic's burned-in ones — are what the switch
                # learns when the host talks, so a port carrying them identifies
                # a hypervisor uplink even while CDP/LLDP is silent (ESXi only
                # reports a neighbor for ~60s after each advertisement, so the
                # hint genuinely lapses between polls).
                for vnic in vnics:
                    db.upsert_interface(
                        conn, device_id=dev_id, name=vnic.device,
                        mac=(vnic.spec.mac if vnic.spec else None),
                        ip=(vnic.spec.ip.ipAddress
                            if vnic.spec and vnic.spec.ip else None),
                        oper_status="up" if live else None,
                    )
                    dvp = getattr(vnic.spec, "distributedVirtualPort", None) \
                        if vnic.spec else None
                    if dvp is not None:
                        tag(vnic.spec.mac, dvpg_vlan.get(dvp.portgroupKey),
                            dvp.portgroupKey)
                    elif vnic.portgroup:
                        tag(vnic.spec.mac if vnic.spec else None,
                            pg_vlan.get((host.name, vnic.portgroup)), vnic.portgroup)
                if not live:
                    continue  # network hints need a live host to answer

                ns = host.configManager.networkSystem
                for hint in ns.QueryNetworkHint():
                    neighbor = None
                    port = None
                    if hint.lldpInfo:
                        params = {p.key: p.value for p in (hint.lldpInfo.parameter or [])}
                        neighbor = params.get("System Name") or hint.lldpInfo.chassisId
                        port = hint.lldpInfo.portId
                    elif hint.connectedSwitchPort:
                        neighbor = hint.connectedSwitchPort.devId
                        port = hint.connectedSwitchPort.portId
                    if neighbor and port:
                        db.upsert_link(conn, a_device=host.name, a_interface=hint.device,
                                       b_device=str(neighbor), b_interface=str(port),
                                       source="vsphere-hint")
                        n_hints += 1

            vm_view = content.viewManager.CreateContainerView(
                content.rootFolder, [vim.VirtualMachine], True)
            vms = []
            network_roles = {"firewall", "switch", "router", "ap", "hypervisor"}
            for vm in vm_view.view:
                if vm.config and vm.config.template:
                    continue
                host_name = vm.runtime.host.name if vm.runtime.host else None
                status = "up" if vm.runtime.powerState == "poweredOn" else "down"
                existing = conn.execute(
                    "SELECT role FROM devices WHERE name = ?", (vm.name,)).fetchone()
                if existing and existing["role"] in network_roles:
                    # a monitored network device that happens to be a VM (a
                    # virtualized firewall): contribute placement + liveness
                    # only — never claim ownership or overwrite its richer
                    # identity from its own collector (vsphere runs last, so
                    # a full upsert here would always win)
                    vm_id = None
                    conn.execute(
                        "UPDATE devices SET parent = ?, status = ?, last_seen = ? "
                        "WHERE name = ?",
                        (host_name, status, db.now(), vm.name))
                else:
                    vm_id = db.upsert_device(
                        conn, name=vm.name, source=NAME, role="vm", parent=host_name,
                        mgmt_ip=(vm.guest.ipAddress if vm.guest else None),
                        os=(vm.config.guestFullName if vm.config else None),
                        status=status,
                    )
                # vNIC MACs matter beyond inventory: a switch port learning
                # them proves the cable leads to this VM's HOST, which is how a
                # hypervisor uplink gets identified when LLDP/CDP is silent (see
                # normalize._place_endpoints_and_infer). The port group VLAN is
                # collected for EVERY guest, including network-role ones — a
                # firewall can't see its own tags, so this is the only source
                # for them. Interface rows are still skipped there; that guest's
                # own collector owns them, under their real names.
                for d in (vm.config.hardware.device if vm.config else []):
                    if not isinstance(d, vim.vm.device.VirtualEthernetCard) \
                            or not d.macAddress:
                        continue
                    port = getattr(d.backing, "port", None)
                    if port is not None:
                        tag(d.macAddress, dvpg_vlan.get(port.portgroupKey),
                            port.portgroupKey)
                    elif getattr(d.backing, "deviceName", None):
                        tag(d.macAddress,
                            pg_vlan.get((host_name, d.backing.deviceName)),
                            d.backing.deviceName)
                    label = (d.deviceInfo.label if d.deviceInfo
                             else f"vnic{d.key}")
                    if vm_id is not None:
                        db.upsert_interface(
                            conn, device_id=vm_id, name=label,
                            mac=d.macAddress,
                            oper_status=("up" if d.connectable
                                         and d.connectable.connected else "down"),
                        )
                    else:
                        # This guest's own collector owns its interfaces under
                        # their real names, so retract any vCenter-labelled row
                        # we created back when that collector wasn't configured
                        # yet. Without this, adding (say) the firewall
                        # integration to a working install leaves "Network
                        # adapter 1..6" alongside vmx0..5 forever — and adding
                        # integrations later is exactly what the README invites.
                        conn.execute(
                            "DELETE FROM interfaces WHERE name = ? AND device_id = "
                            "(SELECT id FROM devices WHERE name = ?)",
                            (label, vm.name))
                vms.append({"name": vm.name, "host": host_name,
                            "power": str(vm.runtime.powerState)})
                n_vms += 1
            db.save_raw(conn, source=NAME, endpoint="vms", payload=vms)
            # retire VMs vSphere no longer has — deleted guests must not
            # haunt the inventory. Only rows this collector owns; role='vm'
            # also catches orphaned rows whose parent went NULL. Never prune
            # on an empty listing: a vCenter hiccup returning zero VMs must
            # not wipe the inventory (an actually-empty lab keeps its truth
            # on the first poll after a VM really existed).
            seen = [v["name"] for v in vms]
            if seen:
                conn.execute(
                    f"DELETE FROM devices WHERE source = ? "
                    f"AND (parent IS NOT NULL OR role = 'vm') "
                    f"AND name NOT IN ({','.join('?' * len(seen))})",
                    (NAME, *seen))
        finally:
            Disconnect(si)
            socket.setdefaulttimeout(prev_timeout)
        return (f"{n_hosts} hosts, {n_vms} vms, {n_hints} uplink hints, "
                f"{n_tags} vnic vlans")


register(VsphereCollector())
