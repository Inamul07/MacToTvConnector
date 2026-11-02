// Captures the Mac's system audio with a Core Audio process tap (macOS 14.2+)
// and writes it as a WAV stream (32-bit float) to the path given as the first
// argument, usually a named pipe that ffmpeg reads. The Mac's own speakers are
// muted while capturing. Capture starts only once the reader opens the pipe,
// so the audio lines up with the video ffmpeg started capturing just before.

import CoreAudio
import Foundation

func fail(_ what: String, _ status: OSStatus = 0) -> Never {
    FileHandle.standardError.write("audiotap: \(what) failed (\(status))\n".data(using: .utf8)!)
    exit(1)
}

guard CommandLine.arguments.count == 2 else {
    FileHandle.standardError.write("usage: audiotap <output path>\n".data(using: .utf8)!)
    exit(2)
}
let outputPath = CommandLine.arguments[1]

var tapID = AudioObjectID(kAudioObjectUnknown)
var aggregateID = AudioObjectID(kAudioObjectUnknown)
var procID: AudioDeviceIOProcID?

func cleanup(_ code: Int32 = 0) -> Never {
    if let procID {
        AudioDeviceStop(aggregateID, procID)
        AudioDeviceDestroyIOProcID(aggregateID, procID)
    }
    if aggregateID != kAudioObjectUnknown { AudioHardwareDestroyAggregateDevice(aggregateID) }
    if tapID != kAudioObjectUnknown { AudioHardwareDestroyProcessTap(tapID) }
    exit(code)
}

var signalSources: [DispatchSourceSignal] = []
for sig in [SIGINT, SIGTERM, SIGHUP] {
    signal(sig, SIG_IGN)
    let source = DispatchSource.makeSignalSource(signal: sig, queue: .main)
    source.setEventHandler { cleanup() }
    source.resume()
    signalSources.append(source)
}
signal(SIGPIPE, SIG_IGN)

/// Audio handed from the real-time IO thread to the writer thread.
final class AudioQueue {
    private var buffer = Data()
    private let lock = NSCondition()
    private let maxBytes: Int

    init(maxBytes: Int) { self.maxBytes = maxBytes }

    func push(_ bytes: UnsafeRawBufferPointer) {
        lock.lock()
        buffer.append(contentsOf: bytes)
        // If the reader falls behind, drop the oldest audio instead of drifting out of sync.
        if buffer.count > maxBytes { buffer.removeFirst(buffer.count - maxBytes) }
        lock.signal()
        lock.unlock()
    }

    /// Returns the queued audio, or nil if none arrived within the timeout.
    func pop(timeout: TimeInterval) -> Data? {
        lock.lock()
        defer { lock.unlock() }
        let deadline = Date(timeIntervalSinceNow: timeout)
        while buffer.isEmpty {
            if !lock.wait(until: deadline) { return nil }
        }
        let data = buffer
        buffer.removeAll(keepingCapacity: true)
        return data
    }
}

func wavHeader(sampleRate: UInt32, channels: UInt16) -> Data {
    var d = Data()
    func put<T: FixedWidthInteger>(_ v: T) { withUnsafeBytes(of: v.littleEndian) { d.append(contentsOf: $0) } }
    let blockAlign = channels * 4
    d.append("RIFF".data(using: .ascii)!); put(UInt32.max)
    d.append("WAVEfmt ".data(using: .ascii)!); put(UInt32(16))
    put(UInt16(3))  // IEEE float
    put(channels); put(sampleRate); put(sampleRate * UInt32(blockAlign)); put(blockAlign); put(UInt16(32))
    d.append("data".data(using: .ascii)!); put(UInt32.max)  // unknown length: live stream
    return d
}

Thread.detachNewThread {
    // Blocks until ffmpeg opens the pipe for reading.
    guard let output = FileHandle(forWritingAtPath: outputPath) else { fail("opening \(outputPath)") }

    let tapDescription = CATapDescription(stereoGlobalTapButExcludeProcesses: [])
    tapDescription.isPrivate = true
    tapDescription.muteBehavior = .mutedWhenTapped
    var status = AudioHardwareCreateProcessTap(tapDescription, &tapID)
    if status != noErr { fail("creating the audio tap", status) }

    var format = AudioStreamBasicDescription()
    var size = UInt32(MemoryLayout<AudioStreamBasicDescription>.size)
    var address = AudioObjectPropertyAddress(mSelector: kAudioTapPropertyFormat,
                                             mScope: kAudioObjectPropertyScopeGlobal,
                                             mElement: kAudioObjectPropertyElementMain)
    status = AudioObjectGetPropertyData(tapID, &address, 0, nil, &size, &format)
    if status != noErr { fail("reading the tap format", status) }
    let channels = Int(format.mChannelsPerFrame)

    let aggregateDescription: [String: Any] = [
        kAudioAggregateDeviceNameKey: "Mac Mirror audio",
        kAudioAggregateDeviceUIDKey: "com.macmirror.audio.\(UUID().uuidString)",
        kAudioAggregateDeviceIsPrivateKey: true,
        kAudioAggregateDeviceTapAutoStartKey: true,
        kAudioAggregateDeviceTapListKey: [[kAudioSubTapUIDKey: tapDescription.uuid.uuidString]],
    ]
    status = AudioHardwareCreateAggregateDevice(aggregateDescription as CFDictionary, &aggregateID)
    if status != noErr { fail("creating the aggregate device", status) }

    let bytesPerSecond = Int(format.mSampleRate) * channels * 4
    let queue = AudioQueue(maxBytes: bytesPerSecond / 2)
    var interleaved = [Float]()

    status = AudioDeviceCreateIOProcIDWithBlock(&procID, aggregateID, nil) { _, inData, _, _, _ in
        let buffers = UnsafeMutableAudioBufferListPointer(UnsafeMutablePointer(mutating: inData))
        if buffers.count == 1, let data = buffers[0].mData {
            queue.push(UnsafeRawBufferPointer(start: data, count: Int(buffers[0].mDataByteSize)))
        } else if buffers.count > 1 {
            // Non-interleaved: one buffer per channel.
            let frames = Int(buffers[0].mDataByteSize) / 4
            if interleaved.count != frames * buffers.count {
                interleaved = [Float](repeating: 0, count: frames * buffers.count)
            }
            for (c, buffer) in buffers.enumerated() {
                guard let p = buffer.mData?.assumingMemoryBound(to: Float.self) else { continue }
                for f in 0..<frames { interleaved[f * buffers.count + c] = p[f] }
            }
            interleaved.withUnsafeBytes { queue.push($0) }
        }
    }
    if status != noErr { fail("creating the IO callback", status) }

    do {
        try output.write(contentsOf: wavHeader(sampleRate: UInt32(format.mSampleRate),
                                               channels: UInt16(max(channels, 1))))
    } catch {
        DispatchQueue.main.async { cleanup() }
        return
    }

    status = AudioDeviceStart(aggregateID, procID)
    if status != noErr { fail("starting capture", status) }

    // macOS only delivers audio while something is playing. Fill the gaps with silence,
    // keeping the audio timeline locked to the clock: otherwise ffmpeg stalls waiting
    // for audio (freezing the video too), and sound drifts ahead of the picture.
    let sampleRate = format.mSampleRate
    let frameBytes = max(channels, 1) * 4
    let maxGapFrames = Int(sampleRate * 0.1)
    let started = Date()
    var framesWritten = 0
    while true {
        let audio = queue.pop(timeout: 0.05)
        let audioFrames = (audio?.count ?? 0) / frameBytes
        let expectedFrames = Int(Date().timeIntervalSince(started) * sampleRate)
        var data = Data()
        let missing = expectedFrames - framesWritten - audioFrames
        if missing > maxGapFrames {
            data.append(Data(count: missing * frameBytes))
            framesWritten += missing
        }
        if let audio {
            data.append(audio)
            framesWritten += audioFrames
        }
        if data.isEmpty { continue }
        do {
            try output.write(contentsOf: data)
        } catch {
            // Reader went away.
            DispatchQueue.main.async { cleanup() }
            return
        }
    }
}

dispatchMain()
