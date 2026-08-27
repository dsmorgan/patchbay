# Manual test guide — pages and map milestone (2026-08-24/25)

For the owner's playtest of branch `ui/pages-and-map` (on top of 0.3.0, which merged PR #1). Automated
gates (pytest, `scripts/screenshots.py`) have passed on every brief; this guide is for what they cannot
see — whether it *feels* ordered. Decisions are in `docs/adr/0001-page-shell-and-map-modes.md`.

## Run it
```bash
cd D:/git/patchbay && git checkout ui/pages-and-map
PATCHBAY_ENV=/nonexistent/.env PATCHBAY_DB=demo.db PYTHONPATH=src .venv/Scripts/python.exe -m uvicorn patchbay.web:app --port 8091
```
Open http://127.0.0.1:8091/. (`patchbay demo` first if `demo.db` is missing.) Your own site DB works
too — every page degrades to a reduced page when a source is absent, never an error.

## 1. Page headers (brief p1)
- [ ] Every page opens with a title and a one-line purpose under the top bar; the purpose is the same
      sentence the rail shows on hover. Does the sentence answer "why would I open this?"
- [ ] Page controls sit at the right of that header: reset layout (Topology), panel select (Patch panels),
      show on map / show all ports (a device page), view running config (a config node).
- [ ] Drill-downs show a breadcrumb above the title (Overview / core1; Configs / sw1) and the rail keeps
      the parent lit.
- [ ] Section headings read as answers ("Cabled to", "Guests on this host", "Subnets no VLAN claims").
      Any that still read as a filing label?
- [ ] Collapse the rail (chevron or foot entry). Reload. Still collapsed. Narrow the window past ~640px:
      labels go, icons stay, headings become rules.

## 2. Overview (brief overview-exceptions)
- [ ] The first thing on the page is the exceptions strip. On the demo it shows the two IPAM conflicts;
      is that the right first thing to see? Click it — it lands on Drift.
- [ ] Stop a device (on your site DB) or imagine one down: does a crit card with the device name and
      a link to its page feel like the right alarm level? "Unknown" status never raises one — agree?
- [ ] With nothing wrong the strip is one "All clear — …" line naming what was checked. Reassuring or noise?
- [ ] Each hypervisor card folds its guests: open one. Is the VM table missed on the front page?
- [ ] The Links table is gone. Did you use it? (The map and each device page keep the same rows.)

## 3. Topology (briefs map-url-modes, map-tiers-fit)
- [ ] The toolbar leads with **Wiring · Load · VLAN · Evidence**. Click through them: does each mode's
      legend make sense on its own? Is "Evidence" the right word (alternative: "Sources")?
- [ ] Wiring is now the default: grey cables, dashed where inferred. Do you miss the reporter colours
      as the default, or is it calmer?
- [ ] Pick VLAN 20 in VLAN mode, then switch to Load: a `vlan 20 ×` chip stays in the toolbar and the dim
      still applies. Clear it with ×.
- [ ] Copy the URL after setting a mode/vlan/toggle and open it in a new tab — identical view? Reload —
      identical? Back button leaves the page rather than unwinding toggles — acceptable?
- [ ] Four labelled bands: Internet / Edge / Fabric / Access & compute. Names right? Too loud, too quiet?
- [ ] Drag a node — it slides along its lane only. Reload: X kept, Y in-lane. `reset layout` clears pins.
      Your previously pinned nodes will have jumped vertically into their lane once — acceptable?
- [ ] The whole graph fits the frame on load, after every toggle, and after resizing the window; the
      `fit` button restores it. Nothing clipped at the top (the internet cloud used to be).
- [ ] From a device page click "show on map": the node is centred and haloed.

## 4. Snapshots (brief snapshots-page)
- [ ] Records now has three entries: Drift, Configs, Snapshots. Right group?
- [ ] `/snapshots`: the demo line, the empty state, then "Where they go" with a caution when no delivery
      dir is set. Take one with "snapshot now"; it appears newest-first with a download link.
- [ ] `/ops` is now Collect now → Declarations (with the conflict/skipped panels) → What patchbay is
      running on. Headings: keep these, or revert to the nouns (Declarations / Effective configuration)?

## 5. Configs (brief configs-timeline)
- [ ] With Oxidized configured (site DB): the page opens with "What changed" across all devices, newest
      first, each row's diff link opening the two-version diff. Is 50 the right depth?
- [ ] Without Oxidized (demo): the same unconfigured prose as before — no error.

## 6. Deep links and the device page (brief deep-links-and-device-endpoints)
- [ ] VLANs: click a vlan id → the map in VLAN mode with that VLAN lit.
- [ ] A device page: the ports table has an Endpoints column; click a count to expand the endpoints on
      that port under it (the graph toggle still works beside it). Is the port the right place for them?
- [ ] Patch panels: `/patchpanel#p3` highlights row 3.

## What to tell me
Taste calls I made rather than blocked on, in the order I'd like your read:
1. Group names *Network* / *Records*, and *Ops* living in the rail's foot.
2. The purpose sentences in `web.py` `NAV` — rewrite any that don't sound like you.
3. Device pages lighting *Overview* in the rail.
4. Tier names, mode names, and the `/ops` section headings (§3, §4 above).
5. The Snapshots rail glyph (an archive box) against the rest of the set.
