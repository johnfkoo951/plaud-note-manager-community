import Foundation
import XCTest
@testable import PlaudNoteApp

final class CommunityRoutingContractTests: XCTestCase {
    private let planID = "0123456789abcdef0123456789abcdef"

    func testPreviewRequiresOneValidPlanIDAcrossEveryRow() throws {
        let rows = try decodeRows([
            row(fileID: "a", planID: planID, applied: false, movedTo: ""),
            row(fileID: "b", planID: planID, applied: false, movedTo: ""),
        ])

        XCTAssertEqual(FileStore.validatedCommunityPlanID(in: rows), planID)

        let mixed = try decodeRows([
            row(fileID: "a", planID: planID, applied: false, movedTo: ""),
            row(
                fileID: "b",
                planID: "fedcba9876543210fedcba9876543210",
                applied: false,
                movedTo: ""
            ),
        ])
        XCTAssertNil(FileStore.validatedCommunityPlanID(in: mixed))

        let uppercase = try decodeRows([
            row(fileID: "a", planID: planID.uppercased(), applied: false, movedTo: ""),
        ])
        XCTAssertNil(FileStore.validatedCommunityPlanID(in: uppercase))
    }

    func testApplyArgumentsEchoExactPlanIDAndSelectedRows() {
        XCTAssertEqual(
            FileStore.communityApplyArguments(fileIDs: ["b", "a", "b"], planID: planID),
            [
                "auto-folder", "--apply", "--plan-id", planID,
                "--min-confidence", "0.6",
                "--only", "b", "--only", "a", "--json",
            ]
        )
        XCTAssertNil(
            FileStore.communityApplyArguments(
                fileIDs: ["a"],
                planID: "not-a-preview-id"
            )
        )
    }

    func testLegacyGeminiAndGrokCLIBackendsAreForcedToAPI() {
        XCTAssertEqual(
            FileStore.normalizedCommunityBackend(provider: "gemini", storedBackend: "cli"),
            "api"
        )
        XCTAssertEqual(
            FileStore.normalizedCommunityBackend(provider: "grok", storedBackend: "cli"),
            "api"
        )
        XCTAssertEqual(
            FileStore.normalizedCommunityBackend(provider: "claude", storedBackend: "cli"),
            "cli"
        )
        XCTAssertEqual(
            FileStore.normalizedCommunityBackend(provider: "codex", storedBackend: "api"),
            "api"
        )
    }

    func testPartialAndAllFailureSummariesUseAppliedJSONFields() throws {
        let partialRows = try decodeRows([
            row(fileID: "a", planID: planID, applied: true, movedTo: "folder-a"),
            row(
                fileID: "b",
                planID: planID,
                applied: false,
                movedTo: "",
                error: "Cloud apply failed: RuntimeError"
            ),
        ])
        let partial = try XCTUnwrap(
            FileStore.communityApplySummary(
                selectedFileIDs: ["a", "b"],
                expectedPlanID: planID,
                results: partialRows
            )
        )
        XCTAssertEqual(partial.movedCount, 1)
        XCTAssertEqual(partial.failedCount, 1)
        XCTAssertEqual(partial.warningCount, 0)
        XCTAssertTrue(partial.message?.contains("1개는 이동했고 1개는 이동하지 못했습니다") == true)
        XCTAssertTrue(partial.message?.contains("Cloud apply failed") == true)

        let failedRows = try decodeRows([
            row(
                fileID: "a",
                planID: planID,
                applied: false,
                movedTo: "",
                error: "Cloud apply failed: TimeoutError"
            ),
            row(
                fileID: "b",
                planID: planID,
                applied: false,
                movedTo: "",
                error: "Cloud apply failed: RuntimeError"
            ),
        ])
        let failed = try XCTUnwrap(
            FileStore.communityApplySummary(
                selectedFileIDs: ["a", "b"],
                expectedPlanID: planID,
                results: failedRows
            )
        )
        XCTAssertEqual(failed.movedCount, 0)
        XCTAssertEqual(failed.failedCount, 2)
        XCTAssertTrue(failed.message?.contains("선택한 2개 모두") == true)
    }

    func testApplySummaryRejectsMissingRowsWrongPlanAndContradictoryMoveState() throws {
        let oneRow = try decodeRows([
            row(fileID: "a", planID: planID, applied: true, movedTo: "folder-a"),
        ])
        XCTAssertNil(
            FileStore.communityApplySummary(
                selectedFileIDs: ["a", "b"],
                expectedPlanID: planID,
                results: oneRow
            )
        )

        let wrongPlan = try decodeRows([
            row(
                fileID: "a",
                planID: "fedcba9876543210fedcba9876543210",
                applied: true,
                movedTo: "folder-a"
            ),
        ])
        XCTAssertNil(
            FileStore.communityApplySummary(
                selectedFileIDs: ["a"],
                expectedPlanID: planID,
                results: wrongPlan
            )
        )

        let contradictory = try decodeRows([
            row(fileID: "a", planID: planID, applied: false, movedTo: "folder-a"),
        ])
        XCTAssertNil(
            FileStore.communityApplySummary(
                selectedFileIDs: ["a"],
                expectedPlanID: planID,
                results: contradictory
            )
        )
    }

    func testProcessCaptureDrainsLargeStdoutAndStderrWhileChildRuns() throws {
        let process = Process()
        process.executableURL = URL(fileURLWithPath: "/usr/bin/awk")
        let chunk = String(repeating: "x", count: 128)
        let lines = 4_096
        process.arguments = [
            "BEGIN { for (i = 0; i < \(lines); i++) { "
                + "print \"\(chunk)\"; print \"\(chunk)\" > \"/dev/stderr\" } }",
        ]

        let result = try FileStore.executeAndCapture(process, timeout: 5)

        XCTAssertTrue(result.ok)
        XCTAssertEqual(result.stdout.utf8.count, (chunk.utf8.count + 1) * lines)
        XCTAssertEqual(result.stderr.utf8.count, (chunk.utf8.count + 1) * lines)
    }

    func testProcessCaptureRemapsPipesWhenStandardDescriptorsStartClosed() throws {
        let result = try { () throws -> FileStore.CommandResult in
            let standardDescriptors = [STDIN_FILENO, STDOUT_FILENO, STDERR_FILENO]
            let savedDescriptors = standardDescriptors.map { Darwin.dup($0) }
            guard savedDescriptors.allSatisfy({ $0 >= 0 }) else {
                savedDescriptors.filter { $0 >= 0 }.forEach { _ = Darwin.close($0) }
                throw XCTSkip("test runner did not provide all standard descriptors")
            }
            standardDescriptors.forEach { _ = Darwin.close($0) }
            defer {
                for (saved, standard) in zip(savedDescriptors, standardDescriptors) {
                    _ = Darwin.dup2(saved, standard)
                    _ = Darwin.close(saved)
                }
            }

            let process = Process()
            process.executableURL = URL(fileURLWithPath: "/bin/sh")
            process.arguments = [
                "-c",
                "IFS= read -r line; printf 'out:%s' \"$line\"; printf 'err:%s' \"$line\" >&2",
            ]
            return try FileStore.executeAndCapture(
                process,
                stdin: "collision-check\n",
                timeout: 2
            )
        }()

        XCTAssertTrue(result.ok)
        XCTAssertEqual(result.stdout, "out:collision-check")
        XCTAssertEqual(result.stderr, "err:collision-check")
    }

    func testProcessCaptureDoesNotInheritUnrelatedParentDescriptors() throws {
        let sentinelURL = FileManager.default.temporaryDirectory
            .appendingPathComponent(UUID().uuidString)
        FileManager.default.createFile(atPath: sentinelURL.path, contents: Data("x".utf8))
        defer { try? FileManager.default.removeItem(at: sentinelURL) }
        let sentinel = Darwin.open(sentinelURL.path, O_RDONLY)
        XCTAssertGreaterThanOrEqual(sentinel, 0)
        guard sentinel >= 0 else { return }
        defer { _ = Darwin.close(sentinel) }
        XCTAssertEqual(Darwin.fcntl(sentinel, F_SETFD, 0), 0)

        let process = Process()
        process.executableURL = URL(fileURLWithPath: "/usr/bin/python3")
        process.arguments = [
            "-c",
            "import errno,os,sys\n"
                + "try:\n os.fstat(int(sys.argv[1]))\nexcept OSError as e:\n "
                + "sys.exit(0 if e.errno == errno.EBADF else 2)\nsys.exit(1)",
            String(sentinel),
        ]

        let result = try FileStore.executeAndCapture(process, timeout: 2)

        XCTAssertTrue(result.ok, result.failureMessage)
    }

    func testProcessCapturePreservesTimeoutWatchdog() throws {
        let process = Process()
        process.executableURL = URL(fileURLWithPath: "/bin/sleep")
        process.arguments = ["2"]

        let result = try FileStore.executeAndCapture(process, timeout: 0.05)

        XCTAssertFalse(result.ok)
        XCTAssertEqual(result.exitCode, -1)
        XCTAssertTrue(result.stderr.contains("timed out"))
        XCTAssertFalse(result.forceKilled)
    }

    func testProcessCaptureForceKillsChildThatIgnoresTermination() throws {
        let process = Process()
        process.executableURL = URL(fileURLWithPath: "/bin/sh")
        process.arguments = ["-c", "trap '' TERM; while :; do :; done"]
        let startedAt = Date()

        let result = try FileStore.executeAndCapture(
            process,
            timeout: 0.1,
            terminationGrace: 0.1
        )

        XCTAssertFalse(result.ok)
        XCTAssertEqual(result.exitCode, -1)
        XCTAssertTrue(result.forceKilled)
        XCTAssertLessThan(Date().timeIntervalSince(startedAt), 2)
    }

    func testTimeoutKillsDescendantThatKeepsCommandPipesOpen() throws {
        let process = Process()
        process.executableURL = URL(fileURLWithPath: "/bin/sh")
        // The leader honors TERM and exits. Its background descendant ignores
        // TERM and would keep the inherited pipes open for three seconds if
        // the watchdog killed only the leader instead of the owned group.
        process.arguments = [
            "-c",
            "trap 'exit 0' TERM; (trap '' TERM; sleep 3) & wait",
        ]
        let startedAt = Date()

        let result = try FileStore.executeAndCapture(
            process,
            timeout: 0.1,
            terminationGrace: 0.1,
            drainGrace: 0.2
        )

        XCTAssertFalse(result.ok)
        XCTAssertEqual(result.exitCode, -1)
        XCTAssertTrue(result.forceKilled)
        XCTAssertLessThan(Date().timeIntervalSince(startedAt), 1)
    }

    func testSuccessfulLeaderCannotLeavePipeHoldingDescendantRunning() throws {
        let process = Process()
        process.executableURL = URL(fileURLWithPath: "/bin/sh")
        process.arguments = [
            "-c",
            "(trap '' TERM; sleep 30) & echo $!; exit 0",
        ]
        let startedAt = Date()

        let result = try FileStore.executeAndCapture(
            process,
            timeout: 5,
            terminationGrace: 0.1,
            drainGrace: 0.1
        )

        XCTAssertFalse(result.ok)
        XCTAssertEqual(result.exitCode, -1)
        XCTAssertTrue(result.forceKilled)
        XCTAssertTrue(result.stderr.contains("helper process"))
        XCTAssertLessThan(Date().timeIntervalSince(startedAt), 1)
        let descendantPID = try XCTUnwrap(pid_t(result.stdout.trimmingCharacters(
            in: .whitespacesAndNewlines
        )))
        var descendantIsGone = false
        for _ in 0..<200 {
            errno = 0
            if Darwin.kill(descendantPID, 0) == -1, errno == ESRCH {
                descendantIsGone = true
                break
            }
            usleep(10_000)
        }
        XCTAssertTrue(descendantIsGone)
    }

    func testSameFileTranscriptionCanOnlyBeReservedOnce() {
        var active: Set<String> = []

        XCTAssertTrue(FileStore.reserveTranscription("recording-1", in: &active))
        XCTAssertFalse(FileStore.reserveTranscription("recording-1", in: &active))
        XCTAssertTrue(FileStore.reserveTranscription("recording-2", in: &active))
    }

    func testAmbiguousElevenLabsAttemptRequiresExplicitForcedRetry() throws {
        XCTAssertEqual(
            FileStore.communityElevenLabsRetryState(
                from: #"{"status":"outcome_unknown","retry_may_bill_twice":true}"#
            ),
            .outcomeUnknown
        )
        XCTAssertEqual(
            FileStore.communityElevenLabsRetryState(
                from: #"{"status":"clear","retry_may_bill_twice":false}"#
            ),
            .clear
        )
        XCTAssertNil(
            FileStore.communityElevenLabsRetryState(
                from: #"{"status":"outcome_unknown","retry_may_bill_twice":false}"#
            )
        )

        let retryArguments = FileStore.communityElevenLabsArguments(
            fileID: "recording-1",
            numSpeakers: 2,
            replacingExisting: false,
            retryOutcomeUnknown: true
        )
        XCTAssertTrue(retryArguments.contains("--force"))
        XCTAssertEqual(Array(retryArguments.suffix(2)), ["--num-speakers", "2"])
        XCTAssertFalse(
            FileStore.communityElevenLabsArguments(
                fileID: "recording-1",
                numSpeakers: 0,
                replacingExisting: false,
                retryOutcomeUnknown: false
            ).contains("--force")
        )
    }

    func testDurableUndoAvailabilityParserRestoresRecoveryAction() throws {
        let recovery = try XCTUnwrap(
            FileStore.communityUndoAvailability(
                from: """
                {"status":"apply_recovery_required","count":3,
                 "detail":"stabilize first"}
                """
            )
        )
        XCTAssertEqual(recovery.status, "apply_recovery_required")
        XCTAssertEqual(recovery.count, 3)
        XCTAssertEqual(recovery.detail, "stabilize first")
        XCTAssertNotNil(
            FileStore.communityUndoAvailability(
                from: #"{"status":"undo_available","count":2,"detail":"undo ready"}"#
            )
        )
        XCTAssertNotNil(
            FileStore.communityUndoAvailability(
                from: #"{"status":"none","count":0,"detail":"nothing"}"#
            )
        )
        XCTAssertNil(
            FileStore.communityUndoAvailability(
                from: #"{"status":"apply_recovery_required","count":0,"detail":"bad"}"#
            )
        )
    }

    func testUndoSummaryKeepsOnlyCloudFailuresRetryable() throws {
        let summary = try XCTUnwrap(
            FileStore.communityUndoSummary(
                from: """
                {"status":"partial","reverted":2,"failed":[
                  {"file_id":"cloud-failure","error":"TimeoutError"},
                  {"file_id":"cache-warning","error":"local cache update failed: SQLiteError"}
                ]}
                """,
                currentCount: 3
            )
        )

        XCTAssertEqual(summary.revertedCount, 2)
        XCTAssertEqual(summary.retryCount, 1)
        XCTAssertFalse(summary.completed)
        XCTAssertTrue(summary.message.contains("다시 시도"))
    }

    func testUndoSummaryRetainsAffordanceOnStructuredError() throws {
        let summary = try XCTUnwrap(
            FileStore.communityUndoSummary(
                from: """
                {"status":"error","detail":"remote state changed","reverted":0}
                """,
                currentCount: 2
            )
        )

        XCTAssertEqual(summary.retryCount, 2)
        XCTAssertFalse(summary.completed)
        XCTAssertEqual(summary.message, "remote state changed")
    }

    func testUndoSummaryStopsAfterApplyRecoveryAndRequiresSecondConfirmation() throws {
        let detail = "중단된 폴더 적용을 안정화했습니다. 상태 확인 후 되돌리기를 다시 누르세요."
        let summary = try XCTUnwrap(
            FileStore.communityUndoSummary(
                from: """
                {"status":"apply_recovery_required","detail":"\(detail)","reverted":0,
                 "failed":[],"apply_recovery_required":true}
                """,
                currentCount: 2
            )
        )

        XCTAssertEqual(summary.revertedCount, 0)
        XCTAssertEqual(summary.retryCount, 2)
        XCTAssertFalse(summary.completed)
        XCTAssertEqual(summary.message, detail)
    }

    private func decodeRows(_ rows: [[String: Any]]) throws -> [FileStore.ClassifyPlan] {
        let data = try JSONSerialization.data(withJSONObject: rows)
        return try JSONDecoder().decode([FileStore.ClassifyPlan].self, from: data)
    }

    private func row(
        fileID: String,
        planID: String,
        applied: Bool,
        movedTo: String,
        error: String = ""
    ) -> [String: Any] {
        [
            "file_id": fileID,
            "title": "Title \(fileID)",
            "folder_name": "Folder \(fileID)",
            "confidence": 0.9,
            "reason": "matched",
            "source": "deterministic",
            "error": error,
            "applied": applied,
            "moved_to": movedTo,
            "plan_id": planID,
        ]
    }
}
