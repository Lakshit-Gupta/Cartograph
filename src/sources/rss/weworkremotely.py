"""WeWorkRemotely shares 'rss_generic' strategy; module exists so registry._load
can import without ImportError. Logic is identical to remoteok.py.
"""
from __future__ import annotations
# Strategy registration happens in src/sources/rss/remoteok.py — single registration
# per strategy. No-op import.
