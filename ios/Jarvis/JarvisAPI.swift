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

    /// Per-source sync state. `lastRunAt` and `lastOkAt` are separate on
    /// purpose and must stay separate in the UI: equal means healthy, a gap
    /// means running-and-failing, both old means not running at all. Those are
    /// three different fixes, so they can't collapse into one green dot.
    struct IngestSource: Decodable, Identifiable {
        let source: String
        let lastRunAt: String?
        let lastOkAt: String?
        let detail: String?
        let stale: Bool

        var id: String { source }

        enum Condition {
            case healthy
            case runningAndFailing
            case notRunning
            case neverRun
        }

        var condition: Condition {
            guard lastRunAt != nil else { return .neverRun }
            if !stale { return .healthy }
            guard lastOkAt != nil else { return .neverRun }
            // Stale, but something ran recently — it is running and failing.
            return lastRunAt == lastOkAt ? .notRunning : .runningAndFailing
        }
    }

    struct Ingest: Decodable {
        let sources: [IngestSource]
        let stale: [String]
    }

    let status: String
    let db: Database
    let configured: [String: Bool]
    /// Absent on a server older than migration 006.
    let ingest: Ingest?
}

/// The email review queue. The one path from mail to the calendar, and it
/// requires a human at it.
struct ProposalsResponse: Decodable {
    struct Proposal: Decodable, Identifiable {
        let id: Int
        let source: String
        let kind: String
        let summary: String?
        let confidence: Double
        let title: String
        let location: String?
        /// Rendered by the server, like `/agenda`'s. Never re-derived here.
        let when: String
    }

    let proposals: [Proposal]
}

struct PantryResponse: Decodable {
    struct Item: Decodable, Identifiable {
        let id: Int
        let name: String
        let rawText: String?
        let category: String?
        let quantity: Double?
        let unit: String?
        let location: String
        let expiresOn: String?
        /// "default" when the shelf-life table proposed it, "user" once you
        /// have touched it. The fridge list uses this to show which dates you
        /// actually stand behind.
        let expirySource: String?
        let daysLeft: Int?
    }

    struct ListEntry: Decodable, Identifiable {
        let id: Int
        let name: String
        let reason: String?
    }

    let items: [Item]
    let shoppingList: [ListEntry]
}

/// Three things a day, and the days behind them.
///
/// `today` is separate from `days` rather than being its first element: the
/// card and the history render differently, and the view should not have to
/// work out which group is now.
struct GratitudeResponse: Decodable {
    struct Entry: Decodable, Identifiable {
        let id: Int
        let body: String
        let at: String
    }

    struct Today: Decodable {
        let on: String
        let target: Int
        let entries: [Entry]
    }

    struct Day: Decodable, Identifiable {
        let on: String
        let entries: [Entry]
        var id: String { on }
    }

    let today: Today
    let streak: Int
    let days: [Day]
}

struct ProjectsResponse: Decodable {
    struct Project: Decodable, Identifiable {
        let id: Int
        let name: String
        let description: String?
        let status: String
        let noteCount: Int
        let reportCount: Int
        let linkCount: Int
        let lastActivityAt: String?
    }
    let projects: [Project]
}

/// One project and everything filed under it.
///
/// There is no status paragraph here because the server does not store one:
/// the sections below ARE the status, newest thinking first. Asking "where am
/// I" out loud is what generates prose, and it does it from these same rows.
struct ProjectDetail: Decodable {
    /// Every `when` here is rendered by the server. Do NOT re-format dates
    /// client-side — the same rule `AgendaResponse` states, for the same
    /// reason: two implementations of "tomorrow at 3 PM" will drift.
    struct Note: Decodable, Identifiable {
        let id: Int
        let body: String
        let when: String
    }

    struct Report: Decodable, Identifiable {
        let id: Int
        let prompt: String
        let status: String
        let summary: String?
        let error: String?
    }

    struct Event: Decodable, Identifiable {
        let id: Int
        let title: String
        let location: String?
        let when: String
    }

    struct Reminder: Decodable, Identifiable {
        let id: Int
        let body: String
        let status: String
        let when: String
    }

    struct Link: Decodable, Identifiable {
        let id: Int
        let url: String
        let title: String?
    }

    struct File: Decodable, Identifiable {
        var id: String { name }
        let name: String
        let bytes: Int
        let when: String
    }

    let id: Int
    let name: String
    let description: String?
    let status: String
    let notes: [Note]
    let reports: [Report]
    let events: [Event]
    let reminders: [Reminder]
    let links: [Link]
    let files: [File]
}

struct ReceiptResponse: Decodable {
    let receiptId: Int
    let status: String
}

struct ReceiptDetail: Decodable {
    let id: Int
    let status: String
    /// "photo" or "manual". Not inferable from a missing image: pruning clears
    /// the path of a real photographed receipt thirty days after it is
    /// confirmed.
    let source: String?
    let store: String?
    let purchasedOn: String?
    let totalCents: Int?
    let extractError: String?
    let items: [PantryResponse.Item]
}

/// Ingested mail — metadata and Google's own snippet. Bodies are never stored,
/// which is a property of the architecture rather than a gap to paper over.
struct InboxResponse: Decodable {
    struct Message: Decodable, Identifiable {
        let sender: String?
        let subject: String?
        let snippet: String?
        let receivedAt: String?
        let isUnread: Int?

        /// No id column comes back from `/inbox`, and there is nothing to
        /// address a message by anyway — the list is read-only.
        var id: String { "\(receivedAt ?? "")|\(sender ?? "")|\(subject ?? "")" }
        var unread: Bool { (isUnread ?? 0) != 0 }
    }

    let messages: [Message]
}

struct DevicesResponse: Decodable {
    struct Device: Decodable, Identifiable {
        let id: Int
        let label: String
        let platform: String?
        let apnsEnv: String?
        let createdAt: String?
        let lastSeenAt: String?
        let revokedAt: String?
        let hasPush: Int?

        var isRevoked: Bool { revokedAt != nil }
        var canPush: Bool { (hasPush ?? 0) != 0 }
    }

    let devices: [Device]
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
            return "\(Failure.unreachablePrefix)\(message)"
        }
    }
}

/// Shared vocabulary for the one error that matters most.
///
/// The prefix is defined once and stripped once. A screen whose *headline* is
/// already "Can't reach the Mini" would otherwise print it twice — the message
/// carries the prefix because it also has to stand alone in a one-line banner.
enum Failure {
    static let unreachablePrefix = "Can't reach the Mini: "

    /// The reason without the preamble, for a view that has already said it.
    static func reason(_ message: String) -> String {
        message.hasPrefix(unreachablePrefix)
            ? String(message.dropFirst(unreachablePrefix.count))
            : message
    }

    /// A 404 on a screen's own endpoint means the Mini is reachable and
    /// running a build that predates this screen — not that the network is
    /// down. Worth telling apart, because the remedy is the opposite one:
    /// restart the daemon rather than check Tailscale.
    ///
    /// This is what a new screen looks like against an un-restarted daemon,
    /// and it will happen again every time an endpoint ships ahead of a
    /// deploy.
    static func isMissingEndpoint(_ error: Error) -> Bool {
        if case APIError.server(404, _) = error { return true }
        return false
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

    /// The device's token was rejected. App-level rather than per-screen,
    /// because nothing works until it is re-enrolled and showing the same
    /// failure on six tabs is just noise.
    @Published private(set) var isUnauthorized = false

    /// Last request reached the Mini. Drives the status glyph — a quiet,
    /// always-on answer to "is Tailscale up", which is what this error almost
    /// always turns out to be.
    @Published private(set) var isReachable = true

    private let session: URLSession
    private let decoder: JSONDecoder

    /// Its own session and delegate, because a streamed body has to be read as
    /// it arrives rather than awaited whole. Held for the life of the app so
    /// replies reuse one connection to the Mini.
    private let audioStream = ChunkedAudioClient()

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
        isUnauthorized = false
    }

    func forget() {
        Keychain.remove(Self.tokenAccount)
        isEnrolled = false
        isUnauthorized = false
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

    // MARK: - Projects

    func projects() async throws -> ProjectsResponse {
        try await send("/projects")
    }

    func project(_ id: Int) async throws -> ProjectDetail {
        try await send("/projects/\(id)?tz=\(TimeZone.current.identifier)")
    }

    /// You cannot speak a URL, so this is the one thing about a project that
    /// is typed rather than said.
    @discardableResult
    func addLink(project id: Int, url: String, title: String?) async throws -> ProjectDetail {
        var body: [String: Any] = ["url": url]
        if let title, !title.isEmpty { body["title"] = title }
        return try await send("/projects/\(id)/links", method: "POST", body: body)
    }

    /// Marking a project done takes it out of the router's PROJECTS block, so
    /// it stops being nameable by voice.
    @discardableResult
    func setStatus(project id: Int, status: String) async throws -> ProjectDetail {
        try await send("/projects/\(id)", method: "PATCH", body: ["status": status])
    }

    func projectFile(project id: Int, name: String) async throws -> String {
        struct Artifact: Decodable { let name: String; let text: String }
        let escaped = name.addingPercentEncoding(withAllowedCharacters: .urlPathAllowed) ?? name
        let artifact: Artifact = try await send("/projects/\(id)/files/\(escaped)")
        return artifact.text
    }

    /// Answer a report that asked you something. The job resumes its session
    /// and rewrites its report; the returned detail is the queued job.
    @discardableResult
    func reply(toJob id: Int, text: String) async throws -> JobDetail {
        try await send("/jobs/\(id)/reply", method: "POST", body: ["text": text])
    }

    func health() async throws -> HealthResponse {
        try await send("/health")
    }

    func metrics(days: Int = 1) async throws -> MetricsResponse {
        try await send("/metrics?days=\(days)")
    }

    // MARK: - The turn

    /// The body `/turns` expects.
    ///
    /// Split out and non-private so `ContractTests` can pin the key names
    /// without a server. Nothing else here is written this way because
    /// nothing else needs it — but this is the only request whose keys the
    /// server reads into columns, and a typo would fail silently as an
    /// utterance that simply never reported a turn.
    /// `nonisolated` because it reads nothing — the class is `@MainActor` and
    /// this is pure argument shuffling, which the test calls off the actor.
    nonisolated static func turnBody(utteranceId: Int, turnMs: Int) -> [String: Any] {
        ["utterance_id": utteranceId, "turn_ms": turnMs]
    }

    /// What the turn cost, measured where it is actually felt.
    ///
    /// The server's `latency_ms` times /say and cannot see the endpointer
    /// before it or the synthesis after it — together about 1550ms of a
    /// 3000ms turn.
    ///
    /// Fire-and-forget: sent once playback has started, so it cannot sit on
    /// the critical path it is measuring, and every failure is swallowed. A
    /// dropped measurement is not worth surfacing to someone who has already
    /// been answered — and `isReachable` is deliberately left alone, since a
    /// metrics write is not evidence about the connection the user cares
    /// about.
    func reportTurn(utteranceId: Int, turnMs: Int) async {
        guard
            let request = try? request(
                "/turns",
                method: "POST",
                body: Self.turnBody(utteranceId: utteranceId, turnMs: turnMs)
            )
        else { return }
        _ = try? await session.data(for: request)
    }

    // MARK: - Review queue and context

    func proposals(limit: Int = 50) async throws -> ProposalsResponse {
        try await send("/proposals?limit=\(limit)&tz=\(TimeZone.current.identifier)")
    }

    /// Writes a real calendar event — and, because a human pressed the button,
    /// goes through the mutations log, so it is undoable like anything else.
    @discardableResult
    func acceptProposal(_ id: Int) async throws -> String {
        struct Decision: Decodable { let reply: String }
        let response: Decision = try await send(
            "/proposals/\(id)/accept?tz=\(TimeZone.current.identifier)",
            method: "POST",
            body: [:]
        )
        return response.reply
    }

    /// Permanent. That message will never be proposed again.
    @discardableResult
    func rejectProposal(_ id: Int) async throws -> String {
        struct Decision: Decodable { let reply: String }
        let response: Decision = try await send(
            "/proposals/\(id)/reject", method: "POST", body: [:]
        )
        return response.reply
    }

    func inbox(limit: Int = 25, unreadOnly: Bool = false, query: String = "")
        async throws -> InboxResponse
    {
        var path = "/inbox?limit=\(limit)"
        if unreadOnly { path += "&unread_only=true" }
        let trimmed = query.trimmingCharacters(in: .whitespacesAndNewlines)
        if !trimmed.isEmpty {
            let escaped = trimmed.addingPercentEncoding(
                withAllowedCharacters: .urlQueryAllowed
            ) ?? trimmed
            path += "&q=\(escaped)"
        }
        return try await send(path)
    }

    func devices() async throws -> DevicesResponse {
        try await send("/devices")
    }

    /// The point of per-device tokens: a lost phone costs one request rather
    /// than re-keying every client.
    func revokeDevice(_ id: Int) async throws {
        struct Revoked: Decodable { let revoked: Bool }
        let _: Revoked = try await send("/devices/\(id)", method: "DELETE")
    }

    // MARK: - Pantry

    /// Upload a receipt photo.
    ///
    /// Multipart rather than JSON — this is the only binary payload the app
    /// sends, so it does not go through `send`, which builds JSON bodies.
    /// Returns as soon as the server has a receipt id; extraction runs behind
    /// it and the caller polls `receipt(_:)`.
    func uploadReceipt(_ jpeg: Data) async throws -> ReceiptResponse {
        guard !host.isEmpty, let credential = deviceToken, !credential.isEmpty else {
            throw APIError.notConfigured
        }
        let authority = host.contains(":") ? host : "\(host):8000"
        guard let url = URL(string: "http://\(authority)/receipts") else {
            throw APIError.notConfigured
        }

        let boundary = "jarvis.\(UUID().uuidString)"
        var body = Data()
        body.append("--\(boundary)\r\n".data(using: .utf8)!)
        body.append(
            "Content-Disposition: form-data; name=\"image\"; filename=\"receipt.jpg\"\r\n"
                .data(using: .utf8)!
        )
        body.append("Content-Type: image/jpeg\r\n\r\n".data(using: .utf8)!)
        body.append(jpeg)
        body.append("\r\n--\(boundary)--\r\n".data(using: .utf8)!)

        var request = URLRequest(url: url)
        request.httpMethod = "POST"
        request.setValue("Bearer \(credential)", forHTTPHeaderField: "Authorization")
        request.setValue(
            "multipart/form-data; boundary=\(boundary)", forHTTPHeaderField: "Content-Type"
        )
        request.httpBody = body
        // A photo over a home uplink deserves longer than the default. The
        // server answers in milliseconds; the upload is the slow part.
        request.timeoutInterval = 60

        let data: Data
        let response: URLResponse
        do {
            (data, response) = try await session.data(for: request)
            isReachable = true
        } catch {
            isReachable = false
            throw APIError.transport(error.localizedDescription)
        }

        let status = (response as? HTTPURLResponse)?.statusCode ?? 0
        if status == 401 {
            isUnauthorized = true
            throw APIError.unauthorized
        }
        guard (200..<300).contains(status) else {
            let detail = (try? decoder.decode(ErrorDetail.self, from: data))?.detail ?? ""
            throw APIError.server(status, detail)
        }
        return try decoder.decode(ReceiptResponse.self, from: data)
    }

    /// Audio for a reply, in the Mini's local voice, streamed in clauses.
    ///
    /// Unlike `send`, this touches neither `isReachable` nor `isUnauthorized`.
    /// A voice that failed is not an enrollment problem and must not put the
    /// app into a re-enrol state over it.
    private func speechRequest(for text: String) throws -> URLRequest {
        guard !host.isEmpty, let credential = deviceToken, !credential.isEmpty else {
            throw APIError.notConfigured
        }
        let authority = host.contains(":") ? host : "\(host):8000"
        guard let url = URL(string: "http://\(authority)/speech") else {
            throw APIError.notConfigured
        }

        var request = URLRequest(url: url)
        request.httpMethod = "POST"
        request.setValue("Bearer \(credential)", forHTTPHeaderField: "Authorization")
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.httpBody = try JSONSerialization.data(withJSONObject: ["text": text])
        request.timeoutInterval = Self.speechTimeout(for: text)
        return request
    }

    /// How long to wait, scaled to how much there is to say.
    ///
    /// This was a flat three seconds, chosen when `/speech` returned one WAV
    /// and synthesis was assumed to be quick. Measured, a single sentence
    /// takes about a second — so a three-sentence answer could cross the
    /// deadline and drop to the Apple voice, silently, which is exactly the
    /// failure the unconditional fallback makes invisible.
    ///
    /// On a streamed response `timeoutInterval` is the gap *between* packets
    /// rather than the total, so this is the budget for one chunk. Generous is
    /// cheap: the race against `AVSpeechSynthesizer` is decided by the first
    /// chunk, which now arrives in about 300ms.
    nonisolated static func speechTimeout(for text: String) -> TimeInterval {
        min(15, max(4, 2 + Double(text.count) / 40))
    }

    /// Type a list of food instead of photographing a receipt.
    ///
    /// Lands in the same `pending` review screen a photograph does — the
    /// dates are still being proposed by the shelf-life table, and that
    /// screen is where you agree to them.
    func createManualReceipt(_ names: [String]) async throws -> ReceiptResponse {
        struct Created: Decodable {
            let receiptId: Int
            let status: String
            let items: Int
        }
        let created: Created = try await send(
            "/receipts/manual",
            method: "POST",
            body: ["items": names.map { ["name": $0] }]
        )
        return ReceiptResponse(receiptId: created.receiptId, status: created.status)
    }

    func receipt(_ id: Int) async throws -> ReceiptDetail {
        try await send("/receipts/\(id)")
    }

    func patchReceiptItems(_ id: Int, edits: [[String: Any]]) async throws {
        struct Updated: Decodable { let updated: Int }
        let _: Updated = try await send(
            "/receipts/\(id)/items", method: "PATCH", body: ["items": edits]
        )
    }

    func confirmReceipt(_ id: Int) async throws -> String {
        struct Confirmed: Decodable { let items: Int; let reply: String }
        let done: Confirmed = try await send("/receipts/\(id)/confirm", method: "POST")
        return done.reply
    }

    func discardReceipt(_ id: Int) async throws {
        struct Discarded: Decodable { let discarded: Bool }
        let _: Discarded = try await send("/receipts/\(id)/discard", method: "POST")
    }

    func pantry() async throws -> PantryResponse {
        try await send("/pantry")
    }

    func gratitude(days: Int = 30) async throws -> GratitudeResponse {
        try await send("/gratitude?days=\(days)")
    }

    func addShoppingEntry(_ name: String) async throws {
        struct Added: Decodable { let added: Bool }
        let _: Added = try await send(
            "/shopping-list", method: "POST", body: ["name": name]
        )
    }

    func resolveShoppingEntry(_ id: Int) async throws {
        struct Resolved: Decodable { let resolved: Bool }
        let _: Resolved = try await send("/shopping-list/\(id)", method: "DELETE")
    }

    // MARK: - Transport

    /// One authenticated request, built and not yet sent.
    ///
    /// Split out of `send` for `reportTurn`, which answers 204 — there is no
    /// body to decode, and `send` would turn an empty response into
    /// "unreadable response". Sharing the builder is what keeps the host,
    /// the port default and the bearer token identical on both paths.
    private func request(
        _ path: String,
        method: String = "GET",
        body: [String: Any]? = nil,
        token overrideToken: String? = nil
    ) throws -> URLRequest {
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
        return request
    }

    private func send<T: Decodable>(
        _ path: String,
        method: String = "GET",
        body: [String: Any]? = nil,
        token overrideToken: String? = nil
    ) async throws -> T {
        let request = try self.request(
            path, method: method, body: body, token: overrideToken
        )

        let data: Data
        let response: URLResponse
        do {
            (data, response) = try await session.data(for: request)
            isReachable = true
        } catch {
            isReachable = false
            throw APIError.transport(error.localizedDescription)
        }

        let status = (response as? HTTPURLResponse)?.statusCode ?? 0
        if status == 401 {
            // Not raised during enrollment: a bad shared token there is a typo
            // to correct on the form, not a device to re-enrol.
            if overrideToken == nil { isUnauthorized = true }
            throw APIError.unauthorized
        }
        isUnauthorized = false
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

extension JarvisAPI: SpeechSource {
    func audioChunks(for text: String) -> AsyncThrowingStream<Data, Error> {
        guard let request = try? speechRequest(for: text) else {
            return AsyncThrowingStream { $0.finish(throwing: APIError.notConfigured) }
        }
        return audioStream.chunks(for: request)
    }
}
