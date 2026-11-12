"""Cross-venue participant matching.

Holds the per-league team alias tables (``matching/data/<league>.csv``) and the resolver that
maps a Kalshi code or label, or a Polymarket US code or label, to one canonical team id.
Tables are data, not code: resolution never guesses, and an ambiguous label raises.
"""
