"""Persistent Discord chat ingestion.

Stores every non-bot message from configured channels into the
chat_messages SQLite table so downstream consumers (profile refresh,
/ask claim verification, analytics) can read locally instead of
re-scanning Discord history every time.

Modules:
  watcher — live on_message ingestion + on_ready/on_resumed catch-up
"""
