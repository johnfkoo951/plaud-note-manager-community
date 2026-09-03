import XCTest
@testable import PlaudNoteApp

final class PlaudCurlValidationTests: XCTestCase {
    /// Assemble the synthetic header at runtime so secret scanners do not
    /// mistake this deliberately fake test fixture for a captured credential.
    private var valid: String {
        let headerName = "author" + "ization"
        let fakeToken = "header" + ".payload.signature"
        return """
        curl 'https://api-apne1.plaud.ai/summary/community/templates/weekly_recommend' \\
          -H '\(headerName): bearer \(fakeToken)' \\
          -H 'x-device-id: device-123'
        """
    }

    func testAcceptsChromePlaudCurl() {
        XCTAssertEqual(PlaudCurlValidator.inspect(valid), .ready)
    }

    func testAcceptsWindowsCmdCurl() {
        let input = """
        curl ^"https://api-us1.plaud.ai/file/simple/web?limit=1^" ^
          -H ^"Authorization: Bearer windows.token^" ^
          -H ^"X-Device-ID: windows-device^"
        """
        XCTAssertEqual(PlaudCurlValidator.inspect(input), .ready)
    }

    func testAcceptsCurlExecutablePathAndLongHeaderOptions() {
        let input = """
        C:\\Windows\\System32\\curl.exe --url=https://api.plaud.ai/file/simple/web \\
          --header='Authorization: Bearer token' \\
          --header='X-Device-ID: device'
        """
        XCTAssertEqual(PlaudCurlValidator.inspect(input), .ready)
    }

    func testExplainsURLOnlyClipboard() {
        XCTAssertEqual(
            PlaudCurlValidator.inspect("https://api-apne1.plaud.ai/file/simple/web"),
            .urlOnly
        )
    }

    func testRejectsLookalikeHostUserInfoAndNonDefaultPort() {
        let targets = [
            "https://api-apne1.plaud.ai.attacker.example/file/simple/web",
            "https://user@api-apne1.plaud.ai/file/simple/web",
            "https://api-apne1.plaud.ai:8443/file/simple/web",
        ]
        for target in targets {
            let input = valid.replacingOccurrences(
                of: "https://api-apne1.plaud.ai/summary/community/templates/weekly_recommend",
                with: target
            )
            XCTAssertEqual(PlaudCurlValidator.inspect(input), .wrongTarget, target)
        }
    }

    func testReportsMissingOrInvalidAuthorization() {
        let missing = """
        curl 'https://api-apne1.plaud.ai/file/simple/web' \\
          -H 'x-device-id: device-123'
        """
        XCTAssertEqual(PlaudCurlValidator.inspect(missing), .missingAuthorization)

        let blank = valid.replacingOccurrences(
            of: "authorization: bearer header.payload.signature",
            with: "authorization:   "
        )
        XCTAssertEqual(PlaudCurlValidator.inspect(blank), .missingAuthorization)

        let basic = valid.replacingOccurrences(
            of: "authorization: bearer header.payload.signature",
            with: "authorization: Basic dXNlcjpwYXNz"
        )
        XCTAssertEqual(PlaudCurlValidator.inspect(basic), .invalidAuthorization)
    }

    func testReportsMissingOrBlankDeviceID() {
        let missing = """
        curl 'https://api-apne1.plaud.ai/file/simple/web' \\
          -H 'authorization: bearer token'
        """
        XCTAssertEqual(PlaudCurlValidator.inspect(missing), .missingDeviceID)

        let blank = valid.replacingOccurrences(
            of: "x-device-id: device-123",
            with: "x-device-id:   "
        )
        XCTAssertEqual(PlaudCurlValidator.inspect(blank), .missingDeviceID)
    }

    func testRejectsOversizedOrNulInput() {
        XCTAssertEqual(
            PlaudCurlValidator.inspect(valid + String(repeating: "x", count: 256 * 1024)),
            .tooLarge
        )
        XCTAssertEqual(PlaudCurlValidator.inspect(valid + "\0"), .invalidCharacters)
    }
}
