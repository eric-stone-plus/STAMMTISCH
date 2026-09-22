"""STAMMTISCH interface — the rebuilt terminal workstation (M0 skeleton).

Package layout (DESIGN-BLUEPRINT, staged M0..M5):

- ``snapshot.py``     frozen dataclass contract; the ONLY interface between
                       data collectors and the UI layers.
- ``collectors/``     long-lived read-only collectors (demo first; events /
                       files / core-CLI land in M2).
- ``render/``         pure snapshot -> Rich renderables; single color-token
                       truth; letter flags. No Textual import.
- ``status.py``       tier 1: one-shot plain summary (stdlib-only, pipe-safe).

Boundary rule (enforced in review from week one): UI modules import only
this package's ``snapshot`` contract, the provider protocol, and ``render``.
Everything else is a collector or a service.
"""
