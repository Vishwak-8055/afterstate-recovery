"""Artifact storage interface. The local filesystem implementation is the only one shipped in v1;
an S3-compatible backend would implement the same protocol (see docs/adr/0004)."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


class StorageError(Exception):
    pass


class ObjectTooLarge(StorageError):
    pass


class ObjectNotFound(StorageError):
    pass


@dataclass(frozen=True)
class StoredObject:
    key: str
    size_bytes: int
    sha256: str


class ArtifactStorage(Protocol):
    def write_stream(self, key: str, chunks: Iterable[bytes], *, max_bytes: int) -> StoredObject: ...
    def write_bytes(self, key: str, data: bytes) -> StoredObject: ...
    def read_bytes(self, key: str) -> bytes: ...
    def path_for(self, key: str) -> Path:
        """A local filesystem path the recovery tooling can read. Remote backends download to scratch."""
        ...

    def resolve(self, key: str) -> Path:
        """Path for a key without checking existence (used so a missing object is reported, not raised)."""
        ...

    def exists(self, key: str) -> bool: ...
    def size(self, key: str) -> int: ...
    def delete(self, key: str) -> None: ...
