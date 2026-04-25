"""Conductor domain — the :class:`ConPilot` facade and its primitives.

Re-exports :class:`ConPilot` so existing call sites
(``from con_pilot.conductor import ConPilot``) keep working after the
``conductor.py`` module was promoted to a package.
"""

from con_pilot.conductor.facade import ConPilot

__all__ = ["ConPilot"]
