"""semdiff.semantic_diff 的纯函数测试：逐项语义差异分类。"""
from app.common.semdiff import semantic_diff


def test_added_fragment():
    body = {"op": "eq", "args": [{"var": "x"}, 1]}
    d = semantic_diff(None, body)
    assert d["change_count"] == 1
    assert d["changes"][0]["kind"] == "fragment_added"
    assert d["variables"]["added"] == ["x"]
    assert d["operators"]["added"] == ["eq"]


def test_literal_and_list_element_diff_paths():
    base = {"op": "in", "args": [{"var": "country"}, ["CN", "SG", "HK"]]}
    new = {"op": "in", "args": [{"var": "country"}, ["CN", "SG"]]}
    d = semantic_diff(base, new)
    assert (d["changes"][0]["path"] == "$.args[1][2]"
            and d["changes"][0]["kind"] == "removed")
    assert d["change_count"] == 1


def test_operator_variable_reference_change_classification():
    base = {"op": "and", "args": [{"ref": "a"}, {"var": "x"}]}
    new = {"op": "or", "args": [{"ref": "b"}, {"var": "y"}]}
    d = semantic_diff(base, new)
    kinds = {(c["path"], c["kind"]) for c in d["changes"]}
    assert ("$.op", "operator_changed") in kinds
    assert ("$.args[0].ref", "reference_changed") in kinds
    assert ("$.args[1].var", "variable_changed") in kinds
    assert d["refs"] == {"added": ["b"], "removed": ["a"]}
    assert d["variables"] == {"added": ["y"], "removed": ["x"]}
    assert d["operators"] == {"added": ["or"], "removed": ["and"]}


def test_structure_change():
    base = {"op": "not", "args": [{"ref": "a"}]}
    new = {"ref": "a"}
    d = semantic_diff(base, new)
    assert d["changes"][0]["kind"] == "structure_changed"
    assert d["changes"][0]["from_kind"] == "op"
    assert d["changes"][0]["to_kind"] == "ref"


def test_equal_bodies_have_no_diff():
    body = {"op": "eq", "args": [1, 1]}
    assert semantic_diff(body, body)["equal"] is True
    # 键序不同但语义相同
    other = {"args": [1, 1], "op": "eq"}
    assert semantic_diff(body, other)["equal"] is True
