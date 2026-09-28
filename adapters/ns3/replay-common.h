// SPDX-License-Identifier: GPL-2.0-only
// Shared input/version checks and native receive hook for the two replay programs.

#ifndef OPENLEO_REPLAY_COMMON_H
#define OPENLEO_REPLAY_COMMON_H

#include "ns3/error-model.h"
#include "ns3/packet.h"
#include "ns3/version.h"

#include <charconv>
#include <functional>
#include <iostream>
#include <stdexcept>
#include <string>
#include <vector>

#ifndef ENABLE_BUILD_VERSION
#error "Configure ns-3 with --enable-build-version"
#endif

namespace openleo
{

/** Parse a complete unsigned decimal field. @param value Input. @return Integer. */
inline uint64_t
Unsigned(const std::string& value)
{
    uint64_t result = 0;
    const auto [end, error] = std::from_chars(value.data(), value.data() + value.size(), result);
    if (value.empty() || error != std::errc() || end != value.data() + value.size())
    {
        throw std::runtime_error("expected an unsigned decimal integer");
    }
    return result;
}

/** Split at most 144 unquoted fields, preserving empties. @param text Input. @param sep Separator.
 * @return Fields.
 */
inline std::vector<std::string>
Split(const std::string& text, char sep)
{
    std::vector<std::string> fields;
    size_t start = 0;
    do
    {
        if (fields.size() >= 144)
        {
            throw std::runtime_error("CSV field/path count exceeds supported bounds");
        }
        const auto end = text.find(sep, start);
        fields.push_back(text.substr(start, end - start));
        if (end == std::string::npos)
        {
            break;
        }
        start = end + 1;
    } while (true);
    return fields;
}

/** Check linked version and optional handshake. @param argc Count. @param argv Arguments.
 * @param protocol Handshake protocol. @return Whether the handshake was printed.
 */
inline bool
PrintVersion(int argc, char** argv, const std::string& protocol)
{
    if (ns3::Version::Major() != 3 || ns3::Version::Minor() != 48)
    {
        throw std::runtime_error("this backend requires the actual ns-3.48 libraries");
    }
    if (argc != 2 || std::string(argv[1]) != "--PrintVersion")
    {
        return false;
    }
    std::cout << protocol << " ns-" << ns3::Version::Major() << '.' << ns3::Version::Minor()
              << '\n';
    return true;
}

/** Apply replay outage intervals inside the real ns-3 receive path. */
class OutageErrorModel : public ns3::ErrorModel
{
  public:
    std::function<bool(ns3::Ptr<ns3::Packet>)> corrupt; ///< Replay packet-identity lookup.

  private:
    bool DoCorrupt(ns3::Ptr<ns3::Packet> packet) override
    {
        return corrupt(packet);
    }

    void DoReset() override
    {
    }
};

} // namespace openleo

#endif // OPENLEO_REPLAY_COMMON_H
