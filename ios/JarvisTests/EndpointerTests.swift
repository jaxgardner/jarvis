import AVFoundation
import Testing

@testable import Jarvis

/// When the app decides you have stopped talking.
///
/// This runs on the realtime audio thread against a live microphone, which is
/// the least observable place in the app — so it is built to count samples
/// rather than read a clock, and these tests feed it a scripted room: known
/// energy, known duration, no device required.
///
/// The two failures that matter are asymmetric. Firing late costs a second of
/// waiting. Firing early truncates the utterance, and "remind me to call the
/// dentist at" is worse than useless — so every case below that ends in a
/// premature send is a case worth keeping.
struct EndpointerTests {
    /// Alternating ±magnitude, so RMS is exactly `magnitude` and there is no
    /// tolerance to reason about.
    static func audio(rms magnitude: Float, seconds: Double) -> AVAudioPCMBuffer {
        let format = AVAudioFormat(standardFormatWithSampleRate: 48_000, channels: 1)!
        let frames = AVAudioFrameCount(seconds * format.sampleRate)
        let buffer = AVAudioPCMBuffer(pcmFormat: format, frameCapacity: frames)!
        buffer.frameLength = frames

        let samples = buffer.floatChannelData![0]
        for index in 0..<Int(frames) {
            samples[index] = index.isMultiple(of: 2) ? magnitude : -magnitude
        }
        return buffer
    }

    /// Quiet room tone, well under the absolute floor.
    static let roomTone: Float = 0.0008
    /// Ordinary speech at arm's length.
    static let speech: Float = 0.06

    final class Counter {
        var count = 0
    }

    /// Feeds `seconds` of audio in 50 ms chunks, the way a tap delivers it.
    static func feed(_ endpointer: Endpointer, rms: Float, seconds: Double) {
        for _ in 0..<Int(seconds / 0.05) {
            endpointer.ingest(audio(rms: rms, seconds: 0.05))
        }
    }

    static func make(pause: TimeInterval = 1.0) -> (Endpointer, Counter) {
        let counter = Counter()
        let endpointer = Endpointer(pause: pause) { counter.count += 1 }
        return (endpointer, counter)
    }

    // MARK: -

    @Test func silenceAloneNeverFires() {
        /// The app can be opened by the Action Button and then not spoken to.
        /// Ten seconds of that must not post an empty utterance.
        let (endpointer, counter) = Self.make()
        Self.feed(endpointer, rms: Self.roomTone, seconds: 10)
        #expect(counter.count == 0)
    }

    @Test func speechThenAPauseFiresOnce() {
        let (endpointer, counter) = Self.make(pause: 1.0)
        Self.feed(endpointer, rms: Self.speech, seconds: 1.5)
        #expect(counter.count == 0, "still talking")

        Self.feed(endpointer, rms: Self.roomTone, seconds: 0.5)
        #expect(counter.count == 0, "half the pause is not the pause")

        Self.feed(endpointer, rms: Self.roomTone, seconds: 1.0)
        #expect(counter.count == 1)

        // The owner is expected to stop capture, but a few buffers are always
        // still in flight when it does. They must not fire a second send.
        Self.feed(endpointer, rms: Self.roomTone, seconds: 5)
        #expect(counter.count == 1)
    }

    @Test func aBreathMidSentenceDoesNotSend() {
        /// The whole reason the default pause is 1.2s. "Remind me to call the
        /// dentist" — breath — "at four" has to arrive as one utterance.
        let (endpointer, counter) = Self.make(pause: 1.0)
        Self.feed(endpointer, rms: Self.speech, seconds: 1.0)
        Self.feed(endpointer, rms: Self.roomTone, seconds: 0.6)
        Self.feed(endpointer, rms: Self.speech, seconds: 0.5)
        #expect(counter.count == 0)

        Self.feed(endpointer, rms: Self.roomTone, seconds: 0.6)
        #expect(counter.count == 0, "the pause counter must restart after more speech")

        Self.feed(endpointer, rms: Self.roomTone, seconds: 0.5)
        #expect(counter.count == 1)
    }

    @Test func aDoorSlamIsNotAnUtterance() {
        /// Loud, brief, and produces no text. Below the minimum speech
        /// duration, so the silence after it arms nothing.
        let (endpointer, counter) = Self.make(pause: 1.0)
        Self.feed(endpointer, rms: 0.5, seconds: 0.15)
        Self.feed(endpointer, rms: Self.roomTone, seconds: 3)
        #expect(counter.count == 0)
    }

    @Test func aNoisyRoomStillEndsTheUtterance() {
        /// Fan, traffic, a café. The floor is measured, not assumed — what
        /// counts as silence here would be inaudible in a quiet room.
        let (endpointer, counter) = Self.make(pause: 1.0)
        Self.feed(endpointer, rms: 0.004, seconds: 2)
        Self.feed(endpointer, rms: 0.08, seconds: 1.5)
        Self.feed(endpointer, rms: 0.004, seconds: 1.5)
        #expect(counter.count == 1)
    }

    @Test func aCafeStillEndsTheUtterance() {
        /// Loud enough that the absolute floor alone would hear the room as
        /// continuous speech and never end anything. This is the case the
        /// measured floor exists for — and the case that pins the ceiling on
        /// it, since a threshold scaled off this much noise would land above
        /// the speaker.
        let (endpointer, counter) = Self.make(pause: 1.0)
        Self.feed(endpointer, rms: 0.02, seconds: 2)
        Self.feed(endpointer, rms: 0.09, seconds: 1.5)
        Self.feed(endpointer, rms: 0.02, seconds: 1.5)
        #expect(counter.count == 1)
    }

    @Test func openingTheMicMidSentenceStillWorks() {
        /// The Action Button path: the app launches because you are already
        /// talking, so the first audio the endpointer ever sees is speech.
        /// A floor that averaged its input would calibrate to that and go deaf.
        let (endpointer, counter) = Self.make(pause: 1.0)
        Self.feed(endpointer, rms: Self.speech, seconds: 2)
        Self.feed(endpointer, rms: Self.roomTone, seconds: 1.5)
        #expect(counter.count == 1)
    }

    @Test func rearmingResumesListening() {
        /// What happens when energy produced no text: keep the mic open rather
        /// than end a session that never captured anything.
        let (endpointer, counter) = Self.make(pause: 1.0)
        Self.feed(endpointer, rms: Self.speech, seconds: 1.0)
        Self.feed(endpointer, rms: Self.roomTone, seconds: 1.5)
        #expect(counter.count == 1)

        endpointer.rearm()
        Self.feed(endpointer, rms: Self.speech, seconds: 1.0)
        Self.feed(endpointer, rms: Self.roomTone, seconds: 1.5)
        #expect(counter.count == 2)
    }

    @Test func firesAtTheNewDefaultPause() {
        /// The default is where most of the endpointer's share of the turn is
        /// spent, so it is pinned here rather than left to the picker. Fed in
        /// the tap's own 50 ms chunks, so the boundaries are multiples of that.
        let (endpointer, counter) = Self.make(pause: VoiceSettings.defaultPause)
        Self.feed(endpointer, rms: Self.speech, seconds: 1.0)

        Self.feed(endpointer, rms: Self.roomTone, seconds: 0.4)
        #expect(counter.count == 0, "0.40s is short of the pause")

        Self.feed(endpointer, rms: Self.roomTone, seconds: 0.15)
        #expect(counter.count == 1)
    }

    @Test func aBreathMidSentenceStillDoesNotSendAt045() {
        /// The cost of shortening the endpointer, stated as a test. 0.45s
        /// leaves less room than 0.8s did, so the case that has to keep
        /// working is the ordinary one: a beat in the middle of a sentence
        /// must not post half a reminder.
        let (endpointer, counter) = Self.make(pause: 0.45)
        Self.feed(endpointer, rms: Self.speech, seconds: 1.0)
        Self.feed(endpointer, rms: Self.roomTone, seconds: 0.3)  // a breath, not a stop
        Self.feed(endpointer, rms: Self.speech, seconds: 1.0)
        #expect(counter.count == 0)
    }

    @Test func theConfiguredPauseIsRespected() {
        let (quick, quickCount) = Self.make(pause: 0.8)
        Self.feed(quick, rms: Self.speech, seconds: 1.0)
        Self.feed(quick, rms: Self.roomTone, seconds: 0.9)
        #expect(quickCount.count == 1)

        let (relaxed, relaxedCount) = Self.make(pause: 2.0)
        Self.feed(relaxed, rms: Self.speech, seconds: 1.0)
        Self.feed(relaxed, rms: Self.roomTone, seconds: 0.9)
        #expect(relaxedCount.count == 0, "the same pause must not end a relaxed session")

        Self.feed(relaxed, rms: Self.roomTone, seconds: 1.5)
        #expect(relaxedCount.count == 1)
    }
}
