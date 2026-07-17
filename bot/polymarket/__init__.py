"""Vestigial global-Polymarket (Gamma/CLOB) venue plumbing — NOT on the live path.

This project began as a same-platform Polymarket + sportsbook arb; that v1's detection and
execution logic has been removed. What remains here (client.py, feed.py) is only the venue-selection
fallback the runner constructs when POLY_VENUE != "us". The live engine is bot/poly_us/ + bot/kalshi/
(POLY_VENUE=us). The global Polymarket CLOB is also geoblocked (403 region-restricted) from a US
server, so this venue could not run there regardless. See ARCHITECTURE.md.
"""
