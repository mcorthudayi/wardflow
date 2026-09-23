using Microsoft.AspNetCore.SignalR;
using Npgsql;
using NpgsqlTypes;

var builder = WebApplication.CreateBuilder(args);

var connectionString = builder.Configuration.GetConnectionString("WardFlow")
    ?? throw new InvalidOperationException("ConnectionStrings:WardFlow is not configured. Start the API with scripts/api.sh.");

builder.Services.AddSingleton(NpgsqlDataSource.Create(connectionString));
builder.Services.AddSingleton<OpsRepository>();
builder.Services.AddSignalR();
builder.Services.AddHostedService<OverviewBroadcaster>();
builder.Services.AddProblemDetails();

var app = builder.Build();

app.UseExceptionHandler();
app.Use(async (context, next) =>
{
    context.Response.Headers.XContentTypeOptions = "nosniff";
    context.Response.Headers.XFrameOptions = "DENY";
    context.Response.Headers.CacheControl = "no-store";
    await next();
});

app.MapGet("/health", () => Results.Ok(new { status = "ok" }));

app.MapGet("/api/overview", async (OpsRepository repository, CancellationToken ct) =>
    Results.Ok(await repository.GetOverviewAsync(ct)));

app.MapGet("/api/kpis", async (int? hours, OpsRepository repository, CancellationToken ct) =>
    Results.Ok(await repository.GetSnapshotsAsync(Math.Clamp(hours ?? 24, 1, 168), ct)));

app.MapGet("/api/boarding", async (OpsRepository repository, CancellationToken ct) =>
    Results.Ok(await repository.GetBoardingAsync(await repository.GetSimTimeAsync(ct), ct)));

app.MapGet("/api/events/recent", async (int? limit, OpsRepository repository, CancellationToken ct) =>
    Results.Ok(await repository.GetRecentEventsAsync(Math.Clamp(limit ?? 25, 1, 200), ct)));

app.MapHub<OpsHub>("/hubs/ops");

app.Run();

record Snapshot(
    DateTime CapturedAt,
    int EdCensus,
    int EdBoarding,
    int BedsOccupied,
    int BedsTotal,
    decimal Occupancy,
    int ArrivalsLastHour,
    int DischargesLastHour,
    decimal? AvgBoardingMinutes);

record UnitStatus(string Code, string Name, string Kind, int Capacity, long Occupied, long BoardingForUnit);

record Alert(string Severity, string Title, string Detail);

record BoardingPatient(string VisitId, string? TargetUnit, int? Acuity, DateTime DecisionAt, double WaitingMinutes);

record RecentEvent(string EventType, DateTime OccurredAt, string VisitId, string CurrentUnit, string Status);

record Overview(
    DateTime? SimTime,
    Snapshot? Latest,
    IReadOnlyList<UnitStatus> Units,
    IReadOnlyList<Alert> Alerts,
    IReadOnlyList<BoardingPatient> Boarding,
    IReadOnlyList<RecentEvent> RecentEvents,
    long DeadLetters,
    long EventsProcessed);

static class AlertRules
{
    public static IReadOnlyList<Alert> Build(Snapshot? latest, IReadOnlyList<UnitStatus> units, long deadLetters)
    {
        var alerts = new List<Alert>();

        foreach (var unit in units)
        {
            if (unit.Capacity <= 0)
                continue;

            var ratio = (double)unit.Occupied / unit.Capacity;
            if (unit.Kind == "emergency")
            {
                if (ratio >= 1.0)
                    alerts.Add(new Alert("critical", $"{unit.Name} over capacity", $"{unit.Occupied} patients for {unit.Capacity} treatment spaces"));
                else if (ratio >= 0.8)
                    alerts.Add(new Alert("warning", $"{unit.Name} crowding", $"{unit.Occupied} of {unit.Capacity} treatment spaces in use"));
            }
            else if (ratio >= 0.95)
            {
                alerts.Add(new Alert("critical", $"{unit.Name} at {ratio * 100:0}%", $"{unit.Occupied}/{unit.Capacity} beds, {unit.BoardingForUnit} patients waiting for a bed"));
            }
            else if (ratio >= 0.85)
            {
                alerts.Add(new Alert("warning", $"{unit.Name} at {ratio * 100:0}%", $"{unit.Occupied}/{unit.Capacity} beds occupied"));
            }
        }

        if (latest is not null)
        {
            if (latest.EdBoarding >= 8)
                alerts.Add(new Alert("critical", "ED boarding", $"{latest.EdBoarding} admitted patients are waiting in the ED for a bed"));
            else if (latest.EdBoarding >= 4)
                alerts.Add(new Alert("warning", "ED boarding", $"{latest.EdBoarding} admitted patients are waiting in the ED for a bed"));

            if (latest.AvgBoardingMinutes is decimal minutes)
            {
                if (minutes >= 240)
                    alerts.Add(new Alert("critical", "Long boarding times", $"Average boarding time {minutes:0} min over the last 24 h"));
                else if (minutes >= 120)
                    alerts.Add(new Alert("warning", "Long boarding times", $"Average boarding time {minutes:0} min over the last 24 h"));
            }
        }

        if (deadLetters > 0)
            alerts.Add(new Alert("info", "Rejected events", $"{deadLetters} events are in the dead-letter table"));

        return alerts
            .OrderBy(alert => alert.Severity switch { "critical" => 0, "warning" => 1, _ => 2 })
            .ToList();
    }
}

sealed class OpsRepository(NpgsqlDataSource db)
{
    private const string SnapshotColumns = """
        captured_at, ed_census, ed_boarding, beds_occupied, beds_total, occupancy,
        arrivals_last_hour, discharges_last_hour, avg_boarding_minutes
        """;

    public async Task<string> GetVersionAsync(CancellationToken ct)
    {
        await using var command = db.CreateCommand("""
            SELECT concat_ws('|',
                (SELECT max(captured_at) FROM ops.kpi_snapshots),
                (SELECT count(*) FROM ops.processed_events),
                (SELECT count(*) FROM ops.dead_letter))
            """);
        return (await command.ExecuteScalarAsync(ct))?.ToString() ?? string.Empty;
    }

    public async Task<DateTime?> GetSimTimeAsync(CancellationToken ct)
    {
        await using var command = db.CreateCommand("SELECT max(occurred_at) FROM ops.processed_events");
        return await command.ExecuteScalarAsync(ct) is DateTime value ? value : null;
    }

    public async Task<Overview> GetOverviewAsync(CancellationToken ct)
    {
        var latest = await GetLatestSnapshotAsync(ct);
        var units = await GetUnitsAsync(ct);
        var simTime = await GetSimTimeAsync(ct);
        var boarding = await GetBoardingAsync(simTime, ct);
        var recent = await GetRecentEventsAsync(15, ct);
        var deadLetters = await CountAsync("SELECT count(*) FROM ops.dead_letter", ct);
        var processed = await CountAsync("SELECT count(*) FROM ops.processed_events", ct);
        return new Overview(simTime, latest, units, AlertRules.Build(latest, units, deadLetters), boarding, recent, deadLetters, processed);
    }

    public async Task<Snapshot?> GetLatestSnapshotAsync(CancellationToken ct)
    {
        await using var command = db.CreateCommand($"""
            SELECT {SnapshotColumns}
            FROM ops.kpi_snapshots
            ORDER BY captured_at DESC
            LIMIT 1
            """);
        await using var reader = await command.ExecuteReaderAsync(ct);
        return await reader.ReadAsync(ct) ? ReadSnapshot(reader) : null;
    }

    public async Task<IReadOnlyList<Snapshot>> GetSnapshotsAsync(int hours, CancellationToken ct)
    {
        await using var command = db.CreateCommand($"""
            SELECT {SnapshotColumns}
            FROM ops.kpi_snapshots
            WHERE captured_at > (SELECT max(captured_at) FROM ops.kpi_snapshots) - make_interval(hours => @hours)
            ORDER BY captured_at
            """);
        command.Parameters.Add(new NpgsqlParameter("hours", NpgsqlDbType.Integer) { Value = hours });

        var rows = new List<Snapshot>();
        await using var reader = await command.ExecuteReaderAsync(ct);
        while (await reader.ReadAsync(ct))
            rows.Add(ReadSnapshot(reader));
        return rows;
    }

    public async Task<IReadOnlyList<UnitStatus>> GetUnitsAsync(CancellationToken ct)
    {
        await using var command = db.CreateCommand("""
            SELECT code, name, kind, capacity, occupied, boarding_for_unit
            FROM ops.v_unit_status
            ORDER BY kind, code
            """);
        var rows = new List<UnitStatus>();
        await using var reader = await command.ExecuteReaderAsync(ct);
        while (await reader.ReadAsync(ct))
        {
            rows.Add(new UnitStatus(
                reader.GetString(0),
                reader.GetString(1),
                reader.GetString(2),
                reader.GetInt32(3),
                reader.GetInt64(4),
                reader.GetInt64(5)));
        }
        return rows;
    }

    public async Task<IReadOnlyList<BoardingPatient>> GetBoardingAsync(DateTime? simTime, CancellationToken ct)
    {
        await using var command = db.CreateCommand("""
            SELECT visit_id, target_unit, acuity, decision_at
            FROM ops.visits
            WHERE status = 'boarding' AND decision_at IS NOT NULL
            ORDER BY decision_at
            LIMIT 25
            """);
        var rows = new List<BoardingPatient>();
        await using var reader = await command.ExecuteReaderAsync(ct);
        while (await reader.ReadAsync(ct))
        {
            var decisionAt = reader.GetDateTime(3);
            var waiting = simTime.HasValue ? Math.Round((simTime.Value - decisionAt).TotalMinutes, 1) : 0;
            rows.Add(new BoardingPatient(
                reader.GetString(0),
                reader.IsDBNull(1) ? null : reader.GetString(1),
                reader.IsDBNull(2) ? null : reader.GetInt32(2),
                decisionAt,
                waiting));
        }
        return rows;
    }

    public async Task<IReadOnlyList<RecentEvent>> GetRecentEventsAsync(int limit, CancellationToken ct)
    {
        await using var command = db.CreateCommand("""
            SELECT e.event_type, e.occurred_at, e.visit_id, v.current_unit, v.status
            FROM ops.processed_events e
            JOIN ops.visits v ON v.visit_id = e.visit_id
            ORDER BY e.occurred_at DESC
            LIMIT @limit
            """);
        command.Parameters.Add(new NpgsqlParameter("limit", NpgsqlDbType.Integer) { Value = limit });

        var rows = new List<RecentEvent>();
        await using var reader = await command.ExecuteReaderAsync(ct);
        while (await reader.ReadAsync(ct))
        {
            rows.Add(new RecentEvent(
                reader.GetString(0),
                reader.GetDateTime(1),
                reader.GetString(2),
                reader.GetString(3),
                reader.GetString(4)));
        }
        return rows;
    }

    private async Task<long> CountAsync(string sql, CancellationToken ct)
    {
        await using var command = db.CreateCommand(sql);
        return Convert.ToInt64(await command.ExecuteScalarAsync(ct));
    }

    private static Snapshot ReadSnapshot(NpgsqlDataReader reader) => new(
        reader.GetDateTime(0),
        reader.GetInt32(1),
        reader.GetInt32(2),
        reader.GetInt32(3),
        reader.GetInt32(4),
        reader.GetDecimal(5),
        reader.GetInt32(6),
        reader.GetInt32(7),
        reader.IsDBNull(8) ? null : reader.GetDecimal(8));
}

sealed class OpsHub(OpsRepository repository) : Hub
{
    public override async Task OnConnectedAsync()
    {
        await Clients.Caller.SendAsync("overview", await repository.GetOverviewAsync(Context.ConnectionAborted));
        await base.OnConnectedAsync();
    }
}

sealed class OverviewBroadcaster(OpsRepository repository, IHubContext<OpsHub> hub, ILogger<OverviewBroadcaster> logger)
    : BackgroundService
{
    protected override async Task ExecuteAsync(CancellationToken stoppingToken)
    {
        string? lastVersion = null;
        using var timer = new PeriodicTimer(TimeSpan.FromSeconds(1));

        while (await timer.WaitForNextTickAsync(stoppingToken))
        {
            try
            {
                var version = await repository.GetVersionAsync(stoppingToken);
                if (version == lastVersion)
                    continue;

                lastVersion = version;
                var overview = await repository.GetOverviewAsync(stoppingToken);
                await hub.Clients.All.SendAsync("overview", overview, stoppingToken);
            }
            catch (OperationCanceledException) when (stoppingToken.IsCancellationRequested)
            {
                break;
            }
            catch (Exception exception)
            {
                logger.LogWarning(exception, "Overview broadcast failed");
            }
        }
    }
}
