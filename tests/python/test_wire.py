import gc
import zlib
from concurrent.futures import ThreadPoolExecutor

import pytest
from conftest import varuint

import bedrock_protocol as bp
from bedrock_protocol.wire import BatchCodec, Compression


def frame(*packets):
    return b"".join(varuint(len(packet)) + packet for packet in packets)


def test_no_transport_prefix_and_independent_deflate(golden):
    codec = bp.Codec(2193)
    data = golden(2, "test_002_play_status.cpp")
    packet = codec.decode_packet(data).payload
    expected = frame(data)
    batch = BatchCodec(codec)
    assert batch.encode([packet]) == expected
    batch.set_compression(Compression.ZLIB, 0)
    compressor = zlib.compressobj(wbits=-15)
    compressed = b"\x00" + compressor.compress(expected) + compressor.flush()
    assert batch.encode([packet]) == compressed
    decoded = batch.decode(compressed)
    assert len(decoded) == decoded.packet_count == decoded.decoded_count == 1
    assert not decoded.filtered
    assert decoded.skipped_packet_ids == ()
    assert decoded[0].payload.status == bp.v2193.PlayStatus.PLAYER_SPAWN


@pytest.mark.parametrize(
    "algorithm,wire",
    [
        (Compression.ZLIB, "00636664620600"),
        (Compression.SNAPPY, "01040c03010203"),
        (Compression.NONE, "ff03010203"),
    ],
)
def test_existing_independent_batch_vectors(algorithm, wire):
    # bedrock-bot/tests/test_batch.cpp; only its transport marker is removed.
    batch = BatchCodec(bp.Codec(2193))
    batch.set_compression(algorithm, 0)
    result = batch.decode(bytes.fromhex(wire), packet_ids=[])
    assert result.filtered
    assert result.packet_count == 1 and result.decoded_count == 0
    assert result.headers[0].packet_id == 1
    assert result.skipped_packet_ids == (1,)


@pytest.mark.parametrize("algorithm", [Compression.ZLIB, Compression.SNAPPY, Compression.NONE])
def test_threshold_and_uncompressed_escape(algorithm, golden):
    codec = bp.Codec(2193)
    packet = codec.decode_packet(golden(2, "test_002_play_status.cpp")).payload
    plaintext = frame(codec.encode_packet(packet))
    for threshold in (len(plaintext) + 1, len(plaintext), 0):
        batch = BatchCodec(codec)
        batch.set_compression(algorithm, threshold)
        wire = batch.encode([packet])
        assert (wire[0] == 255) == (algorithm == Compression.NONE or threshold > len(plaintext))
        assert len(batch.decode(wire)) == 1
        assert len(batch.decode(b"\xff" + plaintext)) == 1


def test_batch_failure_is_terminal_and_never_publishes_prefix(golden):
    codec = bp.Codec(2193)
    good = golden(2, "test_002_play_status.cpp")
    packet = codec.decode_packet(good).payload
    for invalid in [
        frame(good, varuint(2) + b"\x00"),
        frame(good) + b"\x80",
        frame(good, varuint(1 << 14)),
        bytes.fromhex("ffffffff10"),
        b"",
        frame(varuint(9)),
    ]:
        batch = BatchCodec(codec)
        with pytest.raises(bp.DecodeError):
            batch.decode(invalid)
        with pytest.raises(RuntimeError, match="terminal"):
            batch.decode(frame(good))
        with pytest.raises(RuntimeError, match="terminal"):
            batch.encode([packet])
        with pytest.raises(RuntimeError, match="terminal"):
            batch.set_compression(Compression.NONE, 0)
        with pytest.raises(RuntimeError, match="terminal"):
            batch.enable_encryption(bytes(32))


def test_filter_is_explicit_and_validates_every_header(golden):
    good = golden(2, "test_002_play_status.cpp")
    unknown = varuint(1023) + b"opaque"
    data = frame(varuint(9), good, unknown)
    batch = BatchCodec(bp.Codec(2193))
    result = batch.decode(data, packet_ids={2, 1023})
    assert result.filtered and result.skipped_packet_ids == (9,)
    assert [header.packet_id for header in result.headers] == [9, 2, 1023]
    assert [packet.header.packet_id for packet in result] == [2, 1023]
    with pytest.raises(bp.DecodeError):
        batch.decode(frame(good, varuint(1 << 14)), packet_ids={2})


def test_aggregate_decoding_budget(golden):
    data = golden(136, "test_136_client_cache_miss_response.cpp")
    limits = bp.DecodeLimits(max_decoded_elements=3)
    assert len(bp.Codec(2193, limits=limits).decode_packet(data).payload.blobs) == 2
    batch = BatchCodec(bp.Codec(2193, limits=limits))
    with pytest.raises(bp.DecodeError):
        batch.decode(frame(data, data))
    skipped = BatchCodec(bp.Codec(2193, limits=bp.DecodeLimits(max_decoded_elements=0)))
    assert skipped.decode(frame(data, data), packet_ids=set()).skipped_packet_ids == (136, 136)


def test_batch_iterator_and_payload_own_storage(golden):
    batch = BatchCodec(bp.Codec(2193))
    result = batch.decode(frame(golden(136, "test_136_client_cache_miss_response.cpp")))
    iterator = iter(result)
    del result, batch
    gc.collect()
    packet = next(iterator)
    assert packet.payload.blobs[0].blob_data.to_bytes() == b"chunk"
    with pytest.raises(StopIteration):
        next(iterator)


def test_owner_thread_and_reentrant_access(golden):
    codec = bp.Codec(2193)
    batch = BatchCodec(codec)
    data = golden(2, "test_002_play_status.cpp")
    with ThreadPoolExecutor(max_workers=1) as pool:
        for action in [
            lambda: batch.decode(frame(data)),
            lambda: batch.set_compression(Compression.NONE, 0),
            lambda: batch.enable_encryption(bytes(32)),
            lambda: batch.encode([]),
        ]:
            with pytest.raises(RuntimeError, match="owner thread"):
                pool.submit(action).result()

    class Packets:
        def __iter__(self):
            batch.set_compression(Compression.NONE, 0)
            yield codec.decode_packet(data).payload

    with pytest.raises(RuntimeError, match="reentrant"):
        batch.encode(Packets())
    assert len(batch.decode(frame(data))) == 1


def test_encrypted_order_checksum_and_argument_failure(golden):
    codec = bp.Codec(2193)
    sender, receiver = BatchCodec(codec), BatchCodec(codec)
    sender.enable_encryption(bytes(range(32)))
    receiver.enable_encryption(bytes(range(32)))
    packet = codec.decode_packet(golden(2, "test_002_play_status.cpp")).payload
    with pytest.raises(ValueError):
        sender.encode([])
    first, second = sender.encode([packet]), sender.encode([packet])
    assert first != second
    assert receiver.decode(first)[0].payload.status == packet.status
    corrupted = second[:-1] + bytes([second[-1] ^ 1])
    with pytest.raises(bp.DecodeError):
        receiver.decode(corrupted)
    with pytest.raises(RuntimeError, match="terminal"):
        receiver.decode(second)
