// SPDX-License-Identifier: GPL-2.0-only
// Bounded, synthetic full-duplex UDP replay for ns-3.48.

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
#include <stdexcept>
#include <string>
#include <unordered_map>
#include <vector>

using namespace ns3;
using openleo::OutageErrorModel;
using openleo::Unsigned;

namespace
{

/** One left-held trace state; the final state has no duration. */
struct Sample
{
    int64_t time;   ///< Nanoseconds from window start.
    uint64_t rate;  ///< Bits per second, zero during an outage.
    int64_t delay;  ///< One-way propagation nanoseconds.
    bool available; ///< Whether new packets may be admitted.
};

/** Validated replay inputs. */
struct Options
{
    std::vector<Sample> samples; ///< Input states.
    std::string output;          ///< Output CSV pathname.
    uint64_t interval;           ///< Source interval in nanoseconds.
    uint32_t packetSize;         ///< UDP payload bytes, including SeqTsHeader.
    uint32_t queuePackets;       ///< Waiting device queue capacity.
    uint32_t seed;               ///< Valid ns-3 random seed.
};

/**
 * Parse bounded CSV and CLI inputs before creating topology or output files.
 * @param argc Argument count.
 * @param argv Argument vector.
 * @return Validated replay options.
 */
Options
ReadOptions(int argc, char** argv)
{
    std::map<std::string, std::string> arguments;
    for (int i = 1; i < argc; ++i)
    {
        const std::string argument(argv[i]);
        const auto separator = argument.find('=');
        if (separator == std::string::npos || argument.rfind("--", 0) != 0 ||
            !arguments.emplace(argument.substr(2, separator - 2), argument.substr(separator + 1))
                 .second)
        {
            throw std::runtime_error("expected unique --name=value arguments");
        }
    }
    if (arguments.size() != 6)
    {
        throw std::runtime_error(
            "require trace, output, intervalNs, packetSize, queuePackets, seed");
    }
    const auto interval = Unsigned(arguments.at("intervalNs"));
    const auto packetSize = Unsigned(arguments.at("packetSize"));
    const auto queuePackets = Unsigned(arguments.at("queuePackets"));
    const auto seed = Unsigned(arguments.at("seed"));
    if (interval == 0 || interval > INT64_MAX || packetSize < 64 || packetSize > 1400 ||
        queuePackets == 0 || queuePackets > 10000 || seed == 0 || seed > INT32_MAX ||
        arguments.at("output").empty())
    {
        throw std::runtime_error("replay parameter outside supported bounds");
    }
    const auto& trace = arguments.at("trace");
    if (std::filesystem::file_size(trace) > 1024 * 1024)
    {
        throw std::runtime_error("trace exceeds one MiB input budget");
    }
    std::ifstream input(trace);
    std::string line;
    if (!std::getline(input, line) || line != "time_ns,rate_bps,delay_ns,available")
    {
        throw std::runtime_error("invalid trace CSV header");
    }
    Options options{{},
                    arguments.at("output"),
                    interval,
                    static_cast<uint32_t>(packetSize),
                    static_cast<uint32_t>(queuePackets),
                    static_cast<uint32_t>(seed)};
    while (std::getline(input, line))
    {
        const auto csv = openleo::Split(line, ',');
        std::array<uint64_t, 4> fields{};
        if (csv.size() != fields.size())
        {
            throw std::runtime_error("trace CSV requires exactly four fields");
        }
        std::transform(csv.begin(), csv.end(), fields.begin(), Unsigned);
        const auto [time, rate, delay, available] = fields;
        if (time > 3600000000000ULL || delay > 1000000000 || available > 1 ||
            rate > 1000000000000ULL || (available == 0 ? rate != 0 : rate == 0) ||
            (options.samples.empty() ? time != 0 : time <= uint64_t(options.samples.back().time)) ||
            options.samples.size() >= 4096 ||
            (available && (packetSize + 30) * 8 * 1000000000ULL < rate))
        {
            throw std::runtime_error("trace state outside supported bounds or time order");
        }
        options.samples.push_back(
            {static_cast<int64_t>(time), rate, static_cast<int64_t>(delay), available == 1});
    }
    if (input.bad() || options.samples.size() < 2 ||
        (uint64_t(options.samples.back().time) - 1) / interval + 1 > 100000)
    {
        throw std::runtime_error("require 2..4096 states and at most 200000 offered packets");
    }
    return options;
}

/** One offered opportunity and its actual ns-3 lifecycle timestamps. */
struct Record
{
    uint32_t direction;                ///< Source node index.
    uint32_t sequence;                 ///< Per-direction sequence number.
    int64_t offered;                   ///< Scheduled source time.
    int64_t udp = -1;                  ///< UDP send attempt time.
    int64_t phy = -1;                  ///< Actual device transmission start.
    int64_t rx = -1;                   ///< Physical receive completion.
    int64_t serialization = 0;         ///< Serialization captured at transmission start.
    int64_t delay = 0;                 ///< Delay captured at transmission start.
    std::string status = "unresolved"; ///< Final accounting status.
};

/** Two-node experiment; all packet scheduling and queuing belongs to ns-3. */
class Replay
{
  public:
    /** @param options Fully validated replay inputs. */
    explicit Replay(const Options& options)
        : m_options(options),
          m_current(options.samples.front()),
          m_end(options.samples.back().time)
    {
    }

    /** Construct the actual UDP topology, run and export reconciled packet rows. */
    void Run()
    {
        Time::SetResolution(Time::NS);
        RngSeedManager::SetSeed(m_options.seed);
        NodeContainer nodes;
        nodes.Create(2);
        PointToPointHelper helper;
        // Prevent both default queue-disc installation and pre-device flow-control drops.
        helper.DisableFlowControl();
        helper.SetDeviceAttribute("DataRate", DataRateValue(DataRate(1)));
        helper.SetQueue("ns3::DropTailQueue<Packet>",
                        "MaxSize",
                        QueueSizeValue(QueueSize(std::to_string(m_options.queuePackets) + "p")));
        const auto devices = helper.Install(nodes);
        m_channel = DynamicCast<PointToPointChannel>(devices.Get(0)->GetChannel());
        InternetStackHelper stack;
        stack.SetIpv6StackInstall(false);
        stack.Install(nodes);
        Ipv4AddressHelper addresses;
        addresses.SetBase("10.1.1.0", "255.255.255.0");
        const auto interfaces = addresses.Assign(devices);
        for (uint32_t direction = 0; direction < 2; ++direction)
        {
            m_devices[direction] = DynamicCast<PointToPointNetDevice>(devices.Get(direction));
            m_devices[direction]->TraceConnectWithoutContext("PhyTxBegin",
                                                             MakeCallback(&Replay::Transmit, this));
            m_devices[direction]->GetQueue()->TraceConnectWithoutContext(
                "Drop",
                MakeCallback(&Replay::QueueDrop, this));
            auto error = CreateObject<OutageErrorModel>();
            error->corrupt = [this](Ptr<Packet> packet) { return Corrupt(packet); };
            m_devices[direction]->SetReceiveErrorModel(error);
            m_receivers[direction] =
                Socket::CreateSocket(nodes.Get(direction), UdpSocketFactory::GetTypeId());
            if (m_receivers[direction]->Bind(InetSocketAddress(Ipv4Address::GetAny(), 9000)) != 0)
            {
                throw std::runtime_error("cannot bind UDP receiver");
            }
            m_receivers[direction]->SetRecvCallback(MakeCallback(&Replay::Receive, this));
            m_sources[direction] =
                Socket::CreateSocket(nodes.Get(direction), UdpSocketFactory::GetTypeId());
            if (m_sources[direction]->Bind() != 0 ||
                m_sources[direction]->Connect(
                    InetSocketAddress(interfaces.GetAddress(1 - direction), 9000)) != 0)
            {
                throw std::runtime_error("cannot connect UDP source");
            }
        }
        uint64_t minRate = UINT64_MAX;
        int64_t maxDelay = 0;
        // Schedule all state boundaries before any packet event, including the final flush.
        for (size_t i = 0; i + 1 < m_options.samples.size(); ++i)
        {
            const auto sample = m_options.samples[i];
            Simulator::Schedule(NanoSeconds(sample.time), &Replay::Update, this, sample);
            if (sample.available)
            {
                minRate = std::min(minRate, sample.rate);
            }
            else
            {
                m_outages.emplace_back(sample.time, m_options.samples[i + 1].time);
            }
            maxDelay = std::max(maxDelay, sample.delay);
        }
        Simulator::Schedule(NanoSeconds(m_end), &Replay::Flush, this, "end_of_window_drop");
        const auto count = (uint64_t(m_end) - 1) / m_options.interval + 1;
        m_records.reserve(count * 2);
        for (uint32_t sequence = 0; sequence < count; ++sequence)
        {
            const int64_t time = sequence * m_options.interval;
            for (uint32_t direction = 0; direction < 2; ++direction)
            {
                m_records.push_back({direction, sequence, time});
                Simulator::Schedule(NanoSeconds(time), &Replay::Offer, this, m_records.size() - 1);
            }
        }
        const auto drain = minRate == UINT64_MAX
                               ? 0
                               : DataRate(minRate)
                                     .CalculateBytesTxTime(m_options.packetSize + 30)
                                     .GetNanoSeconds();
        Simulator::Stop(NanoSeconds(m_end + drain + maxDelay + 1));
        Simulator::Run();
        Write();
        Simulator::Destroy();
    }

  private:
    /**
     * Apply one trace boundary before source/transmit events at that time.
     * @param sample New left-held state.
     */
    void Update(Sample sample)
    {
        if (!sample.available)
        {
            Flush("outage_queue_drop");
        }
        m_current = sample;
        m_channel->SetAttribute("Delay", TimeValue(NanoSeconds(sample.delay)));
        if (sample.available)
        {
            for (const auto& device : m_devices)
            {
                device->SetDataRate(DataRate(sample.rate));
            }
        }
    }

    /**
     * Flush waiting packets through the queue's real drop trace.
     * @param status Final classification of flushed packets.
     */
    void Flush(const std::string& status)
    {
        m_dropStatus = status;
        for (const auto& device : m_devices)
        {
            device->GetQueue()->Flush();
        }
        m_dropStatus = "queue_drop";
    }

    /**
     * Submit one synthetic UDP payload, or account for unavailable admission.
     * @param index Offered record index.
     */
    void Offer(size_t index)
    {
        auto& record = m_records[index];
        if (!m_current.available)
        {
            record.status = "outage_suppressed";
            return;
        }
        SeqTsHeader header;
        header.SetSeq(record.sequence);
        auto packet = Create<Packet>(m_options.packetSize - header.GetSerializedSize());
        packet->AddHeader(header);
        m_packetRecords.emplace(packet->GetUid(), index);
        record.udp = Simulator::Now().GetNanoSeconds();
        const auto sent = m_sources[record.direction]->Send(packet);
        if (sent != static_cast<int>(m_options.packetSize) && record.status == "unresolved")
        {
            record.status = "send_error";
        }
    }

    /**
     * Capture the channel state used by the actual ns-3 transmission.
     * @param packet Actual wire packet.
     */
    void Transmit(Ptr<const Packet> packet)
    {
        auto& record = m_records.at(m_packetRecords.at(packet->GetUid()));
        record.phy = Simulator::Now().GetNanoSeconds();
        if (record.phy >= m_end || !m_current.available)
        {
            throw std::runtime_error("unexpected transmission outside admitted window");
        }
        record.serialization =
            DataRate(m_current.rate).CalculateBytesTxTime(packet->GetSize()).GetNanoSeconds();
        record.delay = m_current.delay;
    }

    /**
     * Account once from Queue::Drop; MacTxDrop also fires and must not be counted.
     * @param packet Dropped wire packet.
     */
    void QueueDrop(Ptr<const Packet> packet)
    {
        m_records.at(m_packetRecords.at(packet->GetUid())).status = m_dropStatus;
    }

    /**
     * Test half-open outage intersections with captured TX and RX bit intervals.
     * @param packet Packet completing physical reception.
     * @return Whether the packet overlaps a declared outage.
     */
    bool Corrupt(Ptr<Packet> packet)
    {
        auto& record = m_records.at(m_packetRecords.at(packet->GetUid()));
        record.rx = Simulator::Now().GetNanoSeconds();
        const int64_t txEnd = record.phy + record.serialization;
        const int64_t rxStart = record.phy + record.delay;
        const auto overlaps = [this](int64_t start, int64_t end) {
            const auto outage = std::lower_bound(
                m_outages.begin(),
                m_outages.end(),
                start,
                [](const auto& interval, int64_t time) { return interval.second <= time; });
            return outage != m_outages.end() && outage->first < end;
        };
        const bool corrupt = overlaps(record.phy, txEnd) || overlaps(rxStart, record.rx);
        if (corrupt)
        {
            record.status = "rx_outage_drop";
        }
        return corrupt;
    }

    /**
     * Reconcile UDP delivery against the recorded packet identity and sequence.
     * @param socket Receiving UDP socket.
     */
    void Receive(Ptr<Socket> socket)
    {
        while (auto packet = socket->Recv())
        {
            auto& record = m_records.at(m_packetRecords.at(packet->GetUid()));
            SeqTsHeader header;
            if (packet->GetSize() != m_options.packetSize || packet->RemoveHeader(header) != 12 ||
                header.GetSeq() != record.sequence)
            {
                throw std::runtime_error("unexpected UDP payload identity");
            }
            record.status = record.rx < m_end ? "received" : "received_after_window";
        }
    }

    /** Write exactly one row per source opportunity in source time/direction order. */
    void Write() const
    {
        std::ofstream output(m_options.output);
        output.exceptions(std::ios::failbit | std::ios::badbit);
        output << "direction,sequence,offered_time_ns,udp_tx_time_ns,phy_tx_time_ns,rx_time_ns,"
                  "status,payload_bytes\n";
        for (const auto& record : m_records)
        {
            output << record.direction << ',' << record.sequence << ',' << record.offered;
            for (const auto time : {record.udp, record.phy, record.rx})
            {
                output << ',';
                if (time >= 0)
                {
                    output << time;
                }
            }
            output << ',' << record.status << ',' << m_options.packetSize << '\n';
        }
        output.close();
    }

    const Options& m_options; ///< Validated inputs, alive for the whole run.
    Sample m_current;         ///< State used by new transmissions.
    int64_t m_end;            ///< Exclusive source and metric window end.
    std::array<Ptr<PointToPointNetDevice>, 2> m_devices;  ///< Actual full-duplex devices.
    Ptr<PointToPointChannel> m_channel;                   ///< Shared propagation channel.
    std::array<Ptr<Socket>, 2> m_sources;                 ///< UDP source sockets.
    std::array<Ptr<Socket>, 2> m_receivers;               ///< UDP receiver sockets.
    std::vector<Record> m_records;                        ///< Ordered output records.
    std::unordered_map<uint64_t, size_t> m_packetRecords; ///< ns-3 UID to offered record.
    std::vector<std::pair<int64_t, int64_t>> m_outages;   ///< Declared half-open fade intervals.
    std::string m_dropStatus = "queue_drop";              ///< Context for real FIFO drops.
};

} // namespace

/**
 * Validate ns-3 version and run one bounded replay.
 * @param argc Argument count.
 * @param argv Argument vector.
 * @return Zero on success, one on rejected input or runtime failure.
 */
int
main(int argc, char** argv)
{
    try
    {
        if (openleo::PrintVersion(argc, argv, "openleo-ns3-replay/1"))
        {
            return 0;
        }
        const auto options = ReadOptions(argc, argv);
        Replay(options).Run();
        return 0;
    }
    catch (const std::exception& error)
    {
        std::cerr << "openleo ns-3 replay: " << error.what() << '\n';
        Simulator::Destroy();
        return 1;
    }
}
