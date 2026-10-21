#include <set>
#include <thread>

#include <bedrock/protocol/wire/batch.h>

#include "codec.h"

namespace bedrock::protocol::python {
namespace {
struct PacketBatch {
    std::shared_ptr<const std::vector<DecodedPacket>> packets;
    std::vector<PacketHeader> headers;
    std::vector<int> skipped;
    bool filtered;
    int protocol;
};

struct BatchIterator {
    std::shared_ptr<const std::vector<DecodedPacket>> packets;
    std::size_t offset = 0;
};

class Batch {
public:
    explicit Batch(const Codec &codec) : codec_(codec) {}

    struct Access {
        Batch &batch;
        explicit Access(Batch &batch) : batch(batch)
        {
            if (batch.owner_ != std::this_thread::get_id()) {
                throw std::runtime_error("BatchCodec accessed outside its owner thread");
            }
            if (batch.busy_) {
                throw std::runtime_error("BatchCodec access is reentrant");
            }
            if (batch.terminal_) {
                throw std::runtime_error("BatchCodec is terminal");
            }
            batch.busy_ = true;
        }
        ~Access() { batch.busy_ = false; }
    };

    void compression(wire::Compression algorithm, std::uint16_t threshold)
    {
        Access access(*this);
        auto result = wire_.setCompression(algorithm, threshold);
        if (!result) {
            throw nb::value_error(result.error().c_str());
        }
    }

    void encryption(nb::handle key)
    {
        Access access(*this);
        auto copy = snapshotBuffer(key, 32);
        if (copy.size() != 32) {
            throw nb::value_error("encryption key must contain 32 bytes");
        }
        std::array<unsigned char, 32> native{};
        std::memcpy(native.data(), copy.data(), native.size());
        auto result = wire_.enableEncryption(native);
        if (!result) {
            throw nb::value_error(result.error().c_str());
        }
    }

    nb::bytes encode(nb::handle packets)
    {
        Access access(*this);
        std::vector<std::pair<int, NativePacket>> values;
        auto iterator = nb::steal<nb::object>(PyObject_GetIter(packets.ptr()));
        if (!iterator.is_valid()) {
            throw nb::python_error();
        }
        while (auto *next = PyIter_Next(iterator.ptr())) {
            auto packet = nb::steal<nb::object>(next);
            if (values.size() == 4096) {
                throw nb::value_error("batch packet count exceeds limit");
            }
            values.push_back(codec_.snapshot(packet));
        }
        if (PyErr_Occurred()) {
            throw nb::python_error();
        }
        if (values.empty()) {
            throw nb::value_error("batch must contain at least one packet");
        }
        std::expected<std::string, std::string> result;
        try {
            nb::gil_scoped_release release;
            std::vector<std::string> encoded;
            encoded.reserve(values.size());
            std::size_t total = 0;
            for (const auto &[id, value] : values) {
                auto bytes = codec_.encode(id, value);
                if (bytes.size() > 16 * 1024 * 1024 - total) {
                    throw std::length_error("batch_byte_limit");
                }
                total += bytes.size();
                encoded.push_back(std::move(bytes));
            }
            std::vector<std::string_view> views;
            views.reserve(encoded.size());
            for (const auto &bytes : encoded) {
                views.emplace_back(bytes);
            }
            result = wire_.encode(views);
        }
        catch (const std::exception &error) {
            fail("EncodeError", codec_.protocol(), -1, error.what());
        }
        if (!result) {
            fail("EncodeError", codec_.protocol(), -1, result.error());
        }
        return nb::bytes(result->data(), result->size());
    }

    PacketBatch decode(nb::handle data, nb::handle packet_ids)
    {
        Access access(*this);
        std::optional<std::set<int>> selected;
        if (!packet_ids.is_none()) {
            selected.emplace();
            for (auto value : nb::borrow<nb::object>(packet_ids)) {
                const int id = nb::cast<int>(value);
                if (id < 0 || id > 1023) {
                    throw nb::value_error("packet_ids must fit 10 bits");
                }
                selected->insert(id);
            }
        }
        std::string bytes;
        try {
            bytes = snapshotBuffer(data, 32 * 1024 * 1024 + 1024);
        }
        catch (const nb::builtin_exception &) {
            terminal_ = true;
            wire_.invalidate();
            fail("DecodeError", codec_.protocol(), -1, "wire_byte_limit");
        }
        struct Failure {
            Batch &batch;
            bool success = false;
            ~Failure()
            {
                if (!success) {
                    batch.terminal_ = true;
                    batch.wire_.invalidate();
                }
            }
        } failure{*this};
        auto packets = std::make_shared<std::vector<DecodedPacket>>();
        PacketBatch batch{packets, {}, {}, selected.has_value(), codec_.protocol()};
        std::optional<NativeError> error;
        {
            nb::gil_scoped_release release;
            auto frames = wire_.decode(bytes);
            if (!frames) {
                error = NativeError{frames.error()};
            }
            else {
                batch.headers.reserve(frames->size());
                for (const auto &frame : *frames) {
                    bp::BinaryReader reader(frame);
                    auto header = readHeader(reader);
                    if (!header) {
                        error = header.error();
                        break;
                    }
                    batch.headers.push_back(*header);
                }
                bp::DecodeBudget budget(codec_.limits());
                if (!error) {
                    for (std::size_t i = 0; i < frames->size(); ++i) {
                        const int id = batch.headers[i].packet_id;
                        if (selected && !selected->contains(id)) {
                            batch.skipped.push_back(id);
                            continue;
                        }
                        auto packet = codec_.decode((*frames)[i], budget);
                        if (!packet) {
                            error = packet.error();
                            break;
                        }
                        packets->push_back(std::move(*packet));
                    }
                }
            }
        }
        if (error) {
            fail("DecodeError", codec_.protocol(), error->packet_id, error->reason, error->offset);
        }
        failure.success = true;
        return batch;
    }

private:
    const Codec codec_;
    wire::BatchCodec wire_;
    const std::thread::id owner_ = std::this_thread::get_id();
    bool busy_ = false;
    bool terminal_ = false;
};
}  // namespace

void bindWire(nb::module_ &module)
{
    auto wire_module = module.def_submodule("wire");
    nb::enum_<wire::Compression>(wire_module, "Compression")
        .value("ZLIB", wire::Compression::zlib)
        .value("SNAPPY", wire::Compression::snappy)
        .value("NONE", wire::Compression::none);
    nb::class_<BatchIterator>(wire_module, "_BatchIterator")
        .def(
            "__iter__", [](BatchIterator &self) -> BatchIterator & { return self; }, nb::rv_policy::reference)
        .def("__next__", [](BatchIterator &self) {
            if (self.offset == self.packets->size()) {
                throw nb::stop_iteration();
            }
            return (*self.packets)[self.offset++];
        });
    nb::class_<PacketBatch>(wire_module, "PacketBatch")
        .def_ro("protocol", &PacketBatch::protocol)
        .def_ro("filtered", &PacketBatch::filtered)
        .def_prop_ro("packet_count", [](const PacketBatch &self) { return self.headers.size(); })
        .def_prop_ro("decoded_count", [](const PacketBatch &self) { return self.packets->size(); })
        .def_prop_ro("headers",
                     [](const PacketBatch &self) {
                         nb::list result;
                         for (const auto &header : self.headers) {
                             result.append(header);
                         }
                         return nb::tuple(result);
                     })
        .def_prop_ro("skipped_packet_ids",
                     [](const PacketBatch &self) {
                         nb::list result;
                         for (const auto id : self.skipped) {
                             result.append(id);
                         }
                         return nb::tuple(result);
                     })
        .def("__len__", [](const PacketBatch &self) { return self.packets->size(); })
        .def("__getitem__",
             [](const PacketBatch &self, Py_ssize_t i) { return (*self.packets)[index(i, self.packets->size())]; })
        .def("__iter__", [](const PacketBatch &self) { return BatchIterator{self.packets}; });
    nb::class_<Batch>(wire_module, "BatchCodec")
        .def(nb::init<const Codec &>(), nb::arg("codec"))
        .def("set_compression", &Batch::compression, nb::arg("algorithm"), nb::arg("threshold"))
        .def("enable_encryption", &Batch::encryption, nb::arg("key"))
        .def("encode", &Batch::encode, nb::arg("packets"))
        .def("decode", &Batch::decode, nb::arg("data"), nb::kw_only(), nb::arg("packet_ids") = nb::none());
}
}  // namespace bedrock::protocol::python
