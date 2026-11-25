"""Qui tam complaint boilerplate, subject parsing, and paragraph renumbering.

Used by ``pdf_brief.py`` to assemble the lawyer-facing PDF in the form of a
federal qui tam complaint (caption block, sequential paragraph numbering,
statutory framework, counts, prayer for relief).
"""

from fraud_investigator.qui_tam import boilerplate, renumber, subject_parser

__all__ = ["boilerplate", "renumber", "subject_parser"]
