// BtedgeBluetooth: scans with CoreBluetooth and feeds the btedge process it starts.
//
//   BtedgeBluetooth <program> [args...]
//
// macOS grants Bluetooth only to a bundle declaring it, which the system Python does not,
// so this signed app is what launchd runs. It starts btedge as a child and writes one JSON
// object per line to its stdin: {"state": ...} or one advertisement (see corebluetooth.py).
import CoreBluetooth
import Foundation

let reportInterval: TimeInterval = 5  // per device; adverts arrive many times a second
let maxTracked = 4096

final class Scanner: NSObject, CBCentralManagerDelegate {
    private let output: FileHandle
    private let queue = DispatchQueue(label: "btedge.bluetooth")
    private var central: CBCentralManager!
    private var lastReport: [UUID: Date] = [:]

    init(output: FileHandle) {
        self.output = output
        super.init()
        central = CBCentralManager(delegate: self, queue: queue)
    }

    private func emit(_ object: [String: Any]) {
        guard let data = try? JSONSerialization.data(withJSONObject: object) else { return }
        try? output.write(contentsOf: data + Data([0x0A]))
    }

    func centralManagerDidUpdateState(_ central: CBCentralManager) {
        let state: String
        switch central.state {
        case .poweredOn: state = "poweredOn"
        case .poweredOff: state = "poweredOff"
        case .unauthorized: state = "unauthorized"
        case .unsupported: state = "unsupported"
        case .resetting: state = "resetting"
        default: state = "unknown"
        }
        emit(["state": state])
        if central.state == .poweredOn {
            central.scanForPeripherals(withServices: nil, options: [CBCentralManagerScanOptionAllowDuplicatesKey: true])
        }
    }

    func centralManager(_ central: CBCentralManager, didDiscover peripheral: CBPeripheral,
                        advertisementData: [String: Any], rssi RSSI: NSNumber) {
        let now = Date()
        if let last = lastReport[peripheral.identifier], now.timeIntervalSince(last) < reportInterval { return }
        if lastReport.count >= maxTracked { lastReport.removeAll() }  // random addresses never come back
        lastReport[peripheral.identifier] = now

        var message: [String: Any] = ["id": peripheral.identifier.uuidString]
        if let name = advertisementData[CBAdvertisementDataLocalNameKey] as? String ?? peripheral.name {
            message["name"] = name
        }
        if RSSI.intValue != 127 { message["rssi"] = RSSI.intValue }  // 127: not available
        if let power = advertisementData[CBAdvertisementDataTxPowerLevelKey] as? NSNumber {
            message["tx_power"] = power.intValue
        }
        if let data = advertisementData[CBAdvertisementDataManufacturerDataKey] as? Data, data.count >= 2 {
            message["manufacturer"] = Int(data[data.startIndex]) | Int(data[data.startIndex + 1]) << 8
        }
        if let uuids = advertisementData[CBAdvertisementDataServiceUUIDsKey] as? [CBUUID] {
            message["uuids"] = uuids.map { $0.uuidString }
        }
        if let serviceData = advertisementData[CBAdvertisementDataServiceDataKey] as? [CBUUID: Data] {
            var hex: [String: String] = [:]
            for (uuid, value) in serviceData {
                hex[uuid.uuidString] = value.map { String(format: "%02x", $0) }.joined()
            }
            message["service_data"] = hex
        }
        emit(message)
    }
}

let arguments = Array(CommandLine.arguments.dropFirst())
guard let program = arguments.first else {
    FileHandle.standardError.write(Data("usage: BtedgeBluetooth <program> [args...]\n".utf8))
    exit(64)
}

let pipe = Pipe()
let child = Process()
child.executableURL = URL(fileURLWithPath: program)
child.arguments = Array(arguments.dropFirst())
child.standardInput = pipe.fileHandleForReading
child.terminationHandler = { exit($0.terminationStatus) }
do {
    try child.run()
} catch {
    FileHandle.standardError.write(Data("cannot start \(program): \(error)\n".utf8))
    exit(1)
}

signal(SIGPIPE, SIG_IGN)
signal(SIGTERM, SIG_IGN)
let terminate = DispatchSource.makeSignalSource(signal: SIGTERM, queue: .main)
terminate.setEventHandler { child.terminate() }
terminate.resume()

let scanner = Scanner(output: pipe.fileHandleForWriting)
RunLoop.main.run()
