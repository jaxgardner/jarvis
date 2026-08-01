import SwiftUI
import Testing

@testable import Jarvis

/// The block parser behind the Reports screen.
///
/// Every case here is a shape the deep path actually emits — the agent writes
/// headings, bullets and tables, and the table is the one that made rendering
/// worth doing at all. The tests that matter most are the negative ones: a
/// `---` rule that is not a table, a `**Bold:**` lead-in that is not a list
/// item. Over-eager parsing is the failure mode that makes a renderer worse
/// than showing the raw text, which is what this screen did before.
struct MarkdownTests {

    // MARK: - Tables

    static let comparison = """
        Here is what I found.

        | Option | Cost | Notes |
        | :-- | ---: | :-: |
        | Rent | $1,900 | includes water |
        | Buy | $2,340 | before taxes |

        Renting wins on cash flow.
        """

    @Test func tableIsParsedAsATable() throws {
        let blocks = Markdown.blocks(of: Self.comparison)
        #expect(blocks.count == 3)

        guard case .table(let table) = blocks[1] else {
            Issue.record("expected the middle block to be a table")
            return
        }
        #expect(table.header == ["Option", "Cost", "Notes"])
        #expect(table.rows.count == 2)
        #expect(table.rows[0] == ["Rent", "$1,900", "includes water"])
    }

    @Test func delimiterColonsSetAlignment() throws {
        let blocks = Markdown.blocks(of: Self.comparison)
        guard case .table(let table) = blocks[1] else {
            Issue.record("expected a table")
            return
        }
        #expect(table.alignments == [.leading, .trailing, .center])
    }

    /// Without the leading and trailing pipes, which plenty of generators omit.
    @Test func tableWithoutOuterPipes() throws {
        let source = """
            Name | Qty
            --- | ---
            milk | 2
            """
        guard case .table(let table) = Markdown.blocks(of: source).first else {
            Issue.record("expected a table")
            return
        }
        #expect(table.header == ["Name", "Qty"])
        #expect(table.rows == [["milk", "2"]])
    }

    /// The check that keeps a horizontal rule from swallowing the line above
    /// it. `---` on its own is a rule; `---|---` under a row of cells is a
    /// table, and only the second line can tell them apart.
    @Test func ruleIsNotATable() {
        let blocks = Markdown.blocks(of: "One paragraph.\n\n---\n\nAnother.")
        #expect(blocks.count == 3)
        guard case .rule = blocks[1] else {
            Issue.record("expected a horizontal rule, got \(blocks[1])")
            return
        }
    }

    /// A pipe in prose is a pipe in prose.
    @Test func pipesWithoutADelimiterRowStayText() {
        let blocks = Markdown.blocks(of: "Run `a | b` to pipe it.")
        guard case .paragraph = blocks.first else {
            Issue.record("expected a paragraph, got \(String(describing: blocks.first))")
            return
        }
    }

    // MARK: - Lists

    @Test func bulletsAndNumbersBothParse() {
        guard case .list(let bullets) = Markdown.blocks(of: "- one\n- two").first else {
            Issue.record("expected a bullet list")
            return
        }
        #expect(bullets.map(\.text) == ["one", "two"])
        #expect(bullets.allSatisfy { $0.marker == "•" })

        guard case .list(let ordered) = Markdown.blocks(of: "3. third\n4. fourth").first else {
            Issue.record("expected an ordered list")
            return
        }
        // The author's own numbering, not a re-count from one.
        #expect(ordered.map(\.marker) == ["3.", "4."])
    }

    @Test func indentedItemsCarryTheirDepth() {
        guard case .list(let items) = Markdown.blocks(of: "- top\n  - under").first else {
            Issue.record("expected a list")
            return
        }
        #expect(items.map(\.depth) == [0, 1])
    }

    /// `**Note:** …` starts with the bullet character and is not a bullet. The
    /// space after the marker is the whole test.
    @Test func boldLeadInIsNotAListItem() {
        guard case .paragraph = Markdown.blocks(of: "**Note:** the lease renews.").first else {
            Issue.record("expected a paragraph")
            return
        }
    }

    // MARK: - Other blocks

    @Test func headingsCarryTheirLevel() {
        guard case .heading(let level, let text) = Markdown.blocks(of: "## Findings").first else {
            Issue.record("expected a heading")
            return
        }
        #expect(level == 2)
        #expect(text == "Findings")
    }

    @Test func fencedCodeKeepsItsLinesAndItsMarkup() {
        let source = "```python\n# not a heading\nx = 1\n```"
        guard case .code(let body) = Markdown.blocks(of: source).first else {
            Issue.record("expected a code block")
            return
        }
        #expect(body == "# not a heading\nx = 1")
    }

    /// A fence the agent forgot to close still ends somewhere, rather than
    /// eating the rest of the report or looping.
    @Test func unclosedFenceTerminatesAtTheEnd() {
        let blocks = Markdown.blocks(of: "```\nstill going")
        #expect(blocks.count == 1)
        guard case .code(let body) = blocks[0] else {
            Issue.record("expected a code block")
            return
        }
        #expect(body == "still going")
    }

    @Test func consecutiveLinesJoinIntoOneParagraph() {
        guard case .paragraph(let text) = Markdown.blocks(of: "one line\nand its wrap").first else {
            Issue.record("expected one paragraph")
            return
        }
        #expect(text == "one line and its wrap")
    }

    // MARK: - Summary

    /// What the list card shows. The point is that it never opens on `#` or on
    /// a row of pipes.
    @Test func summarySkipsMarkupAndFindsProse() {
        #expect(Markdown.summary(of: Self.comparison) == "Here is what I found.")
        #expect(Markdown.summary(of: "## Findings\n\nBody.") == "Findings")
        #expect(Markdown.summary(of: "- first point\n- second") == "first point")
        #expect(Markdown.summary(of: "**Bold** lead") == "Bold lead")
    }

    @Test func summaryOfATableNamesItsColumns() {
        let source = "| Option | Cost |\n| --- | --- |\n| Rent | $1,900 |"
        #expect(Markdown.summary(of: source) == "Option · Cost")
    }

    @Test func summaryOfPlainTextIsThatText() {
        #expect(Markdown.summary(of: "  no markup here\n") == "no markup here")
    }

    // MARK: - Inline

    @Test func inlineEmphasisIsStrippedOfItsMarkers() {
        #expect(String(Markdown.inline("a **bold** word").characters) == "a bold word")
        #expect(String(Markdown.inline("a `code` word").characters) == "a code word")
    }

    /// Unbalanced markers are a thing models emit. Returning the line as-is
    /// beats returning nothing.
    @Test func malformedInlineFallsBackToTheLiteralText() {
        let text = "half **open"
        #expect(String(Markdown.inline(text).characters).contains("open"))
    }
}
