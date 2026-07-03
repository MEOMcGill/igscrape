"""Unit tests for the streaming sink: append_jsonl + hook composition."""

import asyncio
import json

from igscrape.browser_session import _combine_post_hooks
from igscrape.exporter import append_jsonl


def test_append_jsonl_one_object_per_line(tmp_path):
    path = tmp_path / "sub" / "out.jsonl"  # parent dir is created
    append_jsonl([{"pk": "1"}, {"pk": "2"}], path)
    append_jsonl([{"pk": "3"}], path)
    append_jsonl([], path)  # no-op

    lines = path.read_text().splitlines()
    assert [json.loads(l)["pk"] for l in lines] == ["1", "2", "3"]


def test_append_jsonl_is_append_mode(tmp_path):
    path = tmp_path / "out.jsonl"
    append_jsonl([{"pk": "1"}], path)
    append_jsonl([{"pk": "2"}], path)
    assert len(path.read_text().splitlines()) == 2


def test_combine_post_hooks_none_when_empty():
    assert _combine_post_hooks([None, None]) is None


def test_combine_post_hooks_single_passthrough():
    def cb(batch):
        return None

    assert _combine_post_hooks([None, cb]) is cb


def test_combine_post_hooks_runs_all_sync_and_async():
    seen = []

    def sync_sink(batch):
        seen.append(("sync", len(batch)))

    async def async_sink(batch):
        seen.append(("async", len(batch)))

    combined = _combine_post_hooks([sync_sink, async_sink])
    asyncio.run(combined([{"pk": "1"}, {"pk": "2"}]))
    assert seen == [("sync", 2), ("async", 2)]
