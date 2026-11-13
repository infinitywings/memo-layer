"""The v2 kernel (docs/DESIGN_V2.md): labels computed from the log alone.

The projection in `rka_memory.world` supplies structure (sessions, records,
links, deliveries). The kernel never reads the rung state that v1's
RUNG_CHANGE events fold into it.
"""
