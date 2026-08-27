using SlotAutoPlay.Models;

namespace SlotAutoPlay.Configuration;

public abstract class GameConfigProvider
{
    protected GameConfigProvider(string name)
    {
        Name = name;
    }

    public string Name { get; }

    public abstract Uri Url { get; }

    public virtual string OutputFolder { get; init; } =
        Path.Combine("output", "slotautoplay");

    public virtual string? OutputSubfolder { get; init; }

    public virtual bool DebugOnly { get; init; }

    public virtual int ViewportWidth { get; init; } = 1280;
    public virtual int ViewportHeight { get; init; } = 720;
    public virtual TimeSpan PageLoadDuration { get; init; } =
        TimeSpan.FromSeconds(3);
    public virtual TimeSpan IdleDuration { get; init; } =
        TimeSpan.FromSeconds(2);
    public virtual TimeSpan CheckClick { get; init; } =
        TimeSpan.FromSeconds(1);
    public virtual TimeSpan LastResponseTimeout { get; init; } =
        TimeSpan.FromSeconds(30);
    public virtual TimeSpan SessionMaxDuration { get; init; } =
        TimeSpan.FromMinutes(30);
    public virtual TimeSpan RestartDelay { get; init; } =
        TimeSpan.FromSeconds(1);
    public virtual ScreenRect OpenPageClicks { get; init; } =
        new(640, 360, 1, 1);
    public virtual IReadOnlyList<OpenPageAction> OpenPageActions =>
        [OpenPageAction.ForClick(OpenPageClicks)];
    public virtual ScreenRect SpinButton { get; init; } =
        new(1080, 620, 120, 60);
    public virtual string SpinRequestMarker { get; init; } = "spin";
    public virtual string? RoundEndpointHost { get; init; }
    public virtual string? RoundEndpointPath { get; init; }
    public virtual string? RoundEndpointMethod { get; init; }
    public virtual string? RoundRequestContentType { get; init; }
    public virtual string? RoundResponseContentType { get; init; }
    public virtual string RoundParser { get; init; } = "generic-json";
    public virtual int JobsCount { get; init; } = 1;

    public PlayConfig Build(
        PlayOptions options,
        string outputFolder,
        int jobsCount)
    {
        return new PlayConfig
        {
            Name = Name,
            Url = Url,
            OutputFolder = outputFolder,
            ViewportWidth = ViewportWidth,
            ViewportHeight = ViewportHeight,
            OpenPageClicks = OpenPageClicks,
            OpenPageActions = OpenPageActions,
            SpinButton = SpinButton,
            PageLoadDuration = PageLoadDuration,
            IdleDuration = IdleDuration,
            CheckClick = CheckClick,
            LastResponseTimeout = LastResponseTimeout,
            SessionMaxDuration = SessionMaxDuration,
            RestartDelay = RestartDelay,
            DurationOverride = options.DurationOverride,
            RoundsOverride = options.RoundsOverride,
            SpinRequestMarker = SpinRequestMarker,
            CaptureRoundData = options.CaptureRoundData,
            DiagnosticNetwork = options.DiagnosticNetwork,
            MaxResponseBytes = options.MaxResponseBytes,
            RoundEndpointHost = RoundEndpointHost,
            RoundEndpointPath = RoundEndpointPath,
            RoundEndpointMethod = RoundEndpointMethod,
            RoundRequestContentType = RoundRequestContentType,
            RoundResponseContentType = RoundResponseContentType,
            RoundParser = RoundParser,
            JobsCount = jobsCount,
            IsDebugMode = options.IsDebugMode,
            RandomizeClickPoint = options.RandomizeClickPoint,
            ResponseDriven = options.ResponseDriven,
            CollectOnly = options.CollectOnly,
            Headless = options.Headless
        };
    }
}

public sealed class BigMoneyConfig : GameConfigProvider
{
    public BigMoneyConfig()
        : base("BigMoney")
    {
    }

    public override Uri Url { get; } =
        new("https://example.invalid/big-money");

    public override ScreenRect OpenPageClicks { get; init; } =
        new(640, 360, 1, 1);

    public override ScreenRect SpinButton { get; init; } =
        new(1080, 620, 120, 60);

    public override string SpinRequestMarker { get; init; } = "spin";
}

public sealed class FloatingDragonConfig : GameConfigProvider
{
    public FloatingDragonConfig()
        : base("FloatingDragon")
    {
    }

    public override Uri Url { get; } =
        new("https://demogamesfree.pragmaticplay.net/gs2c/html5Game.do?extGame=1&symbol=vs10floatdrg&gname=Floating%20Dragon&jurisdictionID=99&mgckey=stylename@generic~SESSION@2af77440-74f9-4199-bb2c-8f44e91b1e6b");

    // Coordinates are intentionally unset until the initial page screenshot is reviewed.
    public override IReadOnlyList<OpenPageAction> OpenPageActions =>
        [OpenPageAction.ForWaitForPageLoad()];
}

public sealed class MoonSistersConfig : GameConfigProvider
{
    public static readonly ScreenRect START = new(1082, 274, 174, 174);
    public static readonly ScreenRect EXIT_SPLASH = START;
    public static readonly ScreenRect SPIN = START;

    public MoonSistersConfig()
        : base("MoonSisters")
    {
    }

    public override Uri Url { get; } =
        new("https://3oaks.com/api/v1/games/moon_sisters/play?lang=en");

    public override string? OutputSubfolder { get; init; } = "moon-sisters";

    public override ScreenRect OpenPageClicks { get; init; } = START;

    public override IReadOnlyList<OpenPageAction> OpenPageActions =>
        [
            OpenPageAction.ForWaitForPageLoad(),
            OpenPageAction.ForClick(EXIT_SPLASH)
        ];

    public override ScreenRect SpinButton { get; init; } = SPIN;

    public override string? RoundEndpointHost { get; init; } =
        "betman-demo.head.3oaks.com";

    public override string? RoundEndpointPath { get; init; } =
        "/betman-demo/gs/moon_sisters/desktop/{session}/demo/";

    public override string? RoundEndpointMethod { get; init; } = "POST";
    public override string? RoundRequestContentType { get; init; } = "text/plain";
    public override string? RoundResponseContentType { get; init; } = "application/json";
    public override string RoundParser { get; init; } = "moon-sisters";
}