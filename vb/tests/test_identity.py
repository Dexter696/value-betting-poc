from vb.identity import content_hash_bytes, content_hash_file


def test_content_hash_file_matches_content_hash_bytes(tmp_path):
    payload = b"some raw database bytes" * 1000
    path = tmp_path / "data.bin"
    path.write_bytes(payload)

    assert content_hash_file(path) == content_hash_bytes(payload)


def test_content_hash_file_reads_in_chunks_smaller_than_the_file(tmp_path):
    payload = b"x" * 10_000
    path = tmp_path / "data.bin"
    path.write_bytes(payload)

    assert content_hash_file(path, chunk_size=100) == content_hash_bytes(payload)
