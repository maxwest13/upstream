"""Polling collectors for free, official sources.

Each source module exposes:
    parse_*(payload, ...) -> list[Row]   pure, unit-tested
    collect(cfg, http) -> list[Row]      network fetch + parse
and is switched on only when its key (if it needs one) is present in .env.
"""
