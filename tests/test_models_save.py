"""Unit tests for ScrapingResult.save / save_all (JSON + JSONL)."""

import json

from igscrape.models import Query, ScrapingResult


def _result():
    q = Query(endpoint="Search", query={"keyword": "x", "max_posts": -1}, params={})
    return ScrapingResult(query=q, result="success", posts=[{"pk": "1"}, {"pk": "2"}])


def test_save_json_default(tmp_path):
    p = tmp_path / "out.json"
    _result().save(str(p))
    obj = json.loads(p.read_text())
    assert obj["result"] == "success"
    assert len(obj["posts"]) == 2


def test_save_infers_jsonl_from_extension(tmp_path):
    p = tmp_path / "out.jsonl"
    _result().save(str(p))
    lines = p.read_text().splitlines()
    assert len(lines) == 2
    assert [json.loads(l)["pk"] for l in lines] == ["1", "2"]


def test_save_jsonl_flag_overrides_extension(tmp_path):
    p = tmp_path / "out.txt"
    _result().save(str(p), jsonl=True)
    assert len(p.read_text().splitlines()) == 2


def test_save_all_writes_both(tmp_path):
    json_path, jsonl_path = _result().save_all(str(tmp_path / "montreal_Search"))
    assert json_path.endswith(".json") and jsonl_path.endswith(".jsonl")
    assert "posts" in json.loads(open(json_path).read())
    assert len(open(jsonl_path).read().splitlines()) == 2
