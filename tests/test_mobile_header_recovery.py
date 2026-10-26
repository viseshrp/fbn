"""Counter contract for positively classified header assembly, not acceptance."""

import pytest

from fbn.mobile import _is_header_assembly


def evidence():
    return {
        "feedRootCount": 1,
        "itemCount": 2,
        "postCount": 1,
        "headerAssemblyCount": 1,
        "unclassifiedMenuCards": 0,
        "missing": ["unique_primary_menu"],
    }


def test_header_assembly_requires_all_candidates_accounted_for():
    assert _is_header_assembly(evidence())
    value = evidence()
    value.update(itemCount=3, postCount=0, headerAssemblyCount=3)
    assert _is_header_assembly(value)


@pytest.mark.parametrize(
    "field",
    [
        "feedRootCount",
        "itemCount",
        "postCount",
        "headerAssemblyCount",
        "unclassifiedMenuCards",
    ],
)
@pytest.mark.parametrize("value", [None, True, -1, "SYNTHETIC_SECRET", {}, []])
def test_header_assembly_rejects_missing_or_non_integer_counts(field, value):
    data = evidence()
    data[field] = value
    assert not _is_header_assembly(data)


@pytest.mark.parametrize(
    "mutation",
    [
        {"feedRootCount": 2},
        {"headerAssemblyCount": 0},
        {"unclassifiedMenuCards": 1},
        {"itemCount": 3},
        {"missing": []},
        {"missing": ["unique_primary_menu", "unknown_layout"]},
        {"missing": ["unique_primary_timestamp"]},
    ],
)
def test_header_assembly_rejects_other_missing_evidence(mutation):
    data = evidence()
    data.update(mutation)
    assert not _is_header_assembly(data)
