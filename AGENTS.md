# CEB — agent instructions

CEB (Combined Environment Bundles; named PAM during development until 2026-11-05) is a small
GNOME app (Python, GTK 4, libadwaita) for display profiles: screen on/off and positions, plus the
audio output and input device for each profile. It remembers every screen it has seen, also
unplugged ones. Public, MIT, published on GitHub. First release: GNOME only.

- Work is tracked in Backlog (`backlog/`); the plan is TASK-1 and its subtasks.
- Screens are identified by vendor/product/serial, and identical twins by port order; never by
  port name alone (port names change between boots on multi-GPU machines).
- Displays are applied without GNOME's "Keep changes" prompt (temporary apply).
