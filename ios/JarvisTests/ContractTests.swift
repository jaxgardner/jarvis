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
        #expect(response.fast.count == 2)
        #expect(response.fast.p95 == 612)
        #expect(response.deep.count == 0)
        #expect(response.deep.p95 == nil)
    }

    @Test func spendDecodes() throws {
        let response = try Self.decode(MetricsResponse.self, from: "metrics")
        #expect(response.spend.model == "claude-haiku-4-5")
        #expect(response.spend.inputTokens == 2557)
        #expect(response.spend.usd > 0)
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
}

/// Anchors `Bundle(for:)` to the test bundle rather than the app's.
private final class BundleMarker {}
