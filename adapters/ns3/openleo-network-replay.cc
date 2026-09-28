// SPDX-License-Identifier: GPL-2.0-only
// Bounded native IPv4/UDP multi-hop replay for ns-3.48.

#include "replay-common.h"

#include "ns3/applications-module.h"
#include "ns3/core-module.h"
#include "ns3/internet-module.h"
#include "ns3/network-module.h"
#include "ns3/point-to-point-module.h"

#include <algorithm>
#include <array>
#include <filesystem>
#include <fstream>
#include <iostream>
#include <map>
#include <set>
#include <stdexcept>
#include <string>
#include <tuple>
#include <unordered_map>
#include <vector>

using namespace ns3;
using openleo::OutageErrorModel;
using openleo::Split;
using openleo::Unsigned;

namespace
{
/** Overflow-safe flow phase. @param flow Flow. @param period Interval.
 * @param count Flow count. @return Phase nanoseconds.
 */
uint64_t
Phase(uint32_t flow, uint64_t period, uint32_t count)
{
    return (period / count) * flow + (period % count) * flow / count;
}

/** One edge's state for a complete frame. */
struct State
{
    uint64_t rate;  ///< Bits per second, zero if unavailable.
    int64_t delay;  ///< Propagation nanoseconds.
    bool available; ///< Edge admission state.
};

/** Stable physical edge identity. */
struct Edge
{
    uint64_t id; ///< CSV edge ID.
    uint32_t a;  ///< First node.
    uint32_t b;  ///< Second node.
};

/** One atomic routing and physical state update. */
struct Frame
{
    int64_t time;               ///< Left boundary, nanoseconds.
    std::string state;          ///< ready, disconnected, acquiring or stop.
    std::vector<uint32_t> path; ///< Selected source-to-target path.
    std::vector<State> edges;   ///< Complete edge state vector.
};

/** Validated configuration, prior to topology or output creation. */
struct Options
{
    std::string output;        ///< Output directory.
    uint32_t source;           ///< Forward endpoint.
    uint32_t target;           ///< Reverse endpoint.
    uint32_t nodes;            ///< Node count.
    uint64_t interval;         ///< Period per flow, nanoseconds.
    uint32_t packetSize;       ///< UDP payload bytes including measurement header.
    uint32_t queuePackets;     ///< Waiting device FIFO limit.
    uint32_t flows;            ///< Flows per direction.
    uint32_t seed;             ///< ns-3 seed.
    uint64_t offered = 0;      ///< Total scheduled opportunities.
    std::vector<Edge> edges;   ///< Stable topology.
    std::vector<Frame> frames; ///< Synchronized routing/physical frames.
};

/** Open a bounded CSV with an exact header. @param path File. @param header Header.
 * @param limit Maximum bytes. @return Input stream.
 */
std::ifstream
OpenCsv(const std::string& path, const std::string& header, uintmax_t limit)
{
    if (std::filesystem::file_size(path) > limit)
    {
        throw std::runtime_error("CSV exceeds input byte budget");
    }
    std::ifstream input(path);
    std::string line;
    if (!std::getline(input, line) || line != header)
    {
        throw std::runtime_error("invalid CSV header");
    }
    return input;
}

/** Parse and bound every route before building simulation objects. @param options Options.
 * @param file Routes CSV.
 */
void
ReadRoutes(Options& options, const std::string& file)
{
    auto input = OpenCsv(file, "time_ns,state,path", 4 * 1024 * 1024);
    std::string line;
    while (std::getline(input, line))
    {
        const auto fields = Split(line, ',');
        if (fields.size() != 3 || options.frames.size() >= 4096)
        {
            throw std::runtime_error("routes require three fields and at most 4096 frames");
        }
        const auto time = Unsigned(fields[0]);
        if (time > 3600000000000ULL ||
            (options.frames.empty() ? time != 0 : time <= uint64_t(options.frames.back().time)) ||
            (!options.frames.empty() && options.frames.back().state == "stop"))
        {
            throw std::runtime_error("invalid frame order or window");
        }
        Frame frame{static_cast<int64_t>(time), fields[1], {}, {}};
        if (frame.state == "ready")
        {
            std::set<uint32_t> visited;
            for (const auto& field : Split(fields[2], ';'))
            {
                const auto node = Unsigned(field);
                if (node >= options.nodes || !visited.insert(node).second)
                {
                    throw std::runtime_error("route requires unique valid node IDs");
                }
                frame.path.push_back(node);
            }
            if (frame.path.size() < 2 || frame.path.front() != options.source ||
                frame.path.back() != options.target)
            {
                throw std::runtime_error("ready route must run from source to target");
            }
        }
        else if ((frame.state != "disconnected" && frame.state != "acquiring" &&
                  frame.state != "stop") ||
                 !fields[2].empty())
        {
            throw std::runtime_error("invalid route state or nonempty inactive path");
        }
        options.frames.push_back(frame);
    }
    if (input.bad() || options.frames.size() < 2 || options.frames.back().state != "stop")
    {
        throw std::runtime_error("require at least two frames and terminal stop");
    }
    const auto end = uint64_t(options.frames.back().time);
    for (uint32_t flow = 0; flow < options.flows; ++flow)
    {
        const auto phase = Phase(flow, options.interval, options.flows);
        if (phase < end)
        {
            options.offered += 2 * ((end - 1 - phase) / options.interval + 1);
        }
    }
    size_t maxHops = 1;
    for (const auto& frame : options.frames)
    {
        maxHops = std::max(maxHops, frame.path.empty() ? size_t(1) : frame.path.size() - 1);
    }
    if (options.offered > 200000 || options.offered * maxHops > 500000)
    {
        throw std::runtime_error("offered packet or packet-hop budget exceeded");
    }
}

/** Parse complete edge frames and check each chosen route. @param options Options.
 * @param file Edges CSV.
 */
void
ReadEdges(Options& options, const std::string& file)
{
    auto input = OpenCsv(file,
                         "time_ns,edge_id,node_a,node_b,rate_bps,delay_ns,available",
                         32 * 1024 * 1024);
    std::map<uint64_t, size_t> ids;
    std::map<std::pair<uint32_t, uint32_t>, size_t> pairs;
    size_t frame = 0;
    size_t rows = 0;
    std::set<size_t> seen;
    std::string line;
    while (std::getline(input, line))
    {
        const auto fields = Split(line, ',');
        if (fields.size() != 7 || ++rows > 200000)
        {
            throw std::runtime_error("edges require seven fields and at most 200000 rows");
        }
        std::array<uint64_t, 7> values{};
        std::transform(fields.begin(), fields.end(), values.begin(), Unsigned);
        const auto [time, id, a, b, rate, delay, available] = values;
        if (time != uint64_t(options.frames[frame].time))
        {
            if (seen.size() != options.edges.size() || frame + 1 >= options.frames.size() ||
                time != uint64_t(options.frames[frame + 1].time))
            {
                throw std::runtime_error("edge frames must exactly match route times");
            }
            ++frame;
            seen.clear();
            options.frames[frame].edges.resize(options.edges.size());
        }
        if (a >= options.nodes || b >= options.nodes || a == b || delay > 1000000000 ||
            available > 1 || rate > 1000000000000ULL || (available ? rate == 0 : rate != 0))
        {
            throw std::runtime_error("edge state outside supported bounds");
        }
        if (frame == 0)
        {
            if (options.edges.size() >= 1024 || !ids.emplace(id, options.edges.size()).second ||
                !pairs.emplace(std::minmax(uint32_t(a), uint32_t(b)), options.edges.size()).second)
            {
                throw std::runtime_error("duplicate edge ID/pair or excessive edge count");
            }
            options.edges.push_back({id, uint32_t(a), uint32_t(b)});
            options.frames[0].edges.push_back({rate, int64_t(delay), available == 1});
        }
        const auto found = ids.find(id);
        if (found == ids.end() || options.edges[found->second].a != a ||
            options.edges[found->second].b != b || !seen.insert(found->second).second)
        {
            throw std::runtime_error("edge identities/pairs must be stable and unique per frame");
        }
        options.frames[frame].edges[found->second] = {rate, int64_t(delay), available == 1};
    }
    if (input.bad() || (!options.edges.empty() && (frame + 1 != options.frames.size() ||
                                                   seen.size() != options.edges.size())))
    {
        throw std::runtime_error("missing complete edge frames");
    }
    for (const auto& route : options.frames)
    {
        for (size_t i = 1; i < route.path.size(); ++i)
        {
            const auto edge = pairs.find(std::minmax(route.path[i - 1], route.path[i]));
            if (edge == pairs.end() || !route.edges[edge->second].available)
            {
                throw std::runtime_error("ready route traverses a missing or unavailable edge");
            }
        }
    }
}

/** Validate all command line inputs. @param argc Count. @param argv Arguments.
 * @return Validated options.
 */
Options
ReadOptions(int argc, char** argv)
{
    std::map<std::string, std::string> args;
    for (int i = 1; i < argc; ++i)
    {
        const std::string arg(argv[i]);
        const auto sep = arg.find('=');
        if (arg.rfind("--", 0) != 0 || sep == std::string::npos ||
            !args.emplace(arg.substr(2, sep - 2), arg.substr(sep + 1)).second)
        {
            throw std::runtime_error("expected unique --name=value arguments");
        }
    }
    if (args.size() != 11)
    {
        throw std::runtime_error("require edges, routes, output, source, target, nodeCount, "
                                 "intervalNs, packetSize, queuePackets, flows, seed");
    }
    const auto source = Unsigned(args.at("source"));
    const auto target = Unsigned(args.at("target"));
    const auto nodes = Unsigned(args.at("nodeCount"));
    const auto interval = Unsigned(args.at("intervalNs"));
    const auto packet = Unsigned(args.at("packetSize"));
    const auto queue = Unsigned(args.at("queuePackets"));
    const auto flows = Unsigned(args.at("flows"));
    const auto seed = Unsigned(args.at("seed"));
    if (nodes < 2 || nodes > 144 || source >= nodes || target >= nodes || source == target ||
        interval == 0 || interval > INT64_MAX || packet < 64 || packet > 1400 || queue == 0 ||
        queue > 10000 || flows == 0 || flows > 4 || seed == 0 || seed > INT32_MAX ||
        args.at("output").empty())
    {
        throw std::runtime_error("network replay argument outside supported bounds");
    }
    Options options{args.at("output"),
                    uint32_t(source),
                    uint32_t(target),
                    uint32_t(nodes),
                    interval,
                    uint32_t(packet),
                    uint32_t(queue),
                    uint32_t(flows),
                    uint32_t(seed),
                    0,
                    {},
                    {}};
    ReadRoutes(options, args.at("routes"));
    ReadEdges(options, args.at("edges"));
    return options;
}

/** One source opportunity and its final accounting. */
struct Record
{
    uint32_t direction;                ///< 0 source to target, 1 reversed.
    uint32_t flow;                     ///< Independent flow index.
    uint32_t sequence;                 ///< Sequence per direction and flow.
    int64_t offered;                   ///< Source opportunity nanoseconds.
    int64_t udp = -1;                  ///< Actual UDP send attempt.
    int64_t rx = -1;                   ///< Successful UDP delivery only.
    std::string status = "unresolved"; ///< Terminal outcome.
    std::vector<size_t> hops;          ///< Actual transmission records.
};

/** Actual wire transmission with the state captured at PHY TX. */
struct Hop
{
    size_t record;                     ///< Parent packet record.
    size_t index;                      ///< Zero-based packet hop index.
    size_t edge;                       ///< Physical edge index.
    uint32_t txNode;                   ///< Transmitter.
    uint32_t rxNode;                   ///< Receiver.
    int64_t tx;                        ///< Actual PHY TX start.
    int64_t rx = -1;                   ///< Actual receive completion.
    uint64_t rate;                     ///< Captured bits per second.
    int64_t delay;                     ///< Captured propagation nanoseconds.
    int64_t serialization;             ///< Captured serialization nanoseconds.
    std::string status = "unresolved"; ///< received or rx_outage_drop.
};

/** Physical devices, IP routes, UDP sockets and trace-based measurement. */
class Replay
{
  public:
    /** @param options Validated inputs. */
    explicit Replay(const Options& options)
        : m_options(options),
          m_end(options.frames.back().time)
    {
    }

    /** Build, run, drain and reconcile the native ns-3 experiment. */
    void Run()
    {
        Time::SetResolution(Time::NS);
        RngSeedManager::SetSeed(m_options.seed);
        Config::SetDefault("ns3::Ipv4L3Protocol::DefaultTtl", UintegerValue(255));
        Build();
        int64_t drain = 0;
        m_outages.resize(m_options.edges.size());
        for (size_t i = 0; i < m_options.frames.size(); ++i)
        {
            const auto& frame = m_options.frames[i];
            Simulator::Schedule(NanoSeconds(frame.time), &Replay::Update, this, i);
            if (i + 1 == m_options.frames.size())
            {
                continue;
            }
            for (size_t edge = 0; edge < frame.edges.size(); ++edge)
            {
                const auto& state = frame.edges[edge];
                if (!state.available)
                {
                    m_outages[edge].emplace_back(frame.time, m_options.frames[i + 1].time);
                }
                else
                {
                    drain =
                        std::max(drain,
                                 state.delay + DataRate(state.rate)
                                                   .CalculateBytesTxTime(m_options.packetSize + 30)
                                                   .GetNanoSeconds());
                }
            }
        }
        m_records.reserve(m_options.offered);
        for (uint32_t direction = 0; direction < 2; ++direction)
        {
            for (uint32_t flow = 0; flow < m_options.flows; ++flow)
            {
                uint32_t sequence = 0;
                const auto phase = Phase(flow, m_options.interval, m_options.flows);
                for (uint64_t time = phase; time < uint64_t(m_end); time += m_options.interval)
                {
                    m_records.push_back(
                        {direction, flow, sequence++, int64_t(time), -1, -1, "unresolved", {}});
                }
            }
        }
        std::sort(m_records.begin(), m_records.end(), [](const auto& a, const auto& b) {
            return std::tie(a.offered, a.direction, a.flow, a.sequence) <
                   std::tie(b.offered, b.direction, b.flow, b.sequence);
        });
        for (size_t i = 0; i < m_records.size(); ++i)
        {
            Simulator::Schedule(NanoSeconds(m_records[i].offered), &Replay::Offer, this, i);
        }
        Simulator::Stop(NanoSeconds(m_end + drain + 1));
        Simulator::Run();
        Write();
        Simulator::Destroy();
    }

  private:
    /** Build real P2P links with a single device DropTail FIFO per direction. */
    void Build()
    {
        m_nodes.Create(m_options.nodes);
        InternetStackHelper stack;
        Ipv4StaticRoutingHelper routing;
        stack.SetRoutingHelper(routing);
        stack.SetIpv6StackInstall(false);
        stack.Install(m_nodes);
        for (uint32_t node = 0; node < m_options.nodes; ++node)
        {
            const auto ip = m_nodes.Get(node)->GetObject<Ipv4>();
            ip->AddAddress(0, Ipv4InterfaceAddress(Address(node), Ipv4Mask("255.255.255.255")));
            ip->GetObject<Ipv4L3Protocol>()->TraceConnectWithoutContext(
                "Drop",
                MakeCallback(&Replay::IpDrop, this));
            // UDP-only experiment: keep native TTL drops but suppress ICMP control traffic.
            m_nodes.Get(node)->GetObject<Icmpv4L4Protocol>()->SetDownTarget(MakeCallback(
                +[](Ptr<Packet>, Ipv4Address, Ipv4Address, uint8_t, Ptr<Ipv4Route>) {}));
            m_routes.push_back(routing.GetStaticRouting(ip));
        }
        PointToPointHelper helper;
        helper.DisableFlowControl();
        helper.SetDeviceAttribute("DataRate", DataRateValue(DataRate(1)));
        helper.SetQueue("ns3::DropTailQueue<Packet>",
                        "MaxSize",
                        QueueSizeValue(QueueSize(std::to_string(m_options.queuePackets) + "p")));
        for (size_t edge = 0; edge < m_options.edges.size(); ++edge)
        {
            const auto& definition = m_options.edges[edge];
            const auto devices =
                helper.Install(m_nodes.Get(definition.a), m_nodes.Get(definition.b));
            Ipv4AddressHelper addresses;
            addresses.SetBase(Ipv4Address(0x0a000000 + uint32_t(edge) * 4),
                              Ipv4Mask("255.255.255.252"));
            addresses.Assign(devices);
            std::array<Ptr<PointToPointNetDevice>, 2> pair;
            for (uint32_t side = 0; side < 2; ++side)
            {
                pair[side] = DynamicCast<PointToPointNetDevice>(devices.Get(side));
                pair[side]->TraceConnectWithoutContext(
                    "PhyTxBegin",
                    MakeCallback(&Replay::Transmit, this).Bind(edge, side));
                pair[side]->GetQueue()->TraceConnectWithoutContext(
                    "Drop",
                    MakeCallback(&Replay::QueueDrop, this));
                auto error = CreateObject<OutageErrorModel>();
                error->corrupt = [this, edge, side](Ptr<Packet> packet) {
                    return Corrupt(edge, side, packet);
                };
                pair[side]->SetReceiveErrorModel(error);
            }
            m_devices.push_back(pair);
            m_channels.push_back(DynamicCast<PointToPointChannel>(devices.Get(0)->GetChannel()));
            m_pairs.emplace(std::minmax(definition.a, definition.b), edge);
        }
        for (uint32_t direction = 0; direction < 2; ++direction)
        {
            const uint32_t node = direction == 0 ? m_options.source : m_options.target;
            const uint32_t peer = direction == 0 ? m_options.target : m_options.source;
            auto receiver = Socket::CreateSocket(m_nodes.Get(node), UdpSocketFactory::GetTypeId());
            if (receiver->Bind(InetSocketAddress(Address(node), 9000)) != 0)
            {
                throw std::runtime_error("cannot bind UDP receiver");
            }
            receiver->SetRecvCallback(MakeCallback(&Replay::Receive, this));
            m_receivers.push_back(receiver);
            for (uint32_t flow = 0; flow < m_options.flows; ++flow)
            {
                auto source =
                    Socket::CreateSocket(m_nodes.Get(node), UdpSocketFactory::GetTypeId());
                if (source->Bind(InetSocketAddress(Address(node), 10000 + flow)) != 0 ||
                    source->Connect(InetSocketAddress(Address(peer), 9000)) != 0)
                {
                    throw std::runtime_error("cannot connect UDP source");
                }
                m_sources[direction].push_back(source);
            }
        }
    }

    /** Endpoint /32 independent of its current first-hop interface. @param node Node ID.
     * @return Endpoint address.
     */
    static Ipv4Address Address(uint32_t node)
    {
        return Ipv4Address(0xac100001 + node);
    }

    /** Atomically replace physical state and all explicit host routes. @param index Frame. */
    void Update(size_t index)
    {
        m_frame = index;
        const auto& frame = m_options.frames[index];
        for (auto& routing : m_routes)
        {
            while (routing->GetNRoutes() > 0)
            {
                routing->RemoveRoute(0);
            }
        }
        for (size_t edge = 0; edge < m_devices.size(); ++edge)
        {
            const auto& state = frame.edges[edge];
            if (frame.state == "stop" || !state.available)
            {
                m_dropStatus = frame.state == "stop" ? "end_of_window_drop" : "outage_queue_drop";
                for (auto& device : m_devices[edge])
                {
                    device->GetQueue()->Flush();
                }
                m_dropStatus = "queue_drop";
            }
            m_channels[edge]->SetAttribute("Delay", TimeValue(NanoSeconds(state.delay)));
            if (state.available)
            {
                for (auto& device : m_devices[edge])
                {
                    device->SetDataRate(DataRate(state.rate));
                }
            }
        }
        for (size_t i = 1; i < frame.path.size(); ++i)
        {
            AddRoute(frame.path[i - 1], frame.path[i], m_options.target);
            AddRoute(frame.path[i], frame.path[i - 1], m_options.source);
        }
    }

    /** Install a native IPv4 host route. @param node Router. @param next Next hop.
     * @param target Destination endpoint.
     */
    void AddRoute(uint32_t node, uint32_t next, uint32_t target)
    {
        const auto edge = m_pairs.at(std::minmax(node, next));
        const uint32_t side = node == m_options.edges[edge].a ? 0 : 1;
        const auto ip = m_nodes.Get(node)->GetObject<Ipv4>();
        const auto peer = m_nodes.Get(next)->GetObject<Ipv4>();
        const auto interface = ip->GetInterfaceForDevice(m_devices[edge][side]);
        const auto peerInterface = peer->GetInterfaceForDevice(m_devices[edge][1 - side]);
        m_routes[node]->AddHostRouteTo(Address(target),
                                       peer->GetAddress(peerInterface, 0).GetLocal(),
                                       interface);
    }

    /** Offer one UDP payload. @param index Opportunity index. */
    void Offer(size_t index)
    {
        auto& record = m_records[index];
        const auto& state = m_options.frames[m_frame].state;
        if (state != "ready")
        {
            record.status = state == "acquiring" ? "acquisition_suppressed" : "no_route";
            return;
        }
        SeqTsHeader header;
        header.SetSeq(record.sequence);
        auto packet = Create<Packet>(m_options.packetSize - header.GetSerializedSize());
        packet->AddHeader(header);
        m_packetRecords.emplace(packet->GetUid(), index);
        record.udp = Simulator::Now().GetNanoSeconds();
        const auto sent = m_sources[record.direction][record.flow]->Send(packet);
        if (sent != int(m_options.packetSize) && record.status == "unresolved")
        {
            record.status = "send_error";
        }
    }

    /** Capture actual device transmission. @param edge Edge. @param side Device side.
     * @param packet Wire packet.
     */
    void Transmit(size_t edge, uint32_t side, Ptr<const Packet> packet)
    {
        const auto found = m_packetRecords.find(packet->GetUid());
        if (found == m_packetRecords.end())
        {
            throw std::runtime_error("unexpected unmeasured network transmission");
        }
        auto& record = m_records[found->second];
        const auto& state = m_options.frames[m_frame].edges[edge];
        const auto& definition = m_options.edges[edge];
        const auto now = Simulator::Now().GetNanoSeconds();
        if (m_hops.size() >= 500000)
        {
            throw std::runtime_error("actual PHY transmission budget exceeded");
        }
        if (now >= m_end || !state.available || packet->GetSize() != m_options.packetSize + 30)
        {
            throw std::runtime_error("unexpected transmission outside admitted wire contract");
        }
        m_hops.push_back(
            {found->second,
             record.hops.size(),
             edge,
             side == 0 ? definition.a : definition.b,
             side == 0 ? definition.b : definition.a,
             now,
             -1,
             state.rate,
             state.delay,
             DataRate(state.rate).CalculateBytesTxTime(packet->GetSize()).GetNanoSeconds(),
             "unresolved"});
        record.hops.push_back(m_hops.size() - 1);
    }

    /** Account once from the real FIFO drop trace. @param packet Dropped packet. */
    void QueueDrop(Ptr<const Packet> packet)
    {
        const auto found = m_packetRecords.find(packet->GetUid());
        if (found != m_packetRecords.end())
        {
            m_records[found->second].status = m_dropStatus;
        }
    }

    /** Native IPv4 failure, including post-stop intermediary arrivals. @param header IP header.
     * @param packet Payload. @param reason Native reason. @param ip IP stack.
     * @param interface Interface.
     */
    void IpDrop(const Ipv4Header& header,
                Ptr<const Packet> packet,
                Ipv4L3Protocol::DropReason reason,
                Ptr<Ipv4> ip,
                uint32_t interface)
    {
        const auto found = m_packetRecords.find(packet->GetUid());
        if (found != m_packetRecords.end())
        {
            auto& record = m_records[found->second];
            if (record.status == "unresolved")
            {
                record.status = Simulator::Now().GetNanoSeconds() >= m_end ? "end_of_window_drop"
                                                                           : "route_drop";
            }
        }
    }

    /** Apply half-open TX/RX bit outage intervals to an actual receive event.
     * @param edge Edge. @param side Receiving side. @param packet Wire packet.
     * @return Whether native PHY reception is corrupt.
     */
    bool Corrupt(size_t edge, uint32_t side, Ptr<Packet> packet)
    {
        auto& record = m_records.at(m_packetRecords.at(packet->GetUid()));
        auto& hop = m_hops.at(record.hops.back());
        const auto node = side == 0 ? m_options.edges[edge].a : m_options.edges[edge].b;
        if (hop.edge != edge || hop.rxNode != node || hop.rx >= 0)
        {
            throw std::runtime_error("physical hop identity mismatch");
        }
        hop.rx = Simulator::Now().GetNanoSeconds();
        const auto& outages = m_outages[edge];
        const auto overlaps = [&outages](int64_t start, int64_t end) {
            const auto outage = std::lower_bound(
                outages.begin(),
                outages.end(),
                start,
                [](const auto& interval, int64_t time) { return interval.second <= time; });
            return outage != outages.end() && outage->first < end;
        };
        const bool corrupt =
            overlaps(hop.tx, hop.tx + hop.serialization) || overlaps(hop.tx + hop.delay, hop.rx);
        hop.status = corrupt ? "rx_outage_drop" : "received";
        if (corrupt)
        {
            record.status = "rx_outage_drop";
        }
        return corrupt;
    }

    /** Reconcile actual endpoint delivery. @param socket UDP receiver. */
    void Receive(Ptr<Socket> socket)
    {
        while (auto packet = socket->Recv())
        {
            auto& record = m_records.at(m_packetRecords.at(packet->GetUid()));
            SeqTsHeader header;
            if (packet->GetSize() != m_options.packetSize || packet->RemoveHeader(header) != 12 ||
                header.GetSeq() != record.sequence || record.status != "unresolved")
            {
                throw std::runtime_error("unexpected UDP payload identity or duplicate delivery");
            }
            record.rx = Simulator::Now().GetNanoSeconds();
            record.status = record.rx < m_end ? "received" : "received_after_window";
        }
    }

    /** Write complete ordered packet and hop tables after the bounded drain. */
    void Write() const
    {
        std::filesystem::create_directories(m_options.output);
        std::ofstream packets(std::filesystem::path(m_options.output) / "packets.csv");
        packets.exceptions(std::ios::failbit | std::ios::badbit);
        packets << "direction,flow,sequence,offered_time_ns,udp_tx_time_ns,rx_time_ns,status,"
                   "payload_bytes,hop_count\n";
        for (const auto& record : m_records)
        {
            packets << record.direction << ',' << record.flow << ',' << record.sequence << ','
                    << record.offered << ',';
            packets << (record.udp < 0 ? "" : std::to_string(record.udp)) << ','
                    << (record.rx < 0 ? "" : std::to_string(record.rx));
            packets << ',' << record.status << ',' << m_options.packetSize << ','
                    << record.hops.size() << '\n';
        }
        packets.close();
        std::vector<size_t> order;
        for (size_t i = 0; i < m_hops.size(); ++i)
        {
            order.push_back(i);
        }
        std::sort(order.begin(), order.end(), [this](size_t a, size_t b) {
            const auto& ha = m_hops[a];
            const auto& hb = m_hops[b];
            const auto& ra = m_records[ha.record];
            const auto& rb = m_records[hb.record];
            return std::tie(ra.direction, ra.flow, ra.sequence, ha.index) <
                   std::tie(rb.direction, rb.flow, rb.sequence, hb.index);
        });
        std::ofstream hops(std::filesystem::path(m_options.output) / "hops.csv");
        hops.exceptions(std::ios::failbit | std::ios::badbit);
        hops << "direction,flow,sequence,hop_index,edge_id,tx_node,rx_node,phy_tx_time_ns,"
                "phy_rx_time_ns,rate_bps,delay_ns,status\n";
        for (const auto i : order)
        {
            const auto& hop = m_hops[i];
            const auto& record = m_records[hop.record];
            if (hop.rx < 0 || hop.status == "unresolved")
            {
                throw std::runtime_error("drain ended before physical receive completion");
            }
            hops << record.direction << ',' << record.flow << ',' << record.sequence << ','
                 << hop.index << ',' << m_options.edges[hop.edge].id << ',' << hop.txNode << ','
                 << hop.rxNode << ',' << hop.tx << ',' << hop.rx << ',' << hop.rate << ','
                 << hop.delay << ',' << hop.status << '\n';
        }
        hops.close();
    }

    const Options& m_options;                     ///< Inputs alive throughout simulation.
    int64_t m_end;                                ///< Exclusive measurement end.
    size_t m_frame = 0;                           ///< Current atomic frame.
    NodeContainer m_nodes;                        ///< Native nodes.
    std::vector<Ptr<Ipv4StaticRouting>> m_routes; ///< Native routing tables.
    std::vector<std::array<Ptr<PointToPointNetDevice>, 2>> m_devices; ///< Edge devices.
    std::vector<Ptr<PointToPointChannel>> m_channels;        ///< Native propagation channels.
    std::map<std::pair<uint32_t, uint32_t>, size_t> m_pairs; ///< Node pair to physical edge.
    std::array<std::vector<Ptr<Socket>>, 2> m_sources;       ///< One UDP source per flow.
    std::vector<Ptr<Socket>> m_receivers;                    ///< Endpoint UDP receivers.
    std::vector<Record> m_records;                           ///< Ordered source opportunities.
    std::vector<Hop> m_hops;                                 ///< Actual PHY transmissions.
    std::unordered_map<uint64_t, size_t> m_packetRecords;    ///< ns-3 packet UID to record.
    std::vector<std::vector<std::pair<int64_t, int64_t>>> m_outages; ///< Edge fade intervals.
    std::string m_dropStatus = "queue_drop"; ///< Flush context for native queue trace.
};

} // namespace

/** Validate linked ns-3 version and execute. @param argc Count. @param argv Arguments.
 * @return Zero on success; one on invalid input or failure.
 */
int
main(int argc, char** argv)
{
    try
    {
        if (openleo::PrintVersion(argc, argv, "openleo-ns3-network/1"))
        {
            return 0;
        }
        const auto options = ReadOptions(argc, argv);
        Replay(options).Run();
        return 0;
    }
    catch (const std::exception& error)
    {
        std::cerr << "openleo ns-3 network replay: " << error.what() << '\n';
        Simulator::Destroy();
        return 1;
    }
}
