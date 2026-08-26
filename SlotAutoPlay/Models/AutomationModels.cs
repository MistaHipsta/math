using System.Globalization;

namespace SlotAutoPlay.Models;

public readonly record struct ScreenRect
{
    public ScreenRect(int x, int y, int width, int height)
    {
        if (width <= 0)
        {
            throw new ArgumentOutOfRangeException(nameof(width), "Width must be positive.");
        }

        if (height <= 0)
        {
            throw new ArgumentOutOfRangeException(nameof(height), "Height must be positive.");
        }

        X = x;
        Y = y;
        Width = width;
        Height = height;
    }

    public int X { get; }
    public int Y { get; }
    public int Width { get; }
    public int Height { get; }

    public int CenterX => checked(X + Width / 2);
    public int CenterY => checked(Y + Height / 2);

    public (int X, int Y) GetClickPoint(Random? random = null, bool randomize = false)
    {
        if (!randomize)
        {
            return (CenterX, CenterY);
        }

        random ??= Random.Shared;
        return (
            random.Next(X, checked(X + Width)),
            random.Next(Y, checked(Y + Height)));
    }

    public override string ToString() =>
        string.Create(CultureInfo.InvariantCulture, $"{X},{Y} {Width}x{Height}");
}

public readonly record struct OpenPageAction
{
    private OpenPageAction(ScreenRect? click, bool waitForPageLoad)
    {
        Click = click;
        WaitForPageLoad = waitForPageLoad;
    }

    public ScreenRect? Click { get; }
    public bool WaitForPageLoad { get; }

    public static OpenPageAction ForClick(ScreenRect rect) =>
        new(rect, waitForPageLoad: false);

    public static OpenPageAction ForCommand(string command) =>
        command == BrowserAutomation.WAIT_FOR_PAGE_LOAD
            ? ForWaitForPageLoad()
            : throw new ArgumentException(
                $"Unsupported open-page command '{command}'.",
                nameof(command));

    public static OpenPageAction ForWaitForPageLoad() =>
        new(click: null, waitForPageLoad: true);
}

public sealed class PlayConfig
{
    public string Name { get; init; } = "BigMoney";
    public Uri Url { get; init; } = new("https://example.invalid/");
    public string OutputFolder { get; init; } = "output/slotautoplay";
    public int ViewportWidth { get; init; } = 1280;
    public int ViewportHeight { get; init; } = 720;
    public ScreenRect OpenPageClicks { get; init; } = new(640, 360, 1, 1);
    public IReadOnlyList<OpenPageAction> OpenPageActions { get; init; } =
        [OpenPageAction.ForClick(new ScreenRect(640, 360, 1, 1))];
    public ScreenRect SpinButton { get; init; } = new(1080, 620, 120, 60);
    public TimeSpan PageLoadDuration { get; init; } = TimeSpan.FromSeconds(3);
    public TimeSpan IdleDuration { get; init; } = TimeSpan.FromSeconds(2);
    public TimeSpan CheckClick { get; init; } = TimeSpan.FromSeconds(1);
    public TimeSpan LastResponseTimeout { get; init; } = TimeSpan.FromSeconds(30);
    public TimeSpan SessionMaxDuration { get; init; } = TimeSpan.FromMinutes(30);
    public TimeSpan RestartDelay { get; init; } = TimeSpan.FromSeconds(1);
    public TimeSpan? DurationOverride { get; init; }
    public int? RoundsOverride { get; init; }
    public int JobsCount { get; init; } = 1;
    public bool RandomizeClickPoint { get; init; }
    public bool IsDebugMode { get; init; }
    public bool Headless { get; init; } = true;
    public string SpinRequestMarker { get; init; } = "spin";
    public bool CaptureRoundData { get; init; }
    public bool DiagnosticNetwork { get; init; }
    public int MaxResponseBytes { get; init; } = 1_048_576;
    public string? RoundEndpointHost { get; init; }
    public string? RoundEndpointPath { get; init; }
    public string? RoundEndpointMethod { get; init; }
    public string? RoundRequestContentType { get; init; }
    public string? RoundResponseContentType { get; init; }
    public string RoundParser { get; init; } = "generic-json";
}

public sealed class PlayOptions
{
    public bool IsDebugMode { get; init; }
    public bool RandomizeClickPoint { get; init; }
    public bool Headless { get; init; } = true;
    public TimeSpan? DurationOverride { get; init; }
    public int? RoundsOverride { get; init; }
    public string? BasePath { get; init; }
    public bool CaptureRoundData { get; init; }
    public bool DiagnosticNetwork { get; init; }
    public int MaxResponseBytes { get; init; } = 1_048_576;
}

public static class BrowserAutomation
{
    public const string WAIT_FOR_PAGE_LOAD = "WAIT_FOR_PAGE_LOAD";
}

public static class ClickPoint
{
    public static (int X, int Y) For(
        ScreenRect rect,
        Random? random = null,
        bool randomize = false) =>
        rect.GetClickPoint(random, randomize);
}