// Reads the two TCC-protected databases and prints NDJSON. Nothing else.
//
// This binary is the only thing in the system holding Full Disk Access, so it
// is deliberately the least interesting program here: it parses nothing,
// interprets nothing, and writes nothing. attributedBody goes out as base64
// and is decoded in Python, where a bug is a failed test rather than a
// privileged crash.
//
// Exit codes: 0 ok, 2 TCC denied, 3 usage, 4 sqlite error.

import Foundation
import SQLite3

let SQLITE_TRANSIENT = unsafeBitCast(-1, to: sqlite3_destructor_type.self)

func fail(_ message: String, _ code: Int32) -> Never {
    FileHandle.standardError.write(Data((message + "\n").utf8))
    exit(code)
}

func openReadOnly(_ path: String) -> OpaquePointer {
    var db: OpaquePointer?
    // mode=ro because chat.db is live and WAL. We are a reader and must never
    // be anything else.
    let uri = "file:\(path)?mode=ro"
    let rc = sqlite3_open_v2(uri, &db, SQLITE_OPEN_READONLY | SQLITE_OPEN_URI, nil)
    guard rc == SQLITE_OK, let handle = db else {
        // TCC surfaces as a plain open failure, so distinguish it by asking
        // the filesystem whether the file is there at all.
        if !FileManager.default.isReadableFile(atPath: path) {
            fail("tcc-denied: \(path)", 2)
        }
        fail("sqlite-open-failed: \(path)", 4)
    }
    return handle
}

func rows(_ db: OpaquePointer, _ sql: String, _ bind: [Any]) -> [[String: Any]] {
    var stmt: OpaquePointer?
    guard sqlite3_prepare_v2(db, sql, -1, &stmt, nil) == SQLITE_OK else {
        fail("sqlite-prepare-failed: \(String(cString: sqlite3_errmsg(db)))", 4)
    }
    defer { sqlite3_finalize(stmt) }

    for (offset, value) in bind.enumerated() {
        let index = Int32(offset + 1)
        switch value {
        case let text as String:
            sqlite3_bind_text(stmt, index, text, -1, SQLITE_TRANSIENT)
        case let number as Int:
            sqlite3_bind_int64(stmt, index, Int64(number))
        case let number as Double:
            sqlite3_bind_double(stmt, index, number)
        default:
            sqlite3_bind_null(stmt, index)
        }
    }

    var out: [[String: Any]] = []
    while sqlite3_step(stmt) == SQLITE_ROW {
        var row: [String: Any] = [:]
        for column in 0..<sqlite3_column_count(stmt) {
            let name = String(cString: sqlite3_column_name(stmt, column))
            switch sqlite3_column_type(stmt, column) {
            case SQLITE_INTEGER:
                row[name] = sqlite3_column_int64(stmt, column)
            case SQLITE_FLOAT:
                row[name] = sqlite3_column_double(stmt, column)
            case SQLITE_TEXT:
                row[name] = String(cString: sqlite3_column_text(stmt, column))
            case SQLITE_BLOB:
                if let bytes = sqlite3_column_blob(stmt, column) {
                    let count = Int(sqlite3_column_bytes(stmt, column))
                    row[name] = Data(bytes: bytes, count: count).base64EncodedString()
                }
            default:
                break
            }
        }
        out.append(row)
    }
    return out
}

func emit(_ rows: [[String: Any]]) {
    for row in rows {
        guard let data = try? JSONSerialization.data(withJSONObject: row) else { continue }
        FileHandle.standardOutput.write(data)
        FileHandle.standardOutput.write(Data("\n".utf8))
    }
}

// ── arguments ────────────────────────────────────────────────────────────

var args = Array(CommandLine.arguments.dropFirst())
guard let command = args.first else { fail("usage: tccread <messages|calls> [--since ISO] [--limit N]", 3) }
args = Array(args.dropFirst())

var since = "1970-01-01T00:00:00Z"
var limit = 2000
var index = 0
while index < args.count - 1 {
    if args[index] == "--since" { since = args[index + 1] }
    if args[index] == "--limit" { limit = Int(args[index + 1]) ?? limit }
    index += 2
}

let home = FileManager.default.homeDirectoryForCurrentUser.path

// Apple's epochs. Messages stores nanoseconds since 2001-01-01; CallHistory
// stores seconds since the same instant. Both are converted in Python — this
// only needs to filter, so it converts the *bound* value, not the rows.
let appleEpoch = Date(timeIntervalSince1970: 978_307_200)
let formatter = ISO8601DateFormatter()
formatter.formatOptions = [.withInternetDateTime]
let sinceDate = formatter.date(from: since) ?? Date(timeIntervalSince1970: 0)
let sinceApple = sinceDate.timeIntervalSince(appleEpoch)

switch command {
case "messages":
    let db = openReadOnly("\(home)/Library/Messages/chat.db")
    emit(rows(db, """
        SELECT m.ROWID           AS external_id,
               h.id              AS handle,
               m.is_from_me      AS is_from_me,
               m.text            AS text,
               m.attributedBody  AS attributed_body,
               m.service         AS service,
               m.date            AS apple_date
          FROM message m
          LEFT JOIN handle h ON h.ROWID = m.handle_id
         WHERE m.date > ?
         ORDER BY m.date ASC
         LIMIT ?
        """, [sinceApple * 1_000_000_000, limit]))

case "calls":
    let db = openReadOnly("\(home)/Library/Application Support/CallHistoryDB/CallHistory.storedata")
    emit(rows(db, """
        SELECT Z_PK        AS external_id,
               ZADDRESS    AS handle,
               ZORIGINATED AS originated,
               ZANSWERED   AS answered,
               ZDURATION   AS duration,
               ZDATE       AS apple_date
          FROM ZCALLRECORD
         WHERE ZDATE > ?
         ORDER BY ZDATE ASC
         LIMIT ?
        """, [sinceApple, limit]))

default:
    fail("unknown command: \(command)", 3)
}
