import Foundation

/// The server contract, as consumed by the app.
///
/// `reply` is always a single plain-text string safe to hand straight to a TTS
/// engine — no markdown, no lists, no emoji. That is a server-side guarantee;
/// nothing here reformats it.
struct SayResponse: Decodable {
    /// The row this utterance touched, when it touched one. Absent for
    /// questions, which change nothing.
    struct Change: Decodable {
        let table: String
        let rowId: Int
        let op: String
    }

    let reply: String
    let route: String
    let utteranceId: Int
    let jobId: Int?
    let latencyMs: Int?
    let changed: Change?

    /// The reminder this utterance created, if it created one — what the Siri
    /// snippet's Snooze button acts on.
    var reminderID: Int? {
        guard let changed, changed.table == "reminders", changed.op == "insert" else {
            return nil
        }
        return changed.rowId
    }
}

struct AgendaResponse: Decodable {
    struct Event: Decodable, Identifiable {
        let id: Int
        let title: String
        let location: String?
        /// Rendered by the server. Do NOT re-format dates client-side — the
        /// server already speaks them ("tomorrow at 3 PM") and two
        /// implementations of that will drift.
        let when: String
    }

    struct Reminder: Decodable, Identifiable {
        let id: Int
        let body: String
        let status: String
        let when: String
    }

    let events: [Event]
    let reminders: [Reminder]
}

/// What you said, and what it changed. The mutations log has always known
/// this; the Activity screen is the first thing to show it.
struct ActivityResponse: Decodable {
    struct Change: Decodable, Identifiable {
        let id: Int
        let table: String
        let rowId: Int
        let op: String
        let undoneAt: String?
        /// True on exactly one row at a time — /undo reverses the most recent
        /// non-undone mutation and nothing else.
        let undoable: Bool
    }

    struct Utterance: Decodable, Identifiable {
        let id: Int
        let rawText: String
        let responseText: String?
        let route: String?
        let intent: String?
        let latencyMs: Int?
        let inputTokens: Int?
        let outputTokens: Int?
        let modelCalls: Int?
        let createdAt: String
        let mutations: [Change]

        var isUndoable: Bool { mutations.contains(where: \.undoable) }
        var wasUndone: Bool {
            !mutations.isEmpty && mutations.allSatisfy { $0.undoneAt != nil }
        }
    }

    let utterances: [Utterance]
}

struct JobsResponse: Decodable {
    struct Job: Decodable, Identifiable {
        let id: Int
        let prompt: String
        let status: String
        let error: String?
        let attempts: Int
        let createdAt: String
        let finishedAt: String?
        let resultPreview: String?
        let resultTruncated: Int?
    }

    let jobs: [Job]
}

/// The full job, including the untruncated result the list view omits.
struct JobDetail: Decodable {
    let id: Int
    let prompt: String
    let status: String
    let result: String?
    let error: String?
    let attempts: Int
    let sessionId: String?
    let createdAt: String
    let startedAt: String?
    let finishedAt: String?
}

struct HealthResponse: Decodable {
    struct Database: Decodable {
        let ok: Bool
        let migrationsApplied: Int?
        let error: String?
    }

    let status: String
    let db: Database
    let configured: [String: Bool]
}

struct MetricsResponse: Decodable {
    struct Latency: Decodable {
        let count: Int
        let p50: Int?
        let p95: Int?
        let max: Int?
    }

    struct Spend: Decodable {
        let model: String
        let utterances: Int
        let modelCalls: Int
        let inputTokens: Int
        let outputTokens: Int
        let usd: Double
        let usdPerUtterance: Double
        let usdPerMonthAtThisRate: Double
    }

    let fast: Latency
    let deep: Latency
    let spend: Spend
}

struct DeviceResponse: Decodable {
    let deviceId: Int
    let label: String
    /// Present exactly once, at enrollment. A refresh returns null.
    let token: String?
}

/// FastAPI's error shape. File-scope rather than nested, because the decoder
/// that reads it lives in a generic function.
private struct ErrorDetail: Decodable {
    let detail: String?
}

enum APIError: LocalizedError {
    case notConfigured
    case unauthorized
    case server(Int, String)
    case transport(String)

    var errorDescription: String? {
        switch self {
        case .notConfigured:
            return "Jarvis isn't set up yet."
        case .unauthorized:
            return "This device isn't authorized. Re-enroll in Settings."
        case .server(let code, let detail):
            return detail.isEmpty ? "Server error \(code)." : detail
        case .transport(let message):
            // Almost always "Tailscale is off" in practice.
            return "Can't reach the Mini: \(message)"
        }
    }
}

/// Thin async wrapper over the API. One instance, shared.
@MainActor
final class JarvisAPI: ObservableObject {
    static let shared = JarvisAPI()

    /// The MagicDNS name, never a hardcoded IP. App Intents run in a separate
    /// extension process that has to reach the tailnet too, and a stale IP
    /// breaks the Siri path while the app itself keeps working — which is a
    /// miserable thing to debug.
    @Published var host: String {
        didSet { UserDefaults.standard.set(host, forKey: "jarvis.host") }
    }

    @Published private(set) var isEnrolled: Bool

    private let session: URLSession
    private let decoder: JSONDecoder

    private init() {
        host = UserDefaults.standard.string(forKey: "jarvis.host") ?? ""
        isEnrolled = Keychain.get(Self.tokenAccount) != nil

        let config = URLSessionConfiguration.default
        // The fast path budget is 2s end to end. Waiting 60s for a request
        // that is never coming just delays telling you Tailscale is off.
        config.timeoutIntervalForRequest = 15
        config.waitsForConnectivity = false
        session = URLSession(configuration: config)

        decoder = JSONDecoder()
        decoder.keyDecodingStrategy = .convertFromSnakeCase
    }

    static let tokenAccount = "device-token"

    var deviceToken: String? { Keychain.get(Self.tokenAccount) }

    // MARK: - Enrollment

    /// Trade the shared JARVIS_TOKEN for a token belonging to this device.
    ///
    /// Called once, from Setup. The shared token is not retained afterwards:
    /// the point of per-device tokens is that losing the phone costs one
    /// revocation, and that is undone if the enrollment credential is sitting
    /// on the phone too.
    func enroll(host: String, sharedToken: String, label: String) async throws {
        self.host = host.trimmingCharacters(in: .whitespacesAndNewlines)
        let body = ["label": label, "platform": "ios"]
        let response: DeviceResponse = try await send(
            "/devices", method: "POST", body: body, token: sharedToken
        )
        guard let token = response.token else { throw APIError.unauthorized }
        Keychain.set(token, for: Self.tokenAccount)
        isEnrolled = true
    }

    func forget() {
        Keychain.remove(Self.tokenAccount)
        isEnrolled = false
    }

    /// Hand the server this launch's APNs token. iOS re-issues these on
    /// reinstall and restore, and a stale one fails silently — so this runs on
    /// every launch, not just the first.
    func refreshPushToken(_ apnsToken: String, label: String) async throws {
        let body: [String: Any] = [
            "label": label,
            "platform": "ios",
            "apns_token": apnsToken,
            "apns_env": Self.apnsEnvironment,
        ]
        let _: DeviceResponse = try await send("/devices", method: "POST", body: body)
    }

    /// Debug builds get sandbox device tokens even though the entitlement says
    /// production. Reporting the wrong one is the classic "push works on
    /// TestFlight but not from Xcode" bug.
    static var apnsEnvironment: String {
        #if DEBUG
        return "sandbox"
        #else
        return "prod"
        #endif
    }

    // MARK: - Endpoints

    func say(_ text: String) async throws -> SayResponse {
        let body: [String: Any] = [
            "text": text,
            "client": "ios",
            "tz": TimeZone.current.identifier,
        ]
        return try await send("/say", method: "POST", body: body)
    }

    func agenda(days: Int = 1) async throws -> AgendaResponse {
        try await send("/agenda?days=\(days)&tz=\(TimeZone.current.identifier)")
    }

    @discardableResult
    func undo() async throws -> [String: String] {
        struct Undone: Decodable { let reply: String }
        let response: Undone = try await send("/undo", method: "POST", body: [:])
        return ["reply": response.reply]
    }

    func snooze(reminder id: Int, minutes: Int = 10) async throws {
        struct Ack: Decodable { let reply: String }
        let _: Ack = try await send(
            "/reminders/\(id)/snooze",
            method: "POST",
            body: ["minutes": minutes, "tz": TimeZone.current.identifier]
        )
    }

    func ack(reminder id: Int) async throws {
        struct Ack: Decodable { let reply: String }
        let _: Ack = try await send("/reminders/\(id)/ack", method: "POST", body: [:])
    }

    // MARK: - Dashboard (7d)

    func activity(limit: Int = 50) async throws -> ActivityResponse {
        try await send("/activity?limit=\(limit)")
    }

    func jobs(limit: Int = 50) async throws -> JobsResponse {
        try await send("/jobs?limit=\(limit)")
    }

    func job(_ id: Int) async throws -> JobDetail {
        try await send("/jobs/\(id)")
    }

    func health() async throws -> HealthResponse {
        try await send("/health")
    }

    func metrics(days: Int = 1) async throws -> MetricsResponse {
        try await send("/metrics?days=\(days)")
    }

    // MARK: - Transport

    private func send<T: Decodable>(
        _ path: String,
        method: String = "GET",
        body: [String: Any]? = nil,
        token overrideToken: String? = nil
    ) async throws -> T {
        let credential = overrideToken ?? deviceToken
        guard !host.isEmpty, let credential, !credential.isEmpty else {
            throw APIError.notConfigured
        }
        // A host may carry its own port ("mini.tailnet.ts.net:9000"); otherwise
        // assume the uvicorn default the launchd plist binds.
        let authority = host.contains(":") ? host : "\(host):8000"
        guard let url = URL(string: "http://\(authority)\(path)") else {
            throw APIError.notConfigured
        }

        var request = URLRequest(url: url)
        request.httpMethod = method
        request.setValue("Bearer \(credential)", forHTTPHeaderField: "Authorization")
        if let body {
            request.setValue("application/json", forHTTPHeaderField: "Content-Type")
            request.httpBody = try JSONSerialization.data(withJSONObject: body)
        }

        let data: Data
        let response: URLResponse
        do {
            (data, response) = try await session.data(for: request)
        } catch {
            throw APIError.transport(error.localizedDescription)
        }

        let status = (response as? HTTPURLResponse)?.statusCode ?? 0
        if status == 401 { throw APIError.unauthorized }
        guard (200..<300).contains(status) else {
            let detail = (try? decoder.decode(ErrorDetail.self, from: data))?.detail ?? ""
            throw APIError.server(status, detail)
        }

        do {
            return try decoder.decode(T.self, from: data)
        } catch {
            throw APIError.server(status, "unreadable response")
        }
    }
}
