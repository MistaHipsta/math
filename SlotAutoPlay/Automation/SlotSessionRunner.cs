using System.Diagnostics;
using SlotAutoPlay.Models;

namespace SlotAutoPlay.Automation;

public sealed class SlotSessionRunner
{
    private readonly PlayConfig config;
    private readonly PlayOptions options;
    private int parsedRoundCount;
    private long sessionStartedTimestamp;
    private string? activeSessionId;

    public SlotSessionRunner(PlayConfig config, PlayOptions options)
    {
        this.config = config;
        this.options = options;
    }

    public async Task RunWorkerAsync(
        int workerNumber,
        CancellationToken cancellationToken)
    {
        while (!cancellationToken.IsCancellationRequested)
        {
            var sessionId =
                $"{Sanitize(config.Name)}-worker-{workerNumber:00}-{Guid.NewGuid():N}";

            try
            {
                var roundsLimitReached = await RunSessionAsync(
                    sessionId,
                    cancellationToken).ConfigureAwait(false);

                if (roundsLimitReached || options.IsDebugMode)
                {
                    break;
                }
            }
            catch (OperationCanceledException)
                when (cancellationToken.IsCancellationRequested)
            {
                break;
            }
            catch (Exception exception)
            {
                Console.Error.WriteLine($"[{sessionId}] session failed: {exception}");
            }

            if (cancellationToken.IsCancellationRequested)
            {
                break;
            }

            try
            {
                await Task.Delay(config.RestartDelay, cancellationToken)
                    .ConfigureAwait(false);
            }
            catch (OperationCanceledException)
            {
                break;
            }
        }
    }

    private async Task<bool> RunSessionAsync(
        string sessionId,
        CancellationToken cancellationToken)
    {
        await using var automation = await HttpBrowserAutomation.CreateAsync(
            config,
            options,
            sessionId,
            cancellationToken).ConfigureAwait(false);

        await automation.NavigateAndOpenAsync(cancellationToken)
            .ConfigureAwait(false);

        await automation.ScreenshotAsync("init", cancellationToken)
            .ConfigureAwait(false);

        if (options.IsDebugMode)
        {
            // Debug stops after navigation, configured page-load waits, and the initial screenshot.
            return false;
        }

        var startedAt = Stopwatch.GetTimestamp();
        sessionStartedTimestamp = startedAt;
        activeSessionId = sessionId;

        while (!cancellationToken.IsCancellationRequested &&
               Stopwatch.GetElapsedTime(startedAt) < config.SessionMaxDuration)
        {
            if (config.CollectOnly)
            {
                var collected = await RunCollectOnlyClickAsync(
                    automation,
                    cancellationToken).ConfigureAwait(false);

                if (!collected)
                {
                    Console.WriteLine(
                        $"[{sessionId}] Collect-only spin did not receive HTTP 200; " +
                        "restarting session.");
                    break;
                }

                if (TryCountCollectedRound())
                {
                    Console.WriteLine(
                        $"[{sessionId}] Collected round limit reached: " +
                        $"{config.RoundsOverride}.");
                    return true;
                }

                continue;
            }

            var result = await RunOrIdleClickAsync(
                automation,
                cancellationToken).ConfigureAwait(false);

            if (config.ResponseDriven && result is null)
            {
                Console.WriteLine(
                    $"[{sessionId}] Response-driven spin did not produce a result; " +
                    "restarting session.");
                break;
            }

            if (result?.Status == RoundParseStatus.Parsed &&
                TryCountParsedRound())
            {
                Console.WriteLine(
                    $"[{sessionId}] Parsed round limit reached: " +
                    $"{config.RoundsOverride}.");
                return true;
            }

            if (automation.ResponseActivityCount > 0 &&
                automation.LastResponseActivity is { } lastActivity &&
                DateTimeOffset.UtcNow - lastActivity >
                config.LastResponseTimeout)
            {
                Console.WriteLine(
                    $"[{sessionId}] LastResponseTimeout reached; restarting session.");
                break;
            }
        }

        return false;
    }

    private async Task<bool> RunCollectOnlyClickAsync(
        HttpBrowserAutomation automation,
        CancellationToken cancellationToken)
    {
        await automation.ClickAsync(
            config.SpinButton,
            "spin",
            cancellationToken).ConfigureAwait(false);

        // Collection is deliberately time-driven: network responses are captured
        // by Playwright listeners and must not gate the next click.
        await Task.Delay(
            TimeSpan.FromMilliseconds(800),
            cancellationToken).ConfigureAwait(false);

        return true;
    }

    public async Task<NormalizedRoundResult?> RunOrIdleClickAsync(
        HttpBrowserAutomation automation,
        CancellationToken cancellationToken)
    {
        if (!config.ResponseDriven)
        {
            await Task.Delay(config.IdleDuration, cancellationToken)
                .ConfigureAwait(false);
        }

        await automation.ClickAsync(
            config.SpinButton,
            "spin",
            cancellationToken).ConfigureAwait(false);

        NormalizedRoundResult? result = null;
        if (config.CaptureRoundData || config.DiagnosticNetwork)
        {
            result = await automation.WaitForSpinAsync(cancellationToken)
                .ConfigureAwait(false);
        }

        if (!config.ResponseDriven)
        {
            await RunOrIdleClickDelayAsync(automation, cancellationToken)
                .ConfigureAwait(false);
        }

        return result;
    }

    public async Task RunOrIdleClickDelayAsync(
        HttpBrowserAutomation automation,
        CancellationToken cancellationToken)
    {
        await Task.Delay(config.CheckClick, cancellationToken)
            .ConfigureAwait(false);

        await automation.ClickAsync(
            config.SpinButton,
            "check",
            cancellationToken).ConfigureAwait(false);
    }

    private bool TryCountCollectedRound()
    {
        if (config.RoundsOverride is not { } limit)
        {
            return false;
        }

        while (true)
        {
            var current = Volatile.Read(ref parsedRoundCount);
            if (current >= limit)
            {
                return false;
            }

            if (Interlocked.CompareExchange(
                    ref parsedRoundCount,
                    current + 1,
                    current) == current)
            {
                var newCount = current + 1;
                TryReportProgress(newCount, limit);
                return newCount == limit;
            }
        }
    }

    private bool TryCountParsedRound()
    {
        if (config.RoundsOverride is not { } limit)
        {
            return false;
        }

        while (true)
        {
            var current = Volatile.Read(ref parsedRoundCount);
            if (current >= limit)
            {
                return false;
            }

            if (Interlocked.CompareExchange(
                    ref parsedRoundCount,
                    current + 1,
                    current) == current)
            {
                var newCount = current + 1;
                TryReportProgress(newCount, limit);
                return newCount == limit;
            }
        }
    }

    private void TryReportProgress(int count, int limit)
    {
        if (count % 50 != 0 && count != limit)
        {
            return;
        }

        if (sessionStartedTimestamp == 0)
        {
            return;
        }

        var elapsed = Stopwatch.GetElapsedTime(sessionStartedTimestamp);
        var percent = (double)count / limit * 100;
        double? etaMinutes = null;
        if (count > 0)
        {
            var perRound = elapsed.TotalSeconds / count;
            etaMinutes = (limit - count) * perRound / 60;
        }

        var now = DateTimeOffset.Now.ToString("HH:mm:ss");
        var eta = etaMinutes is { } m
            ? $" ETA≈{m:F0} min"
            : "";
        Console.WriteLine(
            $"[{activeSessionId}] Progress: {count}/{limit} parsed " +
            $"({percent:F1}%) elapsed={elapsed.TotalMinutes:F1}m{eta} @ {now}");
    }

    private static string Sanitize(string value)
    {
        var invalid = Path.GetInvalidFileNameChars();
        var result = string.Concat(value.Select(character =>
            invalid.Contains(character) ? '_' : character));

        return string.IsNullOrWhiteSpace(result) ? "game" : result;
    }
}