import pytest

from casd import reapi
from conftest import action_result, digest, message, tree, varint

A, B, C, D = ("a" * 64, 10), ("b" * 64, 20), ("c" * 64, 30), ("d" * 64, 40)


def test_action_result_lists_files_stdout_stderr_and_trees():
    blobs, trees = reapi.action_result_refs(action_result(files=[A, B], trees=[C], stdout=D, stderr=A))
    assert blobs == [A, B, D, A]
    assert trees == [C]


def test_action_result_with_no_outputs():
    assert reapi.action_result_refs(action_result()) == ([], [])
    assert reapi.action_result_refs(b"") == ([], [])


def test_tree_lists_files_in_root_and_children():
    assert reapi.tree_file_digests(tree(root_files=[A, B], child_files=[C])) == [A, B, C]


def test_multibyte_varints_round_trip():
    big = ("e" * 64, 5_000_000_000)
    blobs, _ = reapi.action_result_refs(action_result(files=[big]))
    assert blobs == [big]


@pytest.mark.parametrize(
    "garbage",
    [
        b"\x12",  # length-delimited field with no length
        b"\x12\x05ab",  # length runs past the end
        b"\xff" * 12,  # varint that never terminates
        b"\x00\x01",  # field number 0
        message(2, message(2, message(1, b"not-a-sha256"))),  # bad hash
        message(6, message(1, b"\xff" * 64)),  # hash is not ascii
        varint(2 << 3 | 3),  # group wire type, unsupported
    ],
)
def test_malformed_input_raises_value_error(garbage):
    with pytest.raises(ValueError):
        reapi.action_result_refs(garbage)


def test_digest_helper_matches_parser():
    blobs, _ = reapi.action_result_refs(message(6, digest(*A)))
    assert blobs == [A]
