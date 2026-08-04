import Foundation
import Testing

@testable import Jarvis

/// The JSON contract between the Mini and the app.
///
/// This is the seam most likely to break silently: the server changes a column
/// name or starts returning null where it used to return a number, the app's
/// decoder throws, and the screen just renders empty with an error nobody
/// reads. Every fixture here is a real response captured from a running
/// server, not a hand-written approximation — a hand-written one only proves
/// the decoder matches what I imagined the server sends.
///
/// Re-capture the fixtures with:
///     curl -s localhost:8000/activity -H "Authorization: Bearer $JARVIS_TOKEN" \
///       > ios/JarvisTests/Fixtures/activity.json
struct ContractTests {
    static func fixture(_ name: String) throws -> Data {
        let url = try #require(
            Bundle(for: BundleMarker.self).url(forResource: name, withExtension: "json"),
            "fixture \(name).json is missing from the test bundle"
        )
        return try Data(contentsOf: url)
    }

    static func decode<T: Decodable>(_ type: T.Type, from name: String) throws -> T {
        let decoder = JSONDecoder()
        decoder.keyDecodingStrategy = .convertFromSnakeCase
        return try decoder.decode(type, from: try fixture(name))
    }

    // MARK: - Activity

    @Test func activityDecodes() throws {
        let response = try Self.decode(ActivityResponse.self, from: "activity")
        #expect(response.utterances.count == 2)

        let note = try #require(response.utterances.first { $0.intent == "add_note" })
        #expect(note.rawText == "remember the wifi password is hunter2")
        #expect(note.latencyMs == 612)
        #expect(note.inputTokens == 2557)
        #expect(note.mutations.count == 1)
        #expect(note.mutations[0].table == "notes")
        #expect(note.mutations[0].op == "insert")
    }

    @Test func questionsHaveNoMutationsAndNoTokensBeforeMigration004() throws {
        /// `query` changed nothing, and this row predates the token columns —
        /// both must decode as absent rather than throwing.
        let response = try Self.decode(ActivityResponse.self, from: "activity")
        let question = try #require(response.utterances.first { $0.intent == "query" })
        #expect(question.mutations.isEmpty)
        #expect(question.inputTokens == nil)
        #expect(question.isUndoable == false)
    }

    @Test func exactlyOneMutationIsUndoable() throws {
        /// The invariant the swipe gesture depends on: /undo reverses the most
        /// recent non-undone mutation and nothing else, so offering the swipe
        /// on two rows would make one of them a lie.
        let response = try Self.decode(ActivityResponse.self, from: "activity")
        let undoable = response.utterances.flatMap(\.mutations).filter(\.undoable)
        #expect(undoable.count == 1)
    }

    // MARK: - Jobs

    @Test func jobsDecode() throws {
        let response = try Self.decode(JobsResponse.self, from: "jobs")
        #expect(response.jobs.count == 2)
        #expect(response.jobs.first?.id == 2, "newest first")
    }

    @Test func aJobWithNoResultDecodesNullTruncationFlag() throws {
        /// SQLite evaluates `length(NULL) > 280` to NULL, not 0, so a failed
        /// job's `result_truncated` comes back null. Typing it non-optional
        /// would throw on every list containing a failure.
        let response = try Self.decode(JobsResponse.self, from: "jobs")
        let failed = try #require(response.jobs.first { $0.status == "failed" })
        #expect(failed.resultPreview == nil)
        #expect(failed.resultTruncated == nil)
        #expect(failed.error == "timeout")
        #expect(failed.attempts == 3)
    }

    @Test func aLongResultIsMarkedTruncated() throws {
        let response = try Self.decode(JobsResponse.self, from: "jobs")
        let done = try #require(response.jobs.first { $0.status == "done" })
        #expect(done.resultPreview?.count == 280)
        #expect(done.resultTruncated == 1)
    }

    // MARK: - Agenda

    @Test func agendaDecodesAndCarriesServerRenderedTimes() throws {
        /// The `when` string is the server's, and the app must not re-derive
        /// it — two implementations of "tomorrow at 3 PM" drift.
        let response = try Self.decode(AgendaResponse.self, from: "agenda")
        let event = try #require(response.events.first)
        #expect(event.title == "dentist")
        #expect(event.location == "Main St")
        #expect(event.when.contains("today at"))

        let reminder = try #require(response.reminders.first)
        #expect(reminder.status == "pending")
        #expect(!reminder.when.isEmpty)
    }

    // MARK: - Health and metrics

    @Test func healthDecodes() throws {
        let response = try Self.decode(HealthResponse.self, from: "health")
        #expect(response.status == "ok")
        #expect(response.db.ok)
        #expect(response.db.migrationsApplied == 4)
        #expect(response.configured["apns"] == true)
    }

    @Test func metricsDecodeIncludingAnEmptyRoute() throws {
        /// A route with no traffic serializes as `{"count": 0}` — no p50, no
        /// p95, no max. Those have to be optional or the Health screen breaks
        /// on any window where the deep path was idle, which is most of them.
        let response = try Self.decode(MetricsResponse.self, from: "metrics")
        #expect(response.fast.count == 3)
        #expect(response.fast.p95 == 1410)
        #expect(response.deep.count == 0)
        #expect(response.deep.p95 == nil)
    }

    @Test func spendDecodes() throws {
        let response = try Self.decode(MetricsResponse.self, from: "metrics")
        #expect(response.spend.model == "claude-haiku-4-5")
        #expect(response.spend.inputTokens == 7709)
        #expect(response.spend.usd > 0)
    }

    @Test func metricsDecodesTheTurnBlock() throws {
        /// The turn sits beside the two route blocks and is read the same
        /// way. It is larger than `fast` in every real window, which is the
        /// entire reason it was added — /say cannot see the endpointer in
        /// front of it or the synthesis behind it.
        let response = try Self.decode(MetricsResponse.self, from: "metrics")
        let turn = try #require(response.turn)
        #expect(turn.count == 3)
        #expect(turn.p50 == 1840)
        #expect(turn.p95 == 2260)
        #expect(turn.p50! > response.fast.p50!)
    }

    @Test func aServerWithoutTheTurnColumnStillDecodes() throws {
        /// `turn` is optional because the app updates on its own schedule and
        /// a Mini that has not been migrated yet simply omits it. A required
        /// field would take the whole Health screen down over a block that is
        /// only ever informational.
        let json = """
        {"fast": {"count": 0}, "deep": {"count": 0},
         "spend": {"model": "claude-haiku-4-5", "utterances": 0,
                   "model_calls": 0, "input_tokens": 0, "output_tokens": 0,
                   "usd": 0.0, "usd_per_utterance": 0.0,
                   "usd_per_month_at_this_rate": 0.0}}
        """
        let decoder = JSONDecoder()
        decoder.keyDecodingStrategy = .convertFromSnakeCase
        let response = try decoder.decode(MetricsResponse.self, from: Data(json.utf8))
        #expect(response.turn == nil)
    }

    // MARK: - /say

    @Test func sayResponseSurfacesTheCreatedReminder() throws {
        /// What the Siri snippet's Snooze button acts on. Without `changed`,
        /// the snippet can only read the reply out loud.
        let json = Data(
            """
            {"reply":"Got it.","route":"fast","utterance_id":9,"latency_ms":600,
             "changed":{"table":"reminders","row_id":12,"op":"insert"}}
            """.utf8
        )
        let decoder = JSONDecoder()
        decoder.keyDecodingStrategy = .convertFromSnakeCase
        let response = try decoder.decode(SayResponse.self, from: json)
        #expect(response.reminderID == 12)
    }

    @Test func aNoteIsNotMistakenForAReminder() throws {
        let json = Data(
            """
            {"reply":"Noted.","route":"fast","utterance_id":9,
             "changed":{"table":"notes","row_id":3,"op":"insert"}}
            """.utf8
        )
        let decoder = JSONDecoder()
        decoder.keyDecodingStrategy = .convertFromSnakeCase
        let response = try decoder.decode(SayResponse.self, from: json)
        #expect(response.reminderID == nil, "Snooze must not be offered for a note")
    }

    @Test func theDeepPathResponseHasNoChangedField() throws {
        let json = Data(
            """
            {"reply":"On it.","route":"deep","job_id":27,"utterance_id":10}
            """.utf8
        )
        let decoder = JSONDecoder()
        decoder.keyDecodingStrategy = .convertFromSnakeCase
        let response = try decoder.decode(SayResponse.self, from: json)
        #expect(response.jobId == 27)
        #expect(response.changed == nil)
    }

    // MARK: - Gratitude

    @Test func gratitudeDecodes() throws {
        let response = try Self.decode(GratitudeResponse.self, from: "gratitude")

        #expect(response.today.on == "2026-08-04")
        #expect(response.today.target == 3)
        #expect(response.today.entries.count == 2)
        #expect(response.today.entries[0].body == "the sun")
        #expect(response.streak == 9)
        #expect(response.days.map(\.on) == ["2026-08-03", "2026-08-02"])
    }

    @Test func anIncompleteDayDecodesWithoutPadding() throws {
        /// The server sends what was actually said; the three slots are the
        /// view's business. A client that expected three entries per day would
        /// throw on every day anyone answered twice.
        let response = try Self.decode(GratitudeResponse.self, from: "gratitude")
        let short = try #require(response.days.first { $0.on == "2026-08-02" })
        #expect(short.entries.count == 1)
    }

    // MARK: - Projects

    @Test func projectListDecodes() throws {
        let response = try Self.decode(ProjectsResponse.self, from: "projects")
        let project = try #require(response.projects.first)

        #expect(project.name == "Hydroponic Lettuce")
        #expect(project.status == "active")
        #expect(project.noteCount == 2)
        #expect(project.reportCount == 1)
        #expect(project.linkCount == 1)
    }

    @Test func projectDetailDecodes() throws {
        let detail = try Self.decode(ProjectDetail.self, from: "project_detail")

        #expect(detail.name == "Hydroponic Lettuce")
        // Every section must decode even when empty — a throw here renders as
        // a blank screen with an error nobody reads.
        #expect(detail.notes.count == 2)
        #expect(detail.reports.count == 1)
        #expect(detail.events.count == 1)
        #expect(detail.reminders.count == 1)
        #expect(detail.links.count == 1)
        #expect(detail.files.count == 1)
    }

    /// Whether a string is a raw ISO 8601 stamp. Deliberately a pattern and
    /// not `contains("T")` — "Tuesday, August 11 at 3 PM" is a correctly
    /// spoken date that happens to start with the same letter.
    static func looksLikeISO(_ value: String) -> Bool {
        value.range(of: #"\d{4}-\d{2}-\d{2}T"#, options: .regularExpression) != nil
    }

    @Test func theServerSpeaksItsOwnDates() throws {
        /// An ISO string reaching the phone means someone dropped the `when`
        /// rendering and the screen is about to show "2026-08-11T21:00:00Z" to
        /// a human. The rule is stated on AgendaResponse and holds here too.
        let detail = try Self.decode(ProjectDetail.self, from: "project_detail")

        let event = try #require(detail.events.first)
        #expect(event.when == "Tuesday, August 11 at 3 PM")
        #expect(Self.looksLikeISO(event.when) == false)

        let note = try #require(detail.notes.first)
        #expect(Self.looksLikeISO(note.when) == false)

        let file = try #require(detail.files.first)
        #expect(Self.looksLikeISO(file.when) == false)
    }

    @Test func aMissingEndpointIsNotAnUnreachableMini() throws {
        /// The failure mode that sent a real debugging session down the wrong
        /// path: a new screen against a daemon that had not been restarted
        /// answered 404, and the screen said "Can't reach the Mini — usually
        /// means Tailscale is off". The Mini was fine. The remedy was the
        /// opposite one.
        #expect(Failure.isMissingEndpoint(APIError.server(404, "Not Found")))
        #expect(Failure.isMissingEndpoint(APIError.server(500, "boom")) == false)
        #expect(Failure.isMissingEndpoint(APIError.transport("offline")) == false)
        #expect(Failure.isMissingEndpoint(APIError.unauthorized) == false)
    }

    @Test func aReportStillMissingItsSummaryDecodes() throws {
        /// `summary` is NULL for anything that finished before summaries
        /// existed, and that is normal and permanent rather than pending.
        let json = """
        {"id": 1, "name": "x", "description": null, "status": "active",
         "notes": [], "reports": [{"id": 3, "prompt": "a", "status": "done",
         "summary": null, "error": null}],
         "events": [], "reminders": [], "links": [], "files": []}
        """
        let decoder = JSONDecoder()
        decoder.keyDecodingStrategy = .convertFromSnakeCase
        let detail = try decoder.decode(ProjectDetail.self, from: Data(json.utf8))
        #expect(detail.reports[0].summary == nil)
    }

    // MARK: - Turns

    @Test func turnReportEncodesAsTheServerExpects() throws {
        /// There is no `Encodable` request type anywhere in this app — every
        /// body is a `[String: Any]` handed to `JSONSerialization`, so the
        /// key names are written out rather than derived, and nothing but a
        /// test stops one drifting from the column it updates.
        let data = try JSONSerialization.data(
            withJSONObject: JarvisAPI.turnBody(utteranceId: 412, turnMs: 1840)
        )
        let json = try #require(
            try JSONSerialization.jsonObject(with: data) as? [String: Any]
        )
        #expect(json["utterance_id"] as? Int == 412)
        #expect(json["turn_ms"] as? Int == 1840)
    }
}

/// Anchors `Bundle(for:)` to the test bundle rather than the app's.
private final class BundleMarker {}
