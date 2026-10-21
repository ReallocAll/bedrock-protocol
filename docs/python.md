# Python protocol runtime

The runtime exposes every active packet and its dependent schema types for the 19
versions in `SUPPORTED_VERSIONS`: 729, 748, 766, 776, 786, 800, 818, 819, 827, 844,
859, 898, 924, 944, 975, 1001, 2168, 2193 and 2225. This covers 245 distinct packet
IDs and 4,079 version/packet pairs. Retired packets and declaration gaps remain
absent in the corresponding version modules.

```python
from bedrock_protocol import Codec, DecodeLimits, Variant, v2193
from bedrock_protocol.wire import BatchCodec, Compression

codec = Codec(2193, limits=DecodeLimits())
request = v2193.RequestNetworkSettingsPacket(client_network_version=2193)
wire = codec.encode_packet(request)
decoded = codec.decode_packet(wire)
assert decoded.header.packet_id == 193
assert decoded.payload.client_network_version == 2193

batch = BatchCodec(codec)
message = batch.encode([request])
assert batch.decode(message)[0].payload.client_network_version == 2193
```

All nonliteral fields are required keyword arguments, including explicit `None`
for absent optional or conditional fields. A false condition exposes `None` and
rejects a supplied value. Literal fields are readonly constants. Union fields use
`Variant(index, value)`; optional fields use a value or `None`. Enum fields require
the matching generated `IntEnum`. Unknown numeric values retain their enum type
within its underlying range; a name-coded value must have a wire name.

Integer construction rejects values outside a narrower field encoding with
`TypeError`. Same-width signed/unsigned wire mappings retain their bit patterns.
Sequences exceeding their length prefix's nonnegative range raise `ValueError`;
explicit count fields still require a matching sequence length.
Mapping inputs, including NBT compounds, require `items()` to yield actual
2-tuples; other entry types or lengths raise `TypeError`.

Generated version modules alias shared native shapes. A constructed value has
`protocol is None`; access through `v2193` alone does not give a shared shape a
version origin. A decoded envelope and its subviews retain their decoding protocol.
`Codec.encode_packet` checks whether the actual packet shape exists in its version.

Values, sequences, maps and binary fields own immutable native storage. Retained
children, iterators and readonly buffers survive destruction of their parent and
codec. Construction snapshots mutable buffers and containers. Use `copy()` to
detach just a selected subtree; `to_dict()`, `to_list()` and `to_bytes()` explicitly
materialize values. `wire_bytes()` re-encodes the selected value's body. It does not
promise the original byte spelling of noncanonical input.

`DynamicValue` aliases `Variant`. Each node requires an explicit tag: 0 for `None`,
1 for `bool`, 2 for a signed 64-bit `int` excluding `bool`, 3 for `float`, 4 for
`str`, 5 for an array, and 6 for a string-keyed object. Arrays accept lists, tuples
or matching native sequence views; objects accept dictionaries or matching native
map views. Every child is another `Variant`. Construction rejects cycles and
nesting beyond 128 DynamicValue nodes; repeated acyclic references are copied.
Returned arrays and objects are immutable native views. `to_dict()` materializes
nested values, while a view's `copy()` detaches its subtree.

`PhotoTransferPacket.photo_data`, `ResourcePackDataInfoPacket.file_hash` and
`CompressedBiomeDefinitionListPacket.compressed_biome_data` accept readable buffers
and return `BinaryView`. Compressed biome bytes remain opaque; packet 301 is absent
from protocol 800 onward. Sub-chunk entry `serialized_sub_chunk` fields also use
binary views, preserving their conditional absence before 2168 and optional
presence from 2168 onward.

Binary views export readonly format `B`. NBT byte arrays export signed format `b`.
NBT preserves tag kinds and the element kind of an empty list:

```python
from bedrock_protocol import Nbt, NbtType

empty_ints = Nbt(NbtType.LIST, [], element_type=NbtType.INT)
signed = Nbt(NbtType.BYTE_ARRAY, [-1, 0, 127])
assert memoryview(signed).format == "b"
```

`Codec.supported_packet_ids` and `known_packet_ids` are matching readonly tuples.
A schema-unknown ID produces an owned
`UnknownPacket`, whose body can be inspected or forwarded. Known parse errors and
trailing body bytes fail decoding. `ProtocolError` exceptions expose protocol,
packet ID, reason and an offset when available.

Native text bytes convert strictly to UTF-8 when accessed as Python strings.
Malformed text can decode successfully and later raise `UnicodeDecodeError` in a
getter or `to_dict()`, including NBT string values. Invalid NBT compound names
raise `RuntimeError` during name iteration or materialization. Native re-encoding
preserves those bytes. A later text conversion failure does not make `BatchCodec`
terminal. Unknown packet bodies and binary fields remain opaque bytes.

Default limits are 4 MiB per packet, 16 MiB per decompressed batch, 4096 packets,
128 levels of nesting, 1,048,576 decoded container elements and 64 MiB of accounted
decoded storage. `DecodeLimits` configures the last three; depth cannot exceed 128.
Storage accounting covers string/binary bytes and native container elements, not
process RSS or allocator overhead. A packet shares one budget across nested readers;
a batch shares one budget across all selected packet bodies.
The nesting budget includes enclosing schema scopes, so an NBT subtree's depth
alone does not determine whether its packet can decode. `DecodeLimits` applies to
decoding; successful NBT construction, copying, materialization or encoding does
not establish that the value fits a decode budget.

`Codec` is immutable and supports independent calls from different threads.
`BatchCodec` belongs to its creating thread and one connection. It rejects reentrant
and cross-thread calls. After any wire decode failure it is terminal, including
configuration and encode calls. Argument validation before cipher mutation can fail
without consuming the cipher.

Wait for caller backpressure before `BatchCodec.encode()`: a successful encode
advances encryption state, and every returned message must be sent in order.
Configure compression or encryption at the negotiated boundary after processing the
current batch and before decoding the next. The wire component handles raw DEFLATE,
Snappy, none, checksum and directional counters. It adds no RakNet `0xFE` prefix.

`decode(data, packet_ids=...)` explicitly filters bodies. Every frame length and
header is validated first. `PacketBatch.headers`, `packet_count`, `decoded_count`,
`filtered` and `skipped_packet_ids` describe what was selected and skipped. Unselected
bodies may remain undecoded; a selected-body failure rejects the entire batch.

## Building and compiler use

Wheel builds use `conan-py-build==0.6.4`, `nanobind==3.1.0`, OpenSSL 3.5.2,
zlib 1.3.1 and Snappy 1.2.1. The runtime has no compiler-extra, bot or transport
dependency. Install the `compiler` extra to use the installed `bpc` command.

Unlike the pure Python compiler distribution in 0.4.3, `0.5.0.dev0` packages the
compiler and native runtime together. Installing the package, including
`bedrock-protocol[compiler]`, requires a compatible native wheel or the C++ build
toolchain and native dependencies to build one. The `compiler` extra adds only
`click` and `griffe`; it does not select a pure Python installation.

To use a locally built wheel, install it with the extra and invoke its launcher:

```shell
python -m pip install "path/to/bedrock_protocol-0.5.0.dev0-<python>-<abi>-<platform>.whl[compiler]"
bpc --language cpp --out generated --import-path . protocol/network.py
```

Build on Windows in a Visual Studio developer prompt with clang-cl, CMake and Ninja
on PATH. Use an explicit private Conan home and a profile for the build toolchain:

```powershell
$env:CONAN_HOME = Join-Path $PWD ".local/conan"
python -m pip install build conan-py-build==0.6.4 nanobind==3.1.0 click griffe
python -m build --wheel --no-isolation -C build-dir=.local/build `
  -C host-profile=cmake/conan/windows-clang.profile `
  -C build-profile=cmake/conan/windows-clang.profile
```

The compiler itself can run before `_native` exists:

```powershell
$env:PYTHONPATH = Join-Path $PWD "src"
python -m bedrock_protocol --language cpp --out .local/generated --import-path . protocol/network.py
```

The native CMake path uses the current build interpreter and source `PYTHONPATH`;
it never installs the project to start its compiler. Python modules and stubs are
generated by `--language nanobind` from the same descriptors into the build tree.
Wheel builds include all 19 version modules and stubs; source distributions include
the schemas and compiler needed to regenerate them.

For standalone C++, `BEDROCK_PROTOCOL_BUILD_PYTHON=OFF` and
`BEDROCK_PROTOCOL_BUILD_WIRE=OFF` keep Python, nanobind and wire dependencies out of
pregenerated release consumption. Enabling the wire option adds
`bedrock::protocol_wire`. Existing C++ selectors and package version remain 0.4.3;
the Python candidate is `0.5.0.dev0`.

The current release workflow checks tags against the CMake version and publishes
C++ install-tree and pregenerated-source ZIPs. CI builds and tests Python wheels,
but the release workflow does not publish them to PyPI or attach them to releases.
