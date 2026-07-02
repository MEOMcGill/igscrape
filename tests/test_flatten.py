"""Unit tests for the batch flatten engine (exporter.flatten_paths / load_posts)."""

import csv
import gzip
import json

import pytest

from igscrape.exporter import flatten_paths, load_posts, write_jsonl


def _post(pk):
    return {"id": pk, "pk": pk, "code": f"c{pk}", "taken_at": 1700000000, "__typename": "XDTMediaDict"}


# ---- load_posts: format detection --------------------------------------------

def test_load_posts_from_result_json(tmp_path):
    p = tmp_path / "r.json"
    p.write_text(json.dumps({"result": "success", "posts": [_post("1"), _post("2")]}))
    assert [x["pk"] for x in load_posts(p)] == ["1", "2"]


def test_load_posts_from_bare_list(tmp_path):
    p = tmp_path / "r.json"
    p.write_text(json.dumps([_post("1")]))
    assert len(load_posts(p)) == 1


def test_load_posts_from_single_dict(tmp_path):
    p = tmp_path / "r.json"
    p.write_text(json.dumps(_post("9")))
    assert load_posts(p)[0]["pk"] == "9"


def test_load_posts_from_jsonl(tmp_path):
    p = tmp_path / "r.jsonl"
    p.write_text("\n".join(json.dumps(_post(str(i))) for i in range(3)) + "\n")
    assert [x["pk"] for x in load_posts(p)] == ["0", "1", "2"]


def test_load_posts_from_gz(tmp_path):
    p = tmp_path / "r.jsonl.gz"
    with gzip.open(p, "wt", encoding="utf-8") as f:
        f.write(json.dumps(_post("1")) + "\n")
    assert load_posts(p)[0]["pk"] == "1"


# ---- flatten_paths: single file ----------------------------------------------

def test_flatten_single_file_to_named_csv(tmp_path):
    src = tmp_path / "in.json"
    src.write_text(json.dumps({"posts": [_post("1"), _post("2")]}))
    out = tmp_path / "out.csv"
    n = flatten_paths(str(src), output=str(out), fmt="csv")
    assert n == 2
    with open(out) as f:
        rows = list(csv.DictReader(f))
    assert [r["pk"] for r in rows] == ["1", "2"]


def test_flatten_single_file_jsonl_input_to_jsonl(tmp_path):
    src = tmp_path / "in.jsonl"
    src.write_text("\n".join(json.dumps(_post(str(i))) for i in range(2)) + "\n")
    out = tmp_path / "flat.jsonl"
    flatten_paths(str(src), output=str(out), fmt="jsonl")
    lines = out.read_text().splitlines()
    assert len(lines) == 2
    assert json.loads(lines[0])["code"] == "c0"  # flattened schema, not raw


def test_flatten_format_all_writes_three(tmp_path):
    pytest.importorskip("polars")
    src = tmp_path / "in.json"
    src.write_text(json.dumps({"posts": [_post("1")]}))
    out = tmp_path / "flat.csv"
    flatten_paths(str(src), output=str(out), fmt="all")
    assert (tmp_path / "flat.csv").exists()
    assert (tmp_path / "flat.jsonl").exists()
    assert (tmp_path / "flat.parquet").exists()


# ---- flatten_paths: directory + concat ---------------------------------------

def test_flatten_directory_per_file(tmp_path):
    d = tmp_path / "scrapes"
    d.mkdir()
    (d / "a_Search.json").write_text(json.dumps({"posts": [_post("1")]}))
    (d / "b_Search.jsonl").write_text(json.dumps(_post("2")) + "\n")
    outdir = tmp_path / "flat"
    flatten_paths(str(d), output=str(outdir), fmt="jsonl")
    assert (outdir / "a_Search.jsonl").exists()
    assert (outdir / "b_Search.jsonl").exists()


def test_flatten_concat_merges(tmp_path):
    d = tmp_path / "scrapes"
    d.mkdir()
    (d / "a.json").write_text(json.dumps({"posts": [_post("1")]}))
    (d / "b.json").write_text(json.dumps({"posts": [_post("2"), _post("3")]}))
    out = tmp_path / "merged.jsonl"
    n = flatten_paths(str(d), output=str(out), fmt="jsonl", concat=True)
    assert n == 3
    assert len(out.read_text().splitlines()) == 3


def test_flatten_concat_requires_file_output(tmp_path):
    d = tmp_path / "scrapes"
    d.mkdir()
    (d / "a.json").write_text(json.dumps({"posts": [_post("1")]}))
    with pytest.raises(ValueError):
        flatten_paths(str(d), output=str(tmp_path / "afolder"), fmt="csv", concat=True)


def test_flatten_multiple_files_reject_file_output(tmp_path):
    d = tmp_path / "scrapes"
    d.mkdir()
    (d / "a.json").write_text(json.dumps({"posts": [_post("1")]}))
    (d / "b.json").write_text(json.dumps({"posts": [_post("2")]}))
    with pytest.raises(ValueError):
        flatten_paths(str(d), output=str(tmp_path / "out.csv"), fmt="csv")


def test_write_jsonl_roundtrip(tmp_path):
    out = tmp_path / "x.jsonl"
    write_jsonl([{"a": 1}, {"a": 2}], out)
    assert [json.loads(l)["a"] for l in out.read_text().splitlines()] == [1, 2]
