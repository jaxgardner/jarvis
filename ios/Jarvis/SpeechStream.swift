import Foundation

/// Reads `audio/x-jarvis-chunked-wav` off the wire.
///
/// The body is a four-byte big-endian length followed by that many bytes of a
/// complete WAV, repeated. Framing rather than one long PCM stream because the
/// phone has to be able to tell audio from anything else: an unframed stream
/// would render a stray error body as a burst of noise, where a chunk that
/// will not parse simply takes the Apple-voice path like every other failure.
///
/// A delegate rather than `URLSession.bytes`, for two reasons. `bytes` hands
/// over one `UInt8` at a time, and a reply is a couple of hundred kilobytes.
/// And a delegate sees the response headers before the body, which is what
/// lets a 503 become a thrown error instead of a chunk that fails to parse.
final class ChunkedAudioClient: NSObject {
    /// One in-flight request. Only ever touched on `queue`, which is serial,
    /// so none of this needs a lock.
    private final class Transfer {
        let continuation: AsyncThrowingStream<Data, Error>.Continuation
        var buffer = Data()

        init(_ continuation: AsyncThrowingStream<Data, Error>.Continuation) {
            self.continuation = continuation
        }
    }

    /// A chunk larger than this is a framing error, not a long reply — the
    /// server caps `/speech` text at 4000 characters, which is minutes of
    /// speech but nothing like this many bytes in one piece. Without the
    /// ceiling a corrupt length would have us buffer until the app died.
    private static let maximumChunk = 8 * 1024 * 1024

    private let queue: OperationQueue
    private var session: URLSession!
    private var transfers: [Int: Transfer] = [:]

    override init() {
        queue = OperationQueue()
        queue.maxConcurrentOperationCount = 1
        queue.name = "jarvis.speech-stream"
        super.init()

        let configuration = URLSessionConfiguration.default
        configuration.waitsForConnectivity = false
        // One session for the life of the app, so a reply does not pay a fresh
        // TCP handshake to the Mini every time.
        session = URLSession(
            configuration: configuration, delegate: self, delegateQueue: queue
        )
    }

    func chunks(for request: URLRequest) -> AsyncThrowingStream<Data, Error> {
        AsyncThrowingStream { continuation in
            let task = session.dataTask(with: request)
            let transfer = Transfer(continuation)
            queue.addOperation { [weak self] in
                self?.transfers[task.taskIdentifier] = transfer
            }
            continuation.onTermination = { _ in task.cancel() }
            task.resume()
        }
    }
}

extension ChunkedAudioClient: URLSessionDataDelegate {
    func urlSession(
        _ session: URLSession,
        dataTask: URLSessionDataTask,
        didReceive response: URLResponse,
        completionHandler: @escaping (URLSession.ResponseDisposition) -> Void
    ) {
        let status = (response as? HTTPURLResponse)?.statusCode ?? 0
        guard (200..<300).contains(status) else {
            // 503 is the normal answer from a Mini with no weights, and the
            // signal the Speaker turns into "use the Apple voice".
            finish(dataTask, throwing: APIError.server(status, "no voice"))
            completionHandler(.cancel)
            return
        }
        completionHandler(.allow)
    }

    func urlSession(
        _ session: URLSession, dataTask: URLSessionDataTask, didReceive data: Data
    ) {
        guard let transfer = transfers[dataTask.taskIdentifier] else { return }
        transfer.buffer.append(data)

        while transfer.buffer.count >= 4 {
            let size = Int(
                UInt32(transfer.buffer[0]) << 24 | UInt32(transfer.buffer[1]) << 16
                    | UInt32(transfer.buffer[2]) << 8 | UInt32(transfer.buffer[3])
            )
            guard size > 0, size <= Self.maximumChunk else {
                finish(dataTask, throwing: APIError.server(200, "bad audio framing"))
                dataTask.cancel()
                return
            }
            guard transfer.buffer.count >= 4 + size else { return }

            let chunk = transfer.buffer.subdata(in: 4..<(4 + size))
            transfer.buffer.removeSubrange(0..<(4 + size))
            transfer.continuation.yield(chunk)
        }
    }

    func urlSession(
        _ session: URLSession, task: URLSessionTask, didCompleteWithError error: Error?
    ) {
        guard let transfer = transfers.removeValue(forKey: task.taskIdentifier) else {
            return
        }
        if let error {
            transfer.continuation.finish(throwing: APIError.transport(error.localizedDescription))
        } else if !transfer.buffer.isEmpty {
            // A partial frame at the end means the reply was cut off. Say so
            // rather than dropping it quietly — the Speaker has already played
            // what came before and needs to know it is not getting the rest.
            transfer.continuation.finish(throwing: APIError.server(200, "truncated audio"))
        } else {
            transfer.continuation.finish()
        }
    }

    private func finish(_ task: URLSessionTask, throwing error: Error) {
        transfers.removeValue(forKey: task.taskIdentifier)?
            .continuation.finish(throwing: error)
    }
}
