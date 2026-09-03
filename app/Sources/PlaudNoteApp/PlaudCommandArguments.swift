enum PlaudCommandArguments {
    /// `--` terminates Click/Typer option parsing so tags such as `-topic` and
    /// `--topic` reach the positional normalizer instead of becoming options.
    static func tagAdd(fileID: String, tag: String) -> [String] {
        ["tag-add", fileID, "--", tag]
    }

    static func tagRemove(fileID: String, tag: String) -> [String] {
        ["tag-remove", fileID, "--", tag]
    }
}
