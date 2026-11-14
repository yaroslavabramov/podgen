"""Bounded immutable control metadata; only committed overlays enter the owner cache."""

import json
from collections import OrderedDict
from copy import deepcopy


class DescriptorCache:
    def __init__(self, *, max_entries=512, max_bytes=2 * 1024 * 1024, max_entry_bytes=32 * 1024):
        self.max_entries, self.max_bytes = max_entries, max_bytes
        self.max_entry_bytes = max_entry_bytes
        self.entries, self.byte_length = OrderedDict(), 0

    def view(self):
        return DescriptorView(self)

    def get(self, key):
        entry = self.entries.get(key)
        if entry is not None:
            return entry[0]
        return None

    def remember(self, key, value):
        size = len(json.dumps((key, value), default=str, ensure_ascii=True).encode())
        if size > min(self.max_entry_bytes, self.max_bytes):
            return  # A cache miss preserves full persistence; no meaning is cut.
        previous = self.entries.pop(key, None)
        if previous is not None:
            self.byte_length -= previous[1]
        self.entries[key] = (deepcopy(value), size)
        self.byte_length += size
        while len(self.entries) > self.max_entries or self.byte_length > self.max_bytes:
            _, (_, removed) = self.entries.popitem(last=False)
            self.byte_length -= removed


class DescriptorView:
    def __init__(self, owner):
        self.owner = owner
        self.touched = OrderedDict()
        self.pending = DescriptorCache(
            max_entries=owner.max_entries, max_bytes=owner.max_bytes, max_entry_bytes=owner.max_entry_bytes
        )

    def get(self, key):
        value = self.pending.get(key)
        if value is not None:
            return value
        value = self.owner.get(key)
        if value is not None:
            self.touched[key] = None
            self.touched.move_to_end(key)
        return value

    def put(self, key, value):
        self.pending.remember(key, value)

    def commit(self):
        for key in self.touched:
            if key in self.owner.entries:
                self.owner.entries.move_to_end(key)
        for key, (value, _) in self.pending.entries.items():
            self.owner.remember(key, value)
