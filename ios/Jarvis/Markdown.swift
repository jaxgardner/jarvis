import SwiftUI

/// Just enough Markdown to make a deep-path result readable.
///
/// The agent writes prose with headings and bullets and — whenever you ask it
/// to compare anything — a table. The detail screen used to show that raw, on
/// the reasoning that a half-working markdown pass would mangle more than it
/// fixed. That reasoning held for inline emphasis and got the *block* structure
/// exactly backwards: a table rendered as raw pipes is not degraded, it is
/// unreadable, and `|---|---|` is most of a screen.
///
/// So this parses only block structure — headings, lists, code fences, tables,
/// quotes, rules — and hands each block's text to Foundation's own inline
/// parser for the emphasis. Nothing here has to guess at nesting or reference
/// links, because nothing here has to be CommonMark. The escape hatch is on the
/// screen instead: `ReportDetailView` has a Raw toggle, so a result this
/// mis-reads is one tap from being legible again.
enum Markdown {

    // MARK: - Model

    struct ListItem {
        /// Rendered as-is: "•" for a bullet, "3." for the third of an ordered
        /// list. Keeping the author's own numbering matters when the agent
        /// starts at something other than one.
        let marker: String
        let text: String
        /// Nesting depth, two spaces to the level.
        let depth: Int
    }

    struct Table {
        let header: [String]
        let rows: [[String]]
        let alignments: [TextAlignment]
    }

    enum Block {
        case heading(level: Int, text: String)
        case paragraph(String)
        case list([ListItem])
        case code(String)
        case table(Table)
        case quote([String])
        case rule
    }

    // MARK: - Parsing

    static func blocks(of source: String) -> [Block] {
        let lines = source
            .replacingOccurrences(of: "\r\n", with: "\n")
            .components(separatedBy: "\n")

        var blocks: [Block] = []
        var paragraph: [String] = []
        var index = 0

        func flush() {
            guard !paragraph.isEmpty else { return }
            blocks.append(.paragraph(paragraph.joined(separator: " ")))
            paragraph = []
        }

        while index < lines.count {
            let line = lines[index]
            let trimmed = line.trimmingCharacters(in: .whitespaces)

            if trimmed.isEmpty {
                flush()
                index += 1
                continue
            }

            if let fence = fence(trimmed) {
                flush()
                index += 1
                var body: [String] = []
                while index < lines.count, self.fence(lines[index].trimmingCharacters(in: .whitespaces)) != fence {
                    body.append(lines[index])
                    index += 1
                }
                index += 1  // the closing fence, or past the end if there isn't one
                blocks.append(.code(body.joined(separator: "\n")))
                continue
            }

            // A table is two lines before it is one: a row of cells, then a
            // delimiter row. Checked before the horizontal rule, which `---`
            // would otherwise claim.
            if trimmed.contains("|"),
               index + 1 < lines.count,
               let alignments = delimiters(in: lines[index + 1]) {
                flush()
                let header = cells(of: trimmed)
                index += 2
                var rows: [[String]] = []
                while index < lines.count, lines[index].contains("|") {
                    rows.append(cells(of: lines[index]))
                    index += 1
                }
                blocks.append(.table(Table(header: header, rows: rows, alignments: alignments)))
                continue
            }

            if isRule(trimmed) {
                flush()
                blocks.append(.rule)
                index += 1
                continue
            }

            if let heading = heading(trimmed) {
                flush()
                blocks.append(heading)
                index += 1
                continue
            }

            if trimmed.hasPrefix(">") {
                flush()
                var body: [String] = []
                while index < lines.count {
                    let quoted = lines[index].trimmingCharacters(in: .whitespaces)
                    guard quoted.hasPrefix(">") else { break }
                    body.append(String(quoted.dropFirst()).trimmingCharacters(in: .whitespaces))
                    index += 1
                }
                blocks.append(.quote(body))
                continue
            }

            if listItem(line) != nil {
                flush()
                var items: [ListItem] = []
                while index < lines.count, let item = listItem(lines[index]) {
                    items.append(item)
                    index += 1
                }
                blocks.append(.list(items))
                continue
            }

            paragraph.append(trimmed)
            index += 1
        }

        flush()
        return blocks
    }

    /// Inline emphasis, left to Foundation. `.inlineOnly` is the point — the
    /// block structure is already decided by the time anything gets here, and
    /// letting the full parser at it would re-interpret a line that is only a
    /// paragraph because this file said so.
    static func inline(_ text: String) -> AttributedString {
        let parsed = try? AttributedString(
            markdown: text,
            options: .init(
                interpretedSyntax: .inlineOnlyPreservingWhitespace,
                failurePolicy: .returnPartiallyParsedIfPossible
            )
        )
        return parsed ?? AttributedString(text)
    }

    /// The first line worth reading, with the markup taken off — what a list
    /// card has room for. A table's top border is not worth either of its two
    /// lines, and a heading usually says what the whole result concluded.
    static func summary(of source: String) -> String {
        for block in blocks(of: source) {
            switch block {
            case .heading(_, let text):
                return plain(text)
            case .paragraph(let text):
                return plain(text)
            case .quote(let lines):
                if let first = lines.first(where: { !$0.isEmpty }) { return plain(first) }
            case .list(let items):
                if let first = items.first { return plain(first.text) }
            case .table(let table):
                return table.header.filter { !$0.isEmpty }.joined(separator: " · ")
            case .code, .rule:
                continue
            }
        }
        return source.trimmingCharacters(in: .whitespacesAndNewlines)
    }

    private static func plain(_ text: String) -> String {
        String(inline(text).characters)
    }

    // MARK: - Line shapes

    private static func fence(_ line: String) -> String? {
        for marker in ["```", "~~~"] where line.hasPrefix(marker) { return marker }
        return nil
    }

    private static func heading(_ line: String) -> Block? {
        let hashes = line.prefix { $0 == "#" }
        guard (1...6).contains(hashes.count) else { return nil }
        let text = line.dropFirst(hashes.count).trimmingCharacters(in: .whitespaces)
        guard !text.isEmpty else { return nil }
        return .heading(level: hashes.count, text: text)
    }

    private static func isRule(_ line: String) -> Bool {
        let stripped = line.filter { !$0.isWhitespace }
        guard stripped.count >= 3 else { return false }
        return ["-", "*", "_"].contains { marker in
            stripped.allSatisfy { String($0) == marker }
        }
    }

    private static func listItem(_ line: String) -> ListItem? {
        let indent = line.prefix { $0 == " " || $0 == "\t" }.count
        let body = line.dropFirst(indent)
        guard let first = body.first else { return nil }

        // A bullet needs the space after the marker, which is what keeps `---`
        // and a `**Bold:**` lead-in from being read as list items.
        if "-*+".contains(first), body.dropFirst().hasPrefix(" ") {
            return ListItem(
                marker: "•",
                text: String(body.dropFirst(2)).trimmingCharacters(in: .whitespaces),
                depth: indent / 2
            )
        }

        let digits = body.prefix { $0.isNumber }
        if !digits.isEmpty {
            let rest = body.dropFirst(digits.count)
            if let separator = rest.first,
               separator == "." || separator == ")",
               rest.dropFirst().hasPrefix(" ") {
                return ListItem(
                    marker: "\(digits).",
                    text: String(rest.dropFirst(2)).trimmingCharacters(in: .whitespaces),
                    depth: indent / 2
                )
            }
        }

        return nil
    }

    private static func cells(of line: String) -> [String] {
        var text = line.trimmingCharacters(in: .whitespaces)
        if text.hasPrefix("|") { text.removeFirst() }
        if text.hasSuffix("|") { text.removeLast() }
        return text.components(separatedBy: "|").map {
            $0.trimmingCharacters(in: .whitespaces)
        }
    }

    /// `nil` unless every cell is a run of dashes with optional alignment
    /// colons — the whole test for "the line above was a header row".
    private static func delimiters(in line: String) -> [TextAlignment]? {
        let parts = cells(of: line)
        guard !parts.isEmpty else { return nil }

        var alignments: [TextAlignment] = []
        for part in parts {
            guard part.contains("-"), part.allSatisfy({ $0 == "-" || $0 == ":" }) else {
                return nil
            }
            switch (part.hasPrefix(":"), part.hasSuffix(":")) {
            case (true, true): alignments.append(.center)
            case (false, true): alignments.append(.trailing)
            default: alignments.append(.leading)
            }
        }
        return alignments
    }
}

// MARK: - Rendering

/// Renders what `Markdown` parsed, in the app's own type and colour rather than
/// anything web-like. Parsed once, in `init`: `body` runs on every state change
/// upstream and a result can be pages long.
struct MarkdownText: View {
    private let blocks: [Markdown.Block]

    init(_ source: String) {
        blocks = Markdown.blocks(of: source)
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            ForEach(Array(blocks.enumerated()), id: \.offset) { _, block in
                view(for: block)
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .textSelection(.enabled)
    }

    @ViewBuilder
    private func view(for block: Markdown.Block) -> some View {
        switch block {
        case .heading(let level, let text):
            Text(Markdown.inline(text))
                .font(Theme.sans(level <= 1 ? 20 : (level == 2 ? 17 : 15), weight: .semibold))
                .foregroundStyle(Theme.text)
                .tracking(-0.2)
                .padding(.top, 4)

        case .paragraph(let text):
            Text(Markdown.inline(text))
                .font(Theme.sans(15))
                .foregroundStyle(Theme.text2)
                .lineSpacing(4)

        case .list(let items):
            VStack(alignment: .leading, spacing: 7) {
                ForEach(Array(items.enumerated()), id: \.offset) { _, item in
                    HStack(alignment: .firstTextBaseline, spacing: 8) {
                        Text(item.marker)
                            .font(Theme.mono(12))
                            .foregroundStyle(Theme.accent)
                            .frame(minWidth: 14, alignment: .trailing)
                        Text(Markdown.inline(item.text))
                            .font(Theme.sans(15))
                            .foregroundStyle(Theme.text2)
                            .lineSpacing(3)
                            .frame(maxWidth: .infinity, alignment: .leading)
                    }
                    .padding(.leading, CGFloat(min(item.depth, 4)) * 16)
                }
            }

        case .code(let text):
            ScrollView(.horizontal, showsIndicators: false) {
                Text(text)
                    .font(Theme.mono(12.5))
                    .foregroundStyle(Theme.text2)
                    .padding(12)
            }
            .background(Theme.void.opacity(0.5), in: RoundedRectangle(cornerRadius: 10, style: .continuous))
            .overlay {
                RoundedRectangle(cornerRadius: 10, style: .continuous)
                    .strokeBorder(Theme.hairline, lineWidth: 1)
            }

        case .table(let table):
            MarkdownTable(table: table)

        case .quote(let lines):
            HStack(alignment: .top, spacing: 10) {
                Capsule().fill(Theme.accent.opacity(0.5)).frame(width: 2)
                Text(Markdown.inline(lines.joined(separator: " ")))
                    .font(Theme.sans(14.5))
                    .foregroundStyle(Theme.text3)
                    .lineSpacing(3)
            }
            .fixedSize(horizontal: false, vertical: true)

        case .rule:
            Theme.border.frame(height: 1)
        }
    }
}

/// The block that made this whole file worth writing.
///
/// Scrolls horizontally rather than squeezing, and caps a cell at 200pt so a
/// long one wraps instead of pushing the table off into the distance. Row
/// striping and a rule per row do the work that the pipes used to pretend to.
private struct MarkdownTable: View {
    let table: Markdown.Table

    private var columns: Int {
        max(table.header.count, table.rows.map(\.count).max() ?? 0)
    }

    var body: some View {
        // Indicators on, unlike every other scroll view in the app: a table
        // wider than the phone clips at the bezel with nothing to say it did,
        // and a column you can't see is worse than one you have to scroll to.
        ScrollView(.horizontal) {
            Grid(alignment: .topLeading, horizontalSpacing: 0, verticalSpacing: 0) {
                GridRow {
                    ForEach(0..<columns, id: \.self) { column in
                        cell(at: column, of: table.header, row: nil)
                    }
                }
                ForEach(table.rows.indices, id: \.self) { row in
                    GridRow {
                        ForEach(0..<columns, id: \.self) { column in
                            cell(at: column, of: table.rows[row], row: row)
                        }
                    }
                }
            }
        }
        .clipShape(RoundedRectangle(cornerRadius: 10, style: .continuous))
        .overlay {
            RoundedRectangle(cornerRadius: 10, style: .continuous)
                .strokeBorder(Theme.hairline, lineWidth: 1)
        }
    }

    /// `row: nil` is the header. The background lives on the cell rather than
    /// the `GridRow` because a row's modifiers are applied per cell anyway, and
    /// `maxHeight: .infinity` is what makes each one fill the tallest cell in
    /// its row so the stripe doesn't come out ragged.
    private func cell(at column: Int, of cells: [String], row: Int?) -> some View {
        let text = column < cells.count ? cells[column] : ""
        let isHeader = row == nil
        let alignment = column < table.alignments.count ? table.alignments[column] : .leading

        return Text(isHeader ? AttributedString(text.uppercased()) : Markdown.inline(text))
            .font(isHeader ? Theme.mono(10.5, weight: .semibold) : Theme.sans(13.5))
            .tracking(isHeader ? 0.5 : 0)
            .foregroundStyle(isHeader ? Theme.text3 : Theme.text2)
            .multilineTextAlignment(alignment)
            .lineSpacing(2)
            .frame(minWidth: 54, maxWidth: 200, alignment: anchor(alignment))
            .padding(.horizontal, 11)
            .padding(.vertical, 8)
            .frame(maxHeight: .infinity, alignment: .topLeading)
            .background(background(row: row))
            .overlay(alignment: .top) {
                if row != nil { Theme.hairline.frame(height: 1) }
            }
    }

    private func background(row: Int?) -> Color {
        guard let row else { return Theme.surface3.opacity(0.65) }
        return row.isMultiple(of: 2) ? Theme.surface.opacity(0.35) : .clear
    }

    private func anchor(_ alignment: TextAlignment) -> Alignment {
        switch alignment {
        case .center: return .center
        case .trailing: return .trailing
        case .leading: return .leading
        }
    }
}
