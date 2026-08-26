using System.Diagnostics;
using SlotAutoPlay.Models;

namespace SlotAutoPlay.Automation;

public sealed class SlotSessionRunner
{
    private readonly PlayConfig config;
    private readonly PlayOptions options;
    private int parsedRoundCount;

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

        while (!cancellationToken.IsCancellationRequested &&
               Stopwatch.GetElapsedTime(startedAt) < config.SessionMaxDuration)
        {
            var result = await RunOrIdleClickAsync(
                automation,
                cancellationToken).ConfigureAwait(false);

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

    public async Task<NormalizedRoundResult?> RunOrIdleClickAsync(
        HttpBrowserAutomation automation,
        CancellationToken cancellationToken)
    {
        await Task.Delay(config.IdleDuration, cancellationToken)
            .ConfigureAwait(false);

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

        await RunOrIdleClickDelayAsync(automation, cancellationToken)
            .ConfigureAwait(false);

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
                return current + 1 == limit;
            }
        }
    }

    private static string Sanitize(string value)
    {
        var invalid = Path.GetInvalidFileNameChars();
        var result = string.Concat(value.Select(character =>
            invalid.Contains(character) ? '_' : character));

        return string.IsNullOrWhiteSpace(result) ? "game" : result;
    }
}