using SlotAutoPlay.Automation;
using SlotAutoPlay.Configuration;
using SlotAutoPlay.Models;

namespace SlotAutoPlay;

internal static class Program
{
    public static async Task<int> Main(string[] args)
    {
        try
        {
            var options = ParseOptions(args);
            var provider = CreateProvider(args);
            var outputFolder = ResolveOutputFolder(provider);
            var jobsCount = ParseJobsOption(args, provider.JobsCount);
            var duration = ParseDurationOption(args);
            var rounds = ParseRoundsOption(args);
            var maxResponseBytes = ParseMaxResponseBytesOption(args);

            options = new PlayOptions
            {
                IsDebugMode = options.IsDebugMode,
                RandomizeClickPoint = options.RandomizeClickPoint,
                Headless = options.Headless,
                DurationOverride = duration,
                RoundsOverride = rounds,
                CaptureRoundData = options.CaptureRoundData,
                DiagnosticNetwork = options.DiagnosticNetwork,
                MaxResponseBytes = maxResponseBytes
            };

            ValidateGameMode(provider, options, jobsCount);

            var config = provider.Build(options, outputFolder, jobsCount);
            Validate(config);

            Directory.CreateDirectory(config.OutputFolder);
            Console.WriteLine(
                $"Starting {config.Name}; jobs={config.JobsCount}; " +
                $"mode={(options.IsDebugMode ? "debug" : "full")}; " +
                $"output={config.OutputFolder}");

            using var cancellationSource = new CancellationTokenSource();
            using var durationSource = options.DurationOverride is { } durationOverride
                ? new CancellationTokenSource(durationOverride)
                : null;
            using var linkedCancellationSource = durationSource is null
                ? null
                : CancellationTokenSource.CreateLinkedTokenSource(
                    cancellationSource.Token,
                    durationSource.Token);
            var workerCancellationToken =
                linkedCancellationSource?.Token ?? cancellationSource.Token;

            Console.CancelKeyPress += (_, eventArgs) =>
            {
                eventArgs.Cancel = true;
                if (!cancellationSource.IsCancellationRequested)
                {
                    Console.WriteLine("Cancellation requested; stopping workers.");
                    cancellationSource.Cancel();
                }
            };

            var runner = new SlotSessionRunner(config, options);
            var workers = Enumerable.Range(1, config.JobsCount)
                .Select(worker => RunWorkerSafelyAsync(
                    runner,
                    worker,
                    workerCancellationToken))
                .ToArray();

            await Task.WhenAll(workers).ConfigureAwait(false);
            return 0;
        }
        catch (OperationCanceledException)
        {
            return 0;
        }
        catch (Exception exception)
        {
            Console.Error.WriteLine(exception);
            return 1;
        }
    }

    private static async Task RunWorkerSafelyAsync(
        SlotSessionRunner runner,
        int workerNumber,
        CancellationToken cancellationToken)
    {
        try
        {
            await runner.RunWorkerAsync(workerNumber, cancellationToken)
                .ConfigureAwait(false);
        }
        catch (OperationCanceledException)
            when (cancellationToken.IsCancellationRequested)
        {
        }
        catch (Exception exception)
        {
            Console.Error.WriteLine(
                $"Worker {workerNumber} stopped unexpectedly: {exception}");
        }
    }

    private static PlayOptions ParseOptions(string[] args) =>
        new()
        {
            IsDebugMode = HasFlag(args, "--debug"),
            RandomizeClickPoint = HasFlag(args, "--random-click"),
            Headless = !HasFlag(args, "--headed"),
            CaptureRoundData = HasFlag(args, "--capture-round-data"),
            DiagnosticNetwork = HasFlag(args, "--diagnostic-network")
        };

    internal static int ParseMaxResponseBytesOption(string[] args)
    {
        var value = GetOptionValue(args, "--max-response-bytes");
        if (value is null)
        {
            return 1_048_576;
        }

        if (!int.TryParse(value, System.Globalization.NumberStyles.Integer,
                System.Globalization.CultureInfo.InvariantCulture, out var parsed) ||
            parsed < 1_024 || parsed > 16 * 1024 * 1024)
        {
            throw new ArgumentException(
                "--max-response-bytes must be an integer between 1024 and 16777216.");
        }

        return parsed;
    }

    internal static int ParseJobsOption(string[] args, int defaultValue)
    {
        for (var index = 0; index < args.Length; index++)
        {
            if (!string.Equals(
                    args[index],
                    "--jobs",
                    StringComparison.OrdinalIgnoreCase))
            {
                continue;
            }

            if (index == args.Length - 1 ||
                args[index + 1].StartsWith("--", StringComparison.Ordinal))
            {
                throw new ArgumentException(
                    "--jobs requires a positive integer value, for example: --jobs 2.");
            }

            if (!int.TryParse(
                    args[index + 1],
                    System.Globalization.NumberStyles.Integer,
                    System.Globalization.CultureInfo.InvariantCulture,
                    out var parsed) ||
                parsed <= 0)
            {
                throw new ArgumentException(
                    $"Invalid --jobs value '{args[index + 1]}'. " +
                    "--jobs must be a positive integer.");
            }

            return parsed;
        }

        return defaultValue;
    }

    internal static int? ParseRoundsOption(string[] args)
    {
        for (var index = 0; index < args.Length; index++)
        {
            if (!string.Equals(
                    args[index],
                    "--rounds",
                    StringComparison.OrdinalIgnoreCase))
            {
                continue;
            }

            if (index == args.Length - 1 ||
                args[index + 1].StartsWith("--", StringComparison.Ordinal))
            {
                throw new ArgumentException(
                    "--rounds requires a positive integer value, " +
                    "for example: --rounds 1000.");
            }

            if (!int.TryParse(
                    args[index + 1],
                    System.Globalization.NumberStyles.Integer,
                    System.Globalization.CultureInfo.InvariantCulture,
                    out var parsed) ||
                parsed <= 0)
            {
                throw new ArgumentException(
                    $"Invalid --rounds value '{args[index + 1]}'. " +
                    "--rounds must be a positive integer.");
            }

            return parsed;
        }

        return null;
    }

    internal static TimeSpan? ParseDurationOption(string[] args)
    {
        for (var index = 0; index < args.Length; index++)
        {
            if (!string.Equals(
                    args[index],
                    "--duration-seconds",
                    StringComparison.OrdinalIgnoreCase))
            {
                continue;
            }

            if (index == args.Length - 1 ||
                args[index + 1].StartsWith("--", StringComparison.Ordinal))
            {
                throw new ArgumentException(
                    "--duration-seconds requires a positive integer value, " +
                    "for example: --duration-seconds 60.");
            }

            if (!int.TryParse(
                    args[index + 1],
                    System.Globalization.NumberStyles.Integer,
                    System.Globalization.CultureInfo.InvariantCulture,
                    out var parsed) ||
                parsed <= 0)
            {
                throw new ArgumentException(
                    $"Invalid --duration-seconds value '{args[index + 1]}'. " +
                    "--duration-seconds must be a positive integer.");
            }

            return TimeSpan.FromSeconds(parsed);
        }

        return null;
    }

    private static GameConfigProvider CreateProvider(string[] args)
    {
        var game = GetOptionValue(args, "--game") ?? "floating-dragon";

        return game.Trim().ToLowerInvariant() switch
        {
            "floating-dragon" or "floatingdragon" => new FloatingDragonConfig(),
            "moon-sisters" or "moonsisters" => new MoonSistersConfig(),
            _ => throw new ArgumentException(
                $"Unsupported --game value '{game}'. " +
                "Supported values: floating-dragon, moon-sisters.")
        };
    }

    private static string ResolveOutputFolder(GameConfigProvider provider)
    {
        var basePath = ResolveBasePath();

        return string.IsNullOrWhiteSpace(provider.OutputSubfolder)
            ? basePath
            : Path.Combine(basePath, provider.OutputSubfolder);
    }

    private static string ResolveBasePath()
    {
        var configured = Environment.GetEnvironmentVariable(
            "SLOT_AUTOPLAY_BASE_PATH");

        return Path.GetFullPath(
            string.IsNullOrWhiteSpace(configured)
                ? Path.Combine("output", "slotautoplay")
                : configured);
    }

    private static void ValidateGameMode(
        GameConfigProvider provider,
        PlayOptions options,
        int jobsCount)
    {
        if (!provider.DebugOnly)
        {
            return;
        }

        if (!options.IsDebugMode ||
            options.Headless ||
            jobsCount != 1)
        {
            throw new ArgumentException(
                $"{provider.Name} is restricted to one headed debug session: " +
                "use --game moon-sisters --debug --headed --jobs 1.");
        }
    }

    private static bool HasFlag(string[] args, string flag) =>
        args.Any(argument => string.Equals(
            argument,
            flag,
            StringComparison.OrdinalIgnoreCase));

    private static string? GetOptionValue(string[] args, string option)
    {
        for (var index = 0; index < args.Length - 1; index++)
        {
            if (string.Equals(
                args[index],
                option,
                StringComparison.OrdinalIgnoreCase))
            {
                return args[index + 1];
            }
        }

        return null;
    }

    private static void Validate(PlayConfig config)
    {
        if (config.JobsCount <= 0)
        {
            throw new ArgumentOutOfRangeException(
                nameof(config.JobsCount),
                "JobsCount must be greater than zero.");
        }

        if (config.Url is null ||
            !config.Url.IsAbsoluteUri ||
            config.Url.Scheme is not ("http" or "https"))
        {
            throw new ArgumentException(
                "Url must be an absolute HTTP(S) URL.",
                nameof(config.Url));
        }

        if (string.IsNullOrWhiteSpace(config.OutputFolder))
        {
            throw new ArgumentException(
                "OutputFolder must not be empty.",
                nameof(config.OutputFolder));
        }

        if (config.ViewportWidth <= 0 || config.ViewportHeight <= 0)
        {
            throw new ArgumentOutOfRangeException(
                nameof(config),
                "Viewport dimensions must be positive.");
        }

        foreach (var action in config.OpenPageActions)
        {
            if (action.WaitForPageLoad)
            {
                continue;
            }

            if (action.Click is not { } click)
            {
                throw new ArgumentException(
                    "Each OpenPageAction must be either a click or WAIT_FOR_PAGE_LOAD.",
                    nameof(config));
            }

            ValidateRect(click, config, "OpenPageActions");
        }

        ValidateRect(config.SpinButton, config, nameof(config.SpinButton));

        if (config.PageLoadDuration < TimeSpan.Zero ||
            config.IdleDuration < TimeSpan.Zero ||
            config.CheckClick < TimeSpan.Zero ||
            config.LastResponseTimeout <= TimeSpan.Zero ||
            config.SessionMaxDuration <= TimeSpan.Zero ||
            config.RestartDelay < TimeSpan.Zero ||
            config.MaxResponseBytes < 1_024 ||
            config.DurationOverride is { } durationOverride &&
            durationOverride <= TimeSpan.Zero)
        {
            throw new ArgumentOutOfRangeException(
                nameof(config),
                "Timing values are invalid.");
        }
    }

    private static void ValidateRect(
        ScreenRect rect,
        PlayConfig config,
        string name)
    {
        if (rect.X < 0 ||
            rect.Y < 0 ||
            (long)rect.X + rect.Width > config.ViewportWidth ||
            (long)rect.Y + rect.Height > config.ViewportHeight)
        {
            throw new ArgumentOutOfRangeException(
                name,
                $"{name} ({rect}) must fit within the " +
                $"{config.ViewportWidth}x{config.ViewportHeight} viewport.");
        }
    }
}