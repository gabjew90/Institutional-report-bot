"""Dataclasses for Dropbox file metadata."""

from dataclasses import dataclass


@dataclass
class DropboxFileEntry:
    path: str
    name: str
    rev: str
    size: int
    server_modified: str
