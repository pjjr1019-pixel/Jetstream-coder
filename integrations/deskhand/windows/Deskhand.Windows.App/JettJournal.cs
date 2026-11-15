using System.Runtime.InteropServices;
using System.Text.Json;
using System.Text;
using System.Security.Cryptography;
using Deskhand.Core;

namespace Deskhand.Windows.App;

internal sealed record AssistantHistoryItem(string ActionId, string TaskId, string TurnId, string Name, string Status, string? Error, string CreatedUtc);

internal sealed class JettJournal : IDisposable
{
    private const int OpenReadWrite = 0x00000002;
    private const int OpenCreate = 0x00000004;
    private const int OpenFullMutex = 0x00010000;
    private const int Ok = 0;
    private const int Row = 100;
    private const int Done = 101;
    private readonly object _gate = new();
    private IntPtr _database;

    public JettJournal(string path)
    {
        Directory.CreateDirectory(Path.GetDirectoryName(Path.GetFullPath(path))!);
        var result = Open(Path.GetFullPath(path), out _database, OpenReadWrite | OpenCreate | OpenFullMutex, IntPtr.Zero);
        if (result != Ok) Throw(result);
        Check(BusyTimeout(_database, 5000));
        Execute("""
            PRAGMA journal_mode=WAL;
            PRAGMA synchronous=FULL;
            CREATE TABLE IF NOT EXISTS events (
                sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                event_id TEXT NOT NULL UNIQUE,
                happened_utc TEXT NOT NULL,
                event_type TEXT NOT NULL,
                correlation_id TEXT,
                outcome TEXT NOT NULL,
                details_json TEXT NOT NULL,
                level TEXT NOT NULL DEFAULT 'INFO'
            );
            CREATE TABLE IF NOT EXISTS prompts (
                prompt_id TEXT PRIMARY KEY,
                created_utc TEXT NOT NULL,
                route_json TEXT NOT NULL,
                status TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS replies (
                reply_id TEXT PRIMARY KEY,
                prompt_id TEXT NOT NULL UNIQUE,
                text_json TEXT NOT NULL,
                created_utc TEXT NOT NULL,
                status TEXT NOT NULL,
                error TEXT
            );
            CREATE TABLE IF NOT EXISTS actions (
                action_id TEXT PRIMARY KEY,
                reply_id TEXT NOT NULL,
                prompt_id TEXT NOT NULL,
                action_name TEXT NOT NULL,
                arguments_json TEXT NOT NULL,
                created_utc TEXT NOT NULL,
                status TEXT NOT NULL,
                error TEXT,
                evidence_json TEXT
            );
            CREATE TABLE IF NOT EXISTS surface_acks (
                reply_id TEXT NOT NULL,
                surface TEXT NOT NULL,
                acknowledged_utc TEXT NOT NULL,
                PRIMARY KEY(reply_id, surface)
            );
            CREATE TABLE IF NOT EXISTS assistant_tasks (
                task_id TEXT PRIMARY KEY, turn_id TEXT NOT NULL, route_json TEXT NOT NULL,
                created_utc TEXT NOT NULL, status TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS assistant_actions (
                action_id TEXT PRIMARY KEY, task_id TEXT NOT NULL, action_name TEXT NOT NULL,
                arguments_json TEXT NOT NULL, created_utc TEXT NOT NULL, status TEXT NOT NULL,
                error TEXT, result_json TEXT
            );
            CREATE TRIGGER IF NOT EXISTS events_no_update
            BEFORE UPDATE ON events BEGIN SELECT RAISE(ABORT, 'append-only journal'); END;
            CREATE TRIGGER IF NOT EXISTS events_no_delete
            BEFORE DELETE ON events BEGIN SELECT RAISE(ABORT, 'append-only journal'); END;
            """);
        // 100.029 (Worker C) — E41 §3.3: the events table gains a
        // `level` column (DEBUG/INFO/WARN/ERROR). Old rows backfill to
        // INFO — every event recorded before levels existed was recorded
        // as if it were INFO. Fresh databases get the column from the
        // CREATE TABLE above; existing databases get it from this one
        // ALTER TABLE. The check makes the migration idempotent:
        // SQLite has no ADD COLUMN IF NOT EXISTS, so a second run must
        // not fail on the duplicate column. The append-only triggers
        // still hold: an ALTER TABLE ADD COLUMN touches no rows and
        // fires no UPDATE/DELETE trigger. Pruning stays service-side
        // (E17 §3.4), never here.
        if (!EventsHasLevelColumn())
            Execute("ALTER TABLE events ADD COLUMN level TEXT NOT NULL DEFAULT 'INFO';");
    }

    // Shared ledger foundation. These methods do not grant or execute actions.
    public bool ReserveAssistantTask(Guid taskId, Guid turnId, string assistant, string model, string account, string effort)
    {
        if (!IsUuidV4(taskId) || !IsUuidV4(turnId) || string.IsNullOrWhiteSpace(assistant) || string.IsNullOrWhiteSpace(model))
            throw new ArgumentException("Invalid task identity");
        var id = taskId.ToString("D");
        var route = JsonSerializer.Serialize(new { assistant, model, account, effort });
        lock (_gate)
        {
            Execute("BEGIN IMMEDIATE;");
            try
            {
                // A turn can admit only one task, even after completion or a
                // restart. Reusing it must not replay unconfirmed work.
                if (Exists($"SELECT 1 FROM assistant_tasks WHERE task_id={Sql(id)} OR turn_id={Sql(turnId.ToString("D"))};")) { Execute("COMMIT;"); return false; }
                Execute($"INSERT INTO assistant_tasks VALUES({Sql(id)},{Sql(turnId.ToString("D"))},{Sql(route)},{Sql(UtcNow())},'active');");
                AppendEvent("assistant_task_reserved", id, "active", new { turn_id = turnId.ToString("D"), assistant, model });
                Execute("COMMIT;"); return true;
            }
            catch { try { Execute("ROLLBACK;"); } catch (InvalidOperationException) { } throw; }
        }
    }

    public bool ReserveAssistantAction(Guid taskId, Guid actionId, string name, string argumentsJson)
    {
        if (!IsUuidV4(taskId) || !IsUuidV4(actionId) || string.IsNullOrWhiteSpace(name) || name.Length > 128 || argumentsJson.Length > 32768)
            throw new ArgumentException("Invalid action");
        using var json = JsonDocument.Parse(argumentsJson, new JsonDocumentOptions { MaxDepth = 12 });
        if (json.RootElement.ValueKind != JsonValueKind.Object) throw new ArgumentException("Action arguments must be an object");
        var task = taskId.ToString("D"); var id = actionId.ToString("D");
        lock (_gate)
        {
            Execute("BEGIN IMMEDIATE;");
            try
            {
                if (GetText($"SELECT status FROM assistant_tasks WHERE task_id={Sql(task)};") != "active" ||
                    Exists($"SELECT 1 FROM assistant_actions WHERE action_id={Sql(id)};")) { Execute("COMMIT;"); return false; }
                Execute($"INSERT INTO assistant_actions VALUES({Sql(id)},{Sql(task)},{Sql(name)},{Sql(argumentsJson)},{Sql(UtcNow())},'reserved',NULL,NULL);");
                AppendEvent("assistant_action_reserved", task, "reserved", new { action_id = id, name });
                Execute("COMMIT;"); return true;
            }
            catch { try { Execute("ROLLBACK;"); } catch (InvalidOperationException) { } throw; }
        }
    }

    public bool TransitionAssistantAction(Guid actionId, string expected, string next, string? error = null, string? resultJson = null)
    {
        var allowed = (expected, next) is ("reserved", "running" or "cancelled" or "rejected") or
            ("running", "completed" or "failed" or "uncertain" or "cancelled");
        if (!IsUuidV4(actionId) || !allowed || error?.Length > 1000 || resultJson?.Length > 262144) throw new ArgumentException("Invalid action transition");
        if (resultJson is not null) { using var resultDoc = JsonDocument.Parse(resultJson); }
        var id = actionId.ToString("D");
        lock (_gate)
        {
            Execute("BEGIN IMMEDIATE;");
            try
            {
                if (GetText($"SELECT status FROM assistant_actions WHERE action_id={Sql(id)};") != expected) { Execute("COMMIT;"); return false; }
                Execute($"UPDATE assistant_actions SET status={Sql(next)},error={(error is null ? "NULL" : Sql(error))},result_json={(resultJson is null ? "NULL" : Sql(resultJson))} WHERE action_id={Sql(id)};");
                AppendEvent("assistant_action_transition", id, next, new { previous = expected, error });
                Execute("COMMIT;"); return true;
            }
            catch { try { Execute("ROLLBACK;"); } catch (InvalidOperationException) { } throw; }
        }
    }

    public string? AssistantActionStatus(Guid actionId)
    {
        lock (_gate) return GetText($"SELECT status FROM assistant_actions WHERE action_id={Sql(actionId.ToString("D"))};");
    }

    public bool FinishAssistantTask(Guid taskId, string outcome)
    {
        if (!IsUuidV4(taskId) || outcome is not ("completed" or "cancelled" or "failed" or "timeout_unknown")) throw new ArgumentException("Invalid task outcome");
        var id = taskId.ToString("D");
        lock (_gate)
        {
            Execute("BEGIN IMMEDIATE;");
            try
            {
                if (GetText($"SELECT status FROM assistant_tasks WHERE task_id={Sql(id)};") != "active") { Execute("COMMIT;"); return false; }
                Execute($"UPDATE assistant_actions SET status='cancelled',error='task_ended' WHERE task_id={Sql(id)} AND status='reserved'; UPDATE assistant_tasks SET status={Sql(outcome)} WHERE task_id={Sql(id)};");
                AppendEvent("assistant_task_finished", id, outcome, new { });
                Execute("COMMIT;"); return true;
            }
            catch { try { Execute("ROLLBACK;"); } catch (InvalidOperationException) { } throw; }
        }
    }

    public void RecoverAssistantActions()
    {
        lock (_gate)
        {
            Execute("BEGIN IMMEDIATE;");
            try
            {
                Execute("UPDATE assistant_actions SET status='uncertain',error='host_restarted' WHERE status='running'; UPDATE assistant_actions SET status='cancelled',error='host_restarted' WHERE status='reserved'; UPDATE assistant_tasks SET status='interrupted' WHERE status='active';");
                AppendEvent("assistant_recovery", null, "interrupted", new { });
                Execute("COMMIT;");
            }
            catch { try { Execute("ROLLBACK;"); } catch (InvalidOperationException) { } throw; }
        }
    }

    public void OpenPrompt(Guid promptId, string assistant, string model, string account, string effort)
    {
        if (!IsUuidV4(promptId) || assistant is not ("local" or "codex") || string.IsNullOrWhiteSpace(model))
            throw new ArgumentException("Only a valid assistant prompt can be correlated.", nameof(promptId));
        var id = promptId.ToString("D");
        var now = UtcNow();
        var route = JsonSerializer.Serialize(new { assistant, model, account, effort });
        lock (_gate)
        {
            Execute("BEGIN IMMEDIATE;");
            try
            {
                if (Exists($"SELECT 1 FROM prompts WHERE prompt_id = {Sql(id)};"))
                {
                    AppendEvent("prompt_rejected", id, "duplicate_prompt_id", new { });
                    Execute("COMMIT;");
                    throw new InvalidOperationException("The prompt ID is already reserved.");
                }
                Execute($"""
                    INSERT INTO prompts(prompt_id, created_utc, route_json, status)
                        VALUES({Sql(id)}, {Sql(now)}, {Sql(route)}, 'open');
                    """);
                AppendEvent("prompt_opened", id, "open", new { route });
                Execute("COMMIT;");
            }
            catch
            {
                try { Execute("ROLLBACK;"); } catch (InvalidOperationException) { }
                throw;
            }
        }
    }

    public string? AcceptReply(JettReplyRequest request)
    {
        var replyId = request.ReplyId.ToString("D");
        var promptId = request.PromptId.ToString("D");
        var now = UtcNow();
        var textJson = JsonSerializer.Serialize(request.Text);
        string? error = null;
        lock (_gate)
        {
            Execute("BEGIN IMMEDIATE;");
            try
            {
                if (Exists($"SELECT 1 FROM replies WHERE reply_id = {Sql(replyId)};"))
                    error = "duplicate_reply_id";
                else if (GetText($"SELECT status FROM prompts WHERE prompt_id = {Sql(promptId)};") is not "open")
                    error = Exists($"SELECT 1 FROM prompts WHERE prompt_id = {Sql(promptId)};") ? "stale_prompt" : "uncorrelated_prompt";
                else if (request.Actions.Any(action => Exists($"SELECT 1 FROM actions WHERE action_id = {Sql(action.ActionId.ToString("D"))};")))
                    error = "duplicate_action_id";

                if (error is not null)
                {
                    AppendEvent("reply_rejected", promptId, error, new
                    {
                        reply_id = replyId,
                        action_ids = request.Actions.Select(x => x.ActionId.ToString("D")).ToArray()
                    });
                    Execute("COMMIT;");
                    return error;
                }

                Execute($"""
                    INSERT INTO replies(reply_id, prompt_id, text_json, created_utc, status, error)
                        VALUES({Sql(replyId)}, {Sql(promptId)}, {Sql(textJson)}, {Sql(now)}, 'pending', NULL);
                    UPDATE prompts SET status = 'replied' WHERE prompt_id = {Sql(promptId)} AND status = 'open';
                    """);
                AppendEvent("reply_accepted", promptId, "pending", new
                {
                    reply_id = replyId,
                    action_ids = request.Actions.Select(x => x.ActionId.ToString("D")).ToArray()
                });
                foreach (var action in request.Actions)
                {
                    var arguments = action.Name == "desktop.list"
                        ? JsonSerializer.Serialize(new { folder_token = action.ItemToken })
                        : JsonSerializer.Serialize(new { item_token = action.ItemToken });
                    Execute($"""
                        INSERT INTO actions(action_id, reply_id, prompt_id, action_name, arguments_json, created_utc, status, error, evidence_json)
                            VALUES({Sql(action.ActionId.ToString("D"))}, {Sql(replyId)}, {Sql(promptId)}, {Sql(action.Name)}, {Sql(arguments)}, {Sql(now)}, 'rejected', 'backend_blocked', NULL);
                        """);
                    AppendEvent("action_rejected", promptId, "backend_blocked", new
                    {
                        reply_id = replyId,
                        action_id = action.ActionId.ToString("D"),
                        action_name = action.Name
                    });
                }
                Execute("COMMIT;");
                return null;
            }
            catch
            {
                try { Execute("ROLLBACK;"); } catch (InvalidOperationException) { }
                throw;
            }
        }
    }

    public IReadOnlyList<JettConversationReply> ReadJettReplies()
    {
        lock (_gate)
        {
            var result = Prepare(_database,
                "SELECT reply_id, prompt_id, text_json, created_utc, status, error FROM replies ORDER BY created_utc, reply_id;",
                -1, out var statement, IntPtr.Zero);
            Check(result);
            try
            {
                var rows = new List<JettConversationReply>();
                while ((result = Step(statement)) == Row)
                {
                    var textJson = Marshal.PtrToStringUTF8(ColumnText(statement, 2)) ?? "\"\"";
                    var text = JsonSerializer.Deserialize<string>(textJson) ?? "";
                    rows.Add(new(Guid.ParseExact(ColumnString(statement, 0), "D"),
                        Guid.ParseExact(ColumnString(statement, 1), "D"), text,
                        ColumnString(statement, 4), ColumnString(statement, 3),
                        Marshal.PtrToStringUTF8(ColumnText(statement, 5))));
                }
                if (result != 101) Throw(result);
                return rows;
            }
            finally { Finalize(statement); }
        }
    }

    public IReadOnlyList<JettConversationAction> ReadJettActions()
    {
        lock (_gate)
        {
            var result = Prepare(_database,
                "SELECT action_id, reply_id, prompt_id, action_name, status, error FROM actions ORDER BY created_utc, action_id;",
                -1, out var statement, IntPtr.Zero);
            Check(result);
            try
            {
                var rows = new List<JettConversationAction>();
                while ((result = Step(statement)) == Row)
                    rows.Add(new(Guid.ParseExact(ColumnString(statement, 0), "D"),
                        Guid.ParseExact(ColumnString(statement, 1), "D"),
                        Guid.ParseExact(ColumnString(statement, 2), "D"),
                        ColumnString(statement, 3), ColumnString(statement, 4),
                        Marshal.PtrToStringUTF8(ColumnText(statement, 5))));
                if (result != Done) Throw(result);
                return rows;
            }
            finally { Finalize(statement); }
        }
    }

    internal IReadOnlyList<AssistantHistoryItem> ReadAssistantHistory()
    {
        lock (_gate)
        {
            var result = Prepare(_database, "SELECT a.action_id,a.task_id,t.turn_id,a.action_name,a.status,a.error,a.created_utc FROM assistant_actions a JOIN assistant_tasks t ON t.task_id=a.task_id ORDER BY a.created_utc DESC,a.action_id DESC LIMIT 100;", -1, out var statement, IntPtr.Zero);
            Check(result);
            try
            {
                var rows = new List<AssistantHistoryItem>();
                while ((result = Step(statement)) == Row)
                    rows.Add(new(ColumnString(statement, 0), ColumnString(statement, 1), ColumnString(statement, 2), ColumnString(statement, 3), ColumnString(statement, 4), Marshal.PtrToStringUTF8(ColumnText(statement, 5)), ColumnString(statement, 6)));
                if (result != Done) Throw(result);
                return rows;
            }
            finally { Finalize(statement); }
        }
    }

    public object? ReadReplyStatus(Guid replyId)
    {
        lock (_gate)
        {
            var statement = PrepareRow($"SELECT prompt_id, status, error, created_utc FROM replies WHERE reply_id = {Sql(replyId.ToString("D"))};");
            if (statement == IntPtr.Zero) return null;
            try
            {
                return new
                {
                    reply_id = replyId.ToString("D"),
                    correlated_prompt_id = ColumnString(statement, 0),
                    status = ColumnString(statement, 1),
                    error = Marshal.PtrToStringUTF8(ColumnText(statement, 2)),
                    created_at = ColumnString(statement, 3),
                    surfaces = ReadSurfaceAcksLocked(replyId)
                };
            }
            finally { Finalize(statement); }
        }
    }

    public bool AcknowledgeSurface(Guid replyId, string surface)
    {
        if (surface != "windows" && !(surface.StartsWith("android:", StringComparison.Ordinal)
            && surface.Length <= 80 && surface.AsSpan(8).ToString().All(c => char.IsAsciiLetterOrDigit(c) || c is '-' or '_')))
            throw new ArgumentException("Invalid display surface.", nameof(surface));
        var id = replyId.ToString("D");
        lock (_gate)
        {
            Execute("BEGIN IMMEDIATE;");
            try
            {
                if (!Exists($"SELECT 1 FROM replies WHERE reply_id = {Sql(id)};"))
                {
                    Execute("COMMIT;");
                    return false;
                }
                if (Exists($"SELECT 1 FROM surface_acks WHERE reply_id = {Sql(id)} AND surface = {Sql(surface)};"))
                {
                    Execute("COMMIT;");
                    return true;
                }
                var now = UtcNow();
                Execute($"""
                    INSERT INTO surface_acks(reply_id, surface, acknowledged_utc)
                        VALUES({Sql(id)}, {Sql(surface)}, {Sql(now)});
                    """);
                AppendEvent("surface_ack", id, "displayed", new { surface });
                if (surface == "windows")
                    Execute($"UPDATE replies SET status = 'delivered', error = NULL WHERE reply_id = {Sql(id)};");
                Execute("COMMIT;");
                return true;
            }
            catch
            {
                try { Execute("ROLLBACK;"); } catch (InvalidOperationException) { }
                throw;
            }
        }
    }

    private IReadOnlyList<string> ReadSurfaceAcksLocked(Guid replyId)
    {
        var result = Prepare(_database,
            $"SELECT surface FROM surface_acks WHERE reply_id = {Sql(replyId.ToString("D"))} ORDER BY acknowledged_utc, surface;",
            -1, out var statement, IntPtr.Zero);
        Check(result);
        try
        {
            var surfaces = new List<string>();
            while ((result = Step(statement)) == Row) surfaces.Add(ColumnString(statement, 0));
            if (result != Done) Throw(result);
            return surfaces;
        }
        finally { Finalize(statement); }
    }

    public object? ReadActionStatus(Guid actionId)
    {
        lock (_gate)
        {
            var statement = PrepareRow($"SELECT reply_id, prompt_id, action_name, status, error, evidence_json FROM actions WHERE action_id = {Sql(actionId.ToString("D"))};");
            if (statement == IntPtr.Zero) return null;
            try
            {
                var evidence = Marshal.PtrToStringUTF8(ColumnText(statement, 5));
                JsonElement? evidenceElement = null;
                if (evidence is not null)
                {
                    using var document = JsonDocument.Parse(evidence);
                    evidenceElement = document.RootElement.Clone();
                }
                return new
                {
                    action_id = actionId.ToString("D"),
                    reply_id = ColumnString(statement, 0),
                    correlated_prompt_id = ColumnString(statement, 1),
                    name = ColumnString(statement, 2),
                    status = ColumnString(statement, 3),
                    error = Marshal.PtrToStringUTF8(ColumnText(statement, 4)),
                    evidence = evidenceElement
                };
            }
            finally { Finalize(statement); }
        }
    }

    public void RecordGrant(bool enabled, DateTimeOffset? expiresUtc)
    {
        if (enabled != expiresUtc.HasValue)
            throw new ArgumentException("An enabled grant requires an expiry and a revoked grant must not have one.");
        var eventId = Guid.NewGuid().ToString("D");
        var now = DateTimeOffset.UtcNow.ToString("O", System.Globalization.CultureInfo.InvariantCulture);
        var outcome = enabled ? "enabled" : "revoked";
        var expiry = enabled
            ? expiresUtc!.Value.ToString("O", System.Globalization.CultureInfo.InvariantCulture)
            : "";
        var details = enabled
            ? $"{{\"source\":\"windows\",\"scopes\":[\"desktop.read\",\"desktop.open\"],\"expires_utc\":\"{expiry}\"}}"
            : "{\"source\":\"windows\",\"scopes\":[\"desktop.read\",\"desktop.open\"]}";
        lock (_gate)
            Execute($"""
                INSERT INTO events(event_id, happened_utc, event_type, correlation_id, outcome, details_json)
                    VALUES('{eventId}', '{now}', 'desktop_grant', NULL, '{outcome}', '{details}');
                """);
    }



    private void AppendEvent(string eventType, string? correlationId, string outcome, object details)
    {
        var eventId = Guid.NewGuid().ToString("D");
        var happened = UtcNow();
        var detailsJson = JsonSerializer.Serialize(details);
        Execute($"""
            INSERT INTO events(event_id, happened_utc, event_type, correlation_id, outcome, details_json)
                VALUES({Sql(eventId)}, {Sql(happened)}, {Sql(eventType)},
                    {(correlationId is null ? "NULL" : Sql(correlationId))}, {Sql(outcome)}, {Sql(detailsJson)});
            """);
    }

    private IntPtr PrepareRow(string sql)
    {
        var result = Prepare(_database, sql, -1, out var statement, IntPtr.Zero);
        Check(result);
        result = Step(statement);
        if (result == Done)
        {
            Finalize(statement);
            return IntPtr.Zero;
        }
        if (result != Row)
        {
            Finalize(statement);
            Throw(result);
        }
        return statement;
    }

    private string? GetText(string sql)
    {
        var statement = PrepareRow(sql);
        if (statement == IntPtr.Zero) return null;
        try { return Marshal.PtrToStringUTF8(ColumnText(statement, 0)); }
        finally { Finalize(statement); }
    }

    // 100.029 (Worker C) — true when the events table already has
    // the `level` column (fresh DBs from the CREATE TABLE above, or
    // an already-migrated DB). Reads PRAGMA table_info, whose rows
    // are (cid, name, type, notnull, dflt_value, pk) — column 1 is
    // the name. Used only by the constructor migration above.
    private bool EventsHasLevelColumn()
    {
        var result = Prepare(_database, "PRAGMA table_info(events);", -1, out var statement, IntPtr.Zero);
        Check(result);
        try
        {
            while ((result = Step(statement)) == Row)
            {
                if (ColumnString(statement, 1) == "level") return true;
            }
            if (result != Done) Throw(result);
            return false;
        }
        finally { Finalize(statement); }
    }

    private bool Exists(string sql)
    {
        var statement = PrepareRow(sql);
        if (statement == IntPtr.Zero) return false;
        Finalize(statement);
        return true;
    }

    private static string ColumnString(IntPtr statement, int column) =>
        Marshal.PtrToStringUTF8(ColumnText(statement, column)) ?? "";

    private static bool IsUuidV4(Guid id)
    {
        var value = id.ToString("D");
        return id != Guid.Empty && value[14] == '4' && value[19] is '8' or '9' or 'a' or 'b';
    }

    private static string UtcNow() => DateTimeOffset.UtcNow.ToString("O", System.Globalization.CultureInfo.InvariantCulture);

    private static string Sql(string value) => "'" + value.Replace("'", "''", StringComparison.Ordinal) + "'";

    private void Execute(string sql)
    {
        var result = Exec(_database, sql, IntPtr.Zero, IntPtr.Zero, out var error);
        if (result == Ok) return;
        string? message = error == IntPtr.Zero ? Marshal.PtrToStringUTF8(ErrorMessage(_database)) : Marshal.PtrToStringUTF8(error);
        if (error != IntPtr.Zero) Free(error);
        throw new InvalidOperationException($"Jett journal SQLite error {result}: {message}");
    }

    private void Check(int result)
    {
        if (result != Ok) Throw(result);
    }

    private void Throw(int result) =>
        throw new InvalidOperationException($"Jett journal SQLite error {result}: {ErrorMessage(_database)}");

    public void Dispose()
    {
        lock (_gate)
        {
            if (_database == IntPtr.Zero) return;
            var database = _database;
            _database = IntPtr.Zero;
            var result = Close(database);
            if (result != Ok)
                throw new InvalidOperationException($"Jett journal SQLite close error {result}.");
        }
    }

    [DllImport("winsqlite3.dll", EntryPoint = "sqlite3_open_v2", CallingConvention = CallingConvention.Cdecl)]
    private static extern int Open([MarshalAs(UnmanagedType.LPUTF8Str)] string filename, out IntPtr database,
        int flags, IntPtr vfs);

    [DllImport("winsqlite3.dll", EntryPoint = "sqlite3_busy_timeout", CallingConvention = CallingConvention.Cdecl)]
    private static extern int BusyTimeout(IntPtr database, int milliseconds);

    [DllImport("winsqlite3.dll", EntryPoint = "sqlite3_exec", CallingConvention = CallingConvention.Cdecl)]
    private static extern int Exec(IntPtr database, [MarshalAs(UnmanagedType.LPUTF8Str)] string sql,
        IntPtr callback, IntPtr argument, out IntPtr error);

    [DllImport("winsqlite3.dll", EntryPoint = "sqlite3_prepare_v2", CallingConvention = CallingConvention.Cdecl)]
    private static extern int Prepare(IntPtr database, [MarshalAs(UnmanagedType.LPUTF8Str)] string sql,
        int bytes, out IntPtr statement, IntPtr tail);

    [DllImport("winsqlite3.dll", EntryPoint = "sqlite3_step", CallingConvention = CallingConvention.Cdecl)]
    private static extern int Step(IntPtr statement);

    [DllImport("winsqlite3.dll", EntryPoint = "sqlite3_column_text", CallingConvention = CallingConvention.Cdecl)]
    private static extern IntPtr ColumnText(IntPtr statement, int column);

    [DllImport("winsqlite3.dll", EntryPoint = "sqlite3_finalize", CallingConvention = CallingConvention.Cdecl)]
    private static extern int Finalize(IntPtr statement);

    [DllImport("winsqlite3.dll", EntryPoint = "sqlite3_close_v2", CallingConvention = CallingConvention.Cdecl)]
    private static extern int Close(IntPtr database);

    [DllImport("winsqlite3.dll", EntryPoint = "sqlite3_errmsg", CallingConvention = CallingConvention.Cdecl)]
    private static extern IntPtr ErrorMessage(IntPtr database);

    [DllImport("winsqlite3.dll", EntryPoint = "sqlite3_free", CallingConvention = CallingConvention.Cdecl)]
    private static extern void Free(IntPtr value);
}
