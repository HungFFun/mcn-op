"""Shared helpers for the warehouse-backed tasks (MCN report, leave-watch, daily recap).

The payout runner keeps its own self-contained modules under ``payout/``. Everything
else (things that query ``tevi_warehouse`` and post through the *recap* bot) shares the
env loader, ClickHouse runner and Telegram sender defined here.
"""
