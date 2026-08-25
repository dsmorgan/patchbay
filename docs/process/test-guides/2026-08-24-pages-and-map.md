# Manual test guide — pages and map milestone (2026-08-24)

For the owner's playtest of branch `ui/pages-and-map` (stacked on `ui/nav-rail`, PR #1). Automated
gates (pytest, `scripts/screenshots.py`) have passed on every brief; this guide is for what they cannot
see — whether it *feels* ordered.

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
      show all / hide idle ports (a device page), view running config (a config node).
- [ ] Drill-downs show a breadcrumb above the title (Overview / core1; Configs / sw1) and the rail keeps
      the parent lit.
- [ ] Section headings read as answers ("Cabled to", "Guests on this host", "Subnets no VLAN claims").
      Any that still read as a filing label?
- [ ] Collapse the rail (chevron or foot entry). Reload. Still collapsed. Widen/narrow the window
      past ~640px: labels go, icons stay, headings become rules.

## 2–7. (filled in as the remaining briefs land)
- Overview: exceptions strip, folded VMs, no links table.
- Topology: modes (Wiring / Load / VLAN / Evidence) in the URL, drawn tiers, fit-to-view.
- Snapshots page under Records; Ops reordered.
- Configs timeline.
- Deep links between pages and the map.
- Device page: endpoints folded into the ports table.

## What to tell me
Taste calls I made rather than blocked on, in the order I'd like your read:
1. Group names *Network* / *Records*, and *Ops* living in the rail's foot.
2. The purpose sentences in `web.py` `NAV` — rewrite any that don't sound like you.
3. Device pages lighting *Overview* in the rail.
