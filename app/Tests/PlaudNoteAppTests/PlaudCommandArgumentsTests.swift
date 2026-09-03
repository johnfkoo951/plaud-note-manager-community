import XCTest
@testable import PlaudNoteApp

final class PlaudCommandArgumentsTests: XCTestCase {
    func testTagArgumentsTerminateOptionParsing() {
        XCTAssertEqual(
            PlaudCommandArguments.tagAdd(fileID: "recording-1", tag: "-topic"),
            ["tag-add", "recording-1", "--", "-topic"]
        )
        XCTAssertEqual(
            PlaudCommandArguments.tagRemove(fileID: "recording-1", tag: "--topic"),
            ["tag-remove", "recording-1", "--", "--topic"]
        )
    }
}
