"""项目级 Auto Memory。"""

from .extract import EXTRACT_SYSTEM_PROMPT, persist_auto_memory
from .settings import auto_memory_enabled
from .store import (
    list_memory_files,
    load_memory_index_layer,
    memory_dir,
    read_memory_file,
    resolve_memory_file,
    write_entry,
)
from .types import INDEX_FILENAME, KIND_FILENAMES, MemoryEntry, MemoryKind

__all__ = [
    "EXTRACT_SYSTEM_PROMPT",
    "INDEX_FILENAME",
    "KIND_FILENAMES",
    "MemoryEntry",
    "MemoryKind",
    "auto_memory_enabled",
    "list_memory_files",
    "load_memory_index_layer",
    "memory_dir",
    "persist_auto_memory",
    "read_memory_file",
    "resolve_memory_file",
    "write_entry",
]
