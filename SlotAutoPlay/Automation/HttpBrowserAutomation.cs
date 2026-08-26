using Microsoft.Playwright;
using SlotAutoPlay.Infrastructure;
using SlotAutoPlay.Models;

namespace SlotAutoPlay.Automation;

public sealed class HttpBrowserAutomation : IAsyncDisposable
{
    private readonly IPlaywright playwright;
    private readonly IBrowser browser;
    private readonly IBrowserContext context;
    private readonly IPage page;
    private readonly SessionJsonlLogger logger;
    private readonly PlayConfig config;
    private readonly PlayOptions options;
    private readonly string sessionId;
    private readonly object stateGate = new();
    private readonly List<Task> pendingLogTasks = [];
    private readonly RoundCaptureState captureState = new();
    private readonly Dictionary<IRequest, MatchedRequest> matchedRequests = [];
    private readonly Dictionary<string, TaskCompletionSource<RoundResponseRecord?>> responseWaiters = [];
    private bool acceptingRequests = true;
    private bool disposed;
    private DateTimeOffset? lastNetworkActivity;
    private long networkActivityCount;
    private DateTimeOffset? lastResponseActivity;
    private long responseActivityCount;

    private HttpBrowserAutomation(
        IPlaywright playwright,
        IBrowser browser,
        IBrowserContext context,
        IPage page,
        SessionJsonlLogger logger,
        PlayConfig config,
        PlayOptions options,
        string sessionId)
    {
        this.playwright = playwright;
        this.browser = browser;
        this.context = context;
        this.page = page;
        this.logger = logger;
        this.config = config;
        this.options = options;
        this.sessionId = sessionId;
    }

    public DateTimeOffset? LastNetworkActivity
    {
        get
        {
            lock (stateGate)
            {
                return lastNetworkActivity;
            }
        }
    }

    public long NetworkActivityCount
    {
        get
        {
            lock (stateGate)
            {
                return networkActivityCount;
            }
        }
    }

    public DateTimeOffset? LastResponseActivity
    {
        get
        {
            lock (stateGate)
            {
                return lastResponseActivity;
            }
        }
    }

    public long ResponseActivityCount
    {
        get
        {
            lock (stateGate)
            {
                return responseActivityCount;
            }
        }
    }

    public static async Task<HttpBrowserAutomation> CreateAsync(
        PlayConfig config,
        PlayOptions options,
        string sessionId,
        CancellationToken cancellationToken)
    {
        cancellationToken.ThrowIfCancellationRequested();

        IPlaywright? playwright = null;
        IBrowser? browser = null;
        IBrowserContext? context = null;
        IPage? page = null;
        SessionJsonlLogger? logger = null;
        HttpBrowserAutomation? automation = null;
        var requestListenerAttached = false;
        var responseListenerAttached = false;

        try
        {
            playwright = await Playwright.CreateAsync().ConfigureAwait(false);
            browser = await playwright.Chromium.LaunchAsync(new BrowserTypeLaunchOptions
            {
                Headless = options.Headless
            }).ConfigureAwait(false);
            context = await browser.NewContextAsync(new BrowserNewContextOptions
            {
                ViewportSize = new ViewportSize
                {
                    Width = config.ViewportWidth,
                    Height = config.ViewportHeight
                }
            }).ConfigureAwait(false);
            page = await context.NewPageAsync().ConfigureAwait(false);
            logger = await SessionJsonlLogger.CreateAsync(
                config.OutputFolder,
                sessionId,
                cancellationToken).ConfigureAwait(false);

            automation = new HttpBrowserAutomation(
                playwright,
                browser,
                context,
                page,
                logger,
                config,
                options,
                sessionId);

            page.Request += automation.OnRequest;
            requestListenerAttached = true;
            page.Response += automation.OnResponse;
            responseListenerAttached = true;
            return automation;
        }
        catch (Exception exception)
        {
            var cleanupErrors = new List<Exception>();

            if (page is not null && responseListenerAttached)
            {
                await TryCleanupAsync(
                    () =>
                    {
                        page.Response -= automation!.OnResponse;
                        return Task.CompletedTask;
                    },
                    cleanupErrors).ConfigureAwait(false);
            }

            if (page is not null && requestListenerAttached)
            {
                await TryCleanupAsync(
                    () =>
                    {
                        page.Request -= automation!.OnRequest;
                        return Task.CompletedTask;
                    },
                    cleanupErrors).ConfigureAwait(false);
            }

            await TryCleanupAsync(
                logger is null ? null : () => logger.DisposeAsync().AsTask(),
                cleanupErrors).ConfigureAwait(false);
            await TryCleanupAsync(
                page is null ? null : () => page.CloseAsync(),
                cleanupErrors).ConfigureAwait(false);
            await TryCleanupAsync(
                context is null ? null : () => context.CloseAsync(),
                cleanupErrors).ConfigureAwait(false);
            await TryCleanupAsync(
                browser is null ? null : () => browser.CloseAsync(),
                cleanupErrors).ConfigureAwait(false);

            if (playwright is not null)
            {
                try
                {
                    playwright.Dispose();
                }
                catch (Exception cleanupException)
                {
                    cleanupErrors.Add(cleanupException);
                }
            }

            if (cleanupErrors.Count == 0)
            {
                throw;
            }

            throw new AggregateException(
                "Browser automation creation and cleanup both failed.",
                [exception, .. cleanupErrors]);
        }
    }

    public async Task NavigateAndOpenAsync(CancellationToken cancellationToken)
    {
        await page.GotoAsync(
            config.Url.ToString(),
            new PageGotoOptions
            {
                WaitUntil = WaitUntilState.Load,
                Timeout = 120_000
            }).WaitAsync(cancellationToken).ConfigureAwait(false);

        await page.WaitForTimeoutAsync(
            (float)config.PageLoadDuration.TotalMilliseconds)
            .WaitAsync(cancellationToken).ConfigureAwait(false);

        foreach (var action in config.OpenPageActions)
        {
            if (action.WaitForPageLoad)
            {
                await page.WaitForTimeoutAsync(
                    (float)config.PageLoadDuration.TotalMilliseconds)
                    .WaitAsync(cancellationToken).ConfigureAwait(false);
                continue;
            }

            if (action.Click is { } click)
            {
                await ClickAsync(click, "open-page", cancellationToken)
                    .ConfigureAwait(false);
            }
        }
    }

    public async Task ClickAsync(
        ScreenRect rect,
        string name,
        CancellationToken cancellationToken)
    {
        var isSpin = string.Equals(name, "spin", StringComparison.OrdinalIgnoreCase);
        PendingSpin? spin = null;
        if (isSpin && (config.CaptureRoundData || config.DiagnosticNetwork))
        {
            spin = captureState.BeginSpin();
            await LogEventAsync("spin_click", new
            {
                sessionId,
                correlationId = spin.CorrelationId,
                timestampUtc = spin.StartedAtUtc
            }).ConfigureAwait(false);
        }

        try
        {
            var point = rect.GetClickPoint(
                Random.Shared,
                options.RandomizeClickPoint);

            await page.Mouse.ClickAsync(point.X, point.Y)
                .WaitAsync(cancellationToken)
                .ConfigureAwait(false);

            if (options.IsDebugMode)
            {
                await ScreenshotAsync($"click-{name}", cancellationToken)
                    .ConfigureAwait(false);
            }
        }
        catch
        {
            if (spin is not null)
            {
                captureState.Complete();
            }

            throw;
        }
    }

    public async Task<NormalizedRoundResult?> WaitForSpinAsync(
        CancellationToken cancellationToken)
    {
        if (!captureState.HasPending)
        {
            return null;
        }

        var request = await captureState.WaitForRequestAsync(
            config.LastResponseTimeout,
            cancellationToken).ConfigureAwait(false);

        if (request is null)
        {
            await LogEventAsync("round_timeout", new
            {
                sessionId,
                correlationId = captureState.LastCompletedCorrelationId,
                reason = "request_not_found"
            }).ConfigureAwait(false);
            return null;
        }

        TaskCompletionSource<RoundResponseRecord?> waiter;
        lock (stateGate)
        {
            if (!responseWaiters.TryGetValue(
                    request.Spin.CorrelationId,
                    out waiter!))
            {
                waiter = new(TaskCreationOptions.RunContinuationsAsynchronously);
                responseWaiters[request.Spin.CorrelationId] = waiter;
            }
        }

        RoundResponseRecord? response;
        NormalizedRoundResult? parsedResult = null;
        try
        {
            response = await waiter.Task.WaitAsync(
                config.LastResponseTimeout,
                cancellationToken).ConfigureAwait(false);
        }
        catch (TimeoutException)
        {
            response = null;
            await LogEventAsync("round_timeout", new
            {
                sessionId,
                correlationId = request.Spin.CorrelationId,
                reason = "response_timeout"
            }).ConfigureAwait(false);
        }

        if (response is not null &&
            request.IsAuthoritative &&
            IsAuthoritativeRoundResponse(response.ContentType))
        {
            var parser = CreateParser();
            var parserRequest = new RoundParserRequest(
                request.Url,
                request.Method,
                request.ContentType,
                request.Spin.CorrelationId);
            var parserResponse = new RoundParserResponse(
                response.Url,
                response.Method,
                response.Status,
                response.ContentType,
                response.RawJson,
                response.CorrelationId,
                response.TimestampUtc);
            parsedResult = parser.Parse(parserRequest, parserResponse);
            await LogEventAsync("round_result", new { sessionId, result = parsedResult })
                .ConfigureAwait(false);
        }

        lock (stateGate)
        {
            responseWaiters.Remove(request.Spin.CorrelationId);
        }

        captureState.Complete();
        return parsedResult;
    }

    public async Task ScreenshotAsync(
        string name,
        CancellationToken cancellationToken)
    {
        Directory.CreateDirectory(config.OutputFolder);
        var safeName = SanitizeFileNamePart(name);
        var path = Path.Combine(config.OutputFolder, $"{sessionId}-{safeName}.png");

        await page.ScreenshotAsync(new PageScreenshotOptions
        {
            Path = path,
            FullPage = false
        }).WaitAsync(cancellationToken).ConfigureAwait(false);
    }

    public bool IsSpinRequest(IRequest request)
    {
        return NetworkRequestClassifier.IsCandidate(request.Url, request.Method) &&
               (string.IsNullOrWhiteSpace(config.SpinRequestMarker) ||
                request.Url.Contains(
                    config.SpinRequestMarker,
                    StringComparison.OrdinalIgnoreCase));
    }

    private bool IsAuthoritativeRoundRequest(IRequest request)
    {
        var contentType = Header(request.Headers, "content-type");
        return NetworkRequestClassifier.IsAuthoritative(
            request.Url,
            request.Method,
            contentType,
            config.RoundEndpointHost,
            config.RoundEndpointPath,
            config.RoundEndpointMethod,
            config.RoundRequestContentType);
    }

    private bool IsDiagnosticCandidate(IRequest request) =>
        NetworkRequestClassifier.IsCandidate(
            request.Url,
            request.Method,
            Header(request.Headers, "content-type"));

    private bool IsAuthoritativeRoundResponse(string? contentType) =>
        !string.IsNullOrWhiteSpace(config.RoundResponseContentType) &&
        NetworkRequestClassifier.ContentTypeMatches(
            contentType,
            config.RoundResponseContentType);

    private void OnRequest(object? sender, IRequest request)
    {
        lock (stateGate)
        {
            if (!acceptingRequests)
            {
                return;
            }

            lastNetworkActivity = DateTimeOffset.UtcNow;
            networkActivityCount++;
        }

        var authoritative = config.CaptureRoundData &&
                            IsAuthoritativeRoundRequest(request);
        var diagnostic = config.DiagnosticNetwork &&
                         IsDiagnosticCandidate(request);

        if (!authoritative && !diagnostic)
        {
            if (IsSpinRequest(request))
            {
                TrackTask(LogRequestWithoutBlockingAsync(request));
            }

            return;
        }

        if (!captureState.HasPending)
        {
            return;
        }

        var safeUrl = JsonSafety.SanitizeUrl(request.Url);
        var contentType = Header(request.Headers, "content-type");
        if (!captureState.TryMatchRequest(
                safeUrl,
                request.Method,
                contentType,
                authoritative))
        {
            return;
        }

        var matched = captureState.GetMatchedRequest()!;
        lock (stateGate)
        {
            matchedRequests[request] = matched;
            responseWaiters[matched.Spin.CorrelationId] =
                new(TaskCreationOptions.RunContinuationsAsynchronously);
        }

        var requestRecord = new RoundRequestRecord(
            matched.TimestampUtc,
            matched.Spin.CorrelationId,
            safeUrl,
            request.Method,
            contentType,
            null,
            null);

        TrackTask(LogEventAsync(
            authoritative ? "round_request" : "network_candidate",
            new
            {
                sessionId,
                correlationId = matched.Spin.CorrelationId,
                request = requestRecord
            }));
    }

    private void OnResponse(object? sender, IResponse response)
    {
        lock (stateGate)
        {
            if (!acceptingRequests)
            {
                return;
            }

            var now = DateTimeOffset.UtcNow;
            lastNetworkActivity = now;
            lastResponseActivity = now;
            networkActivityCount++;
            responseActivityCount++;
        }

        MatchedRequest matched;
        lock (stateGate)
        {
            if (!matchedRequests.TryGetValue(response.Request, out matched!))
            {
                return;
            }
        }

        TrackTask(HandleResponseAsync(response, matched));
    }

    private async Task HandleResponseAsync(
        IResponse response,
        MatchedRequest matched)
    {
        RoundResponseRecord? record = null;

        try
        {
            var contentType = Header(response.Headers, "content-type");
            var contentLength = ParseContentLength(response.Headers);
            string? rawJson = null;
            var bodyBytes = contentLength ?? 0;

            if (contentType?.Contains(
                    "json",
                    StringComparison.OrdinalIgnoreCase) == true &&
                (contentLength is null ||
                 contentLength <= config.MaxResponseBytes))
            {
                var body = await response.BodyAsync()
                    .WaitAsync(
                        TimeSpan.FromSeconds(10))
                    .ConfigureAwait(false);

                bodyBytes = body.Length;
                if (body.Length <= config.MaxResponseBytes)
                {
                    rawJson = JsonSafety.RedactAndLimitJson(
                        System.Text.Encoding.UTF8.GetString(body),
                        config.MaxResponseBytes);
                }
            }

            record = new RoundResponseRecord(
                DateTimeOffset.UtcNow,
                matched.Spin.CorrelationId,
                JsonSafety.SanitizeUrl(response.Url),
                response.Request.Method,
                response.Status,
                contentType,
                bodyBytes,
                rawJson,
                response.Status >= 400
                    ? new SafeError(
                        "http_error",
                        $"HTTP status {response.Status}.")
                    : null);

            var eventType = matched.IsAuthoritative &&
                            IsAuthoritativeRoundResponse(contentType)
                ? "round_response"
                : "unparsed_response";

            await LogEventAsync(
                eventType,
                new { sessionId, response = record })
                .ConfigureAwait(false);
        }
        catch (OperationCanceledException)
        {
            await LogEventAsync(
                "round_error",
                new
                {
                    sessionId,
                    correlationId = matched.Spin.CorrelationId,
                    error = new SafeError(
                        "response_cancelled",
                        "Response body capture was cancelled.")
                }).ConfigureAwait(false);
        }
        catch (Exception exception)
        {
            await LogEventAsync(
                "round_error",
                new
                {
                    sessionId,
                    correlationId = matched.Spin.CorrelationId,
                    error = new SafeError(
                        "response_read_failed",
                        exception.GetType().Name)
                }).ConfigureAwait(false);
        }
        finally
        {
            lock (stateGate)
            {
                if (record is not null &&
                    responseWaiters.TryGetValue(
                        matched.Spin.CorrelationId,
                        out var waiter))
                {
                    waiter.TrySetResult(record);
                }

                matchedRequests.Remove(response.Request);
            }
        }
    }

    private IRoundDataParser CreateParser() =>
        string.Equals(
            config.RoundParser,
            "moon-sisters",
            StringComparison.OrdinalIgnoreCase)
            ? new MoonSistersRoundParser(config.MaxResponseBytes)
            : new GenericJsonRoundParser(config.MaxResponseBytes);

    private Task LogEventAsync(string type, object payload) =>
        logger.LogEventAsync(type, payload, CancellationToken.None);

    private void TrackTask(Task task)
    {
        lock (stateGate)
        {
            pendingLogTasks.Add(task);
        }

        _ = task.ContinueWith(
            completed =>
            {
                if (completed.IsFaulted)
                {
                    Console.Error.WriteLine(
                        $"[{sessionId}] background network task failed: " +
                        $"{completed.Exception?.GetBaseException().Message}");
                }
            },
            TaskScheduler.Default);
    }

    private static string? Header(
        IReadOnlyDictionary<string, string> headers,
        string name) =>
        headers.TryGetValue(name, out var value) ? value : null;

    private static int? ParseContentLength(
        IReadOnlyDictionary<string, string> headers) =>
        headers.TryGetValue("content-length", out var value) &&
        int.TryParse(value, out var result)
            ? result
            : null;

    private async Task LogRequestWithoutBlockingAsync(IRequest request)
    {
        try
        {
            var safeUrl = RedactQuery(request.Url);

            await logger.LogRequestAsync(
                sessionId,
                safeUrl,
                request.Method,
                postData: null,
                requestUrl: safeUrl,
                cancellationToken: CancellationToken.None).ConfigureAwait(false);
        }
        catch (ObjectDisposedException)
        {
            // Cleanup can race with a late Playwright event.
        }
        catch (Exception exception)
        {
            Console.Error.WriteLine(
                $"[{sessionId}] request log failed: {exception.Message}");
        }
    }

    private static string RedactQuery(string url)
    {
        if (!Uri.TryCreate(url, UriKind.Absolute, out var uri))
        {
            return "[redacted-url]";
        }

        return uri.GetLeftPart(UriPartial.Path);
    }

    private static string SanitizeFileNamePart(string value)
    {
        var invalidCharacters = Path.GetInvalidFileNameChars();
        var sanitized = string.Concat(value.Select(character =>
            invalidCharacters.Contains(character) ? '_' : character));

        return string.IsNullOrWhiteSpace(sanitized) ? "capture" : sanitized;
    }

    public async ValueTask DisposeAsync()
    {
        lock (stateGate)
        {
            if (disposed)
            {
                return;
            }

            acceptingRequests = false;
            disposed = true;
        }

        var cleanupErrors = new List<Exception>();

        try
        {
            page.Request -= OnRequest;
        }
        catch (Exception exception)
        {
            cleanupErrors.Add(exception);
        }

        try
        {
            page.Response -= OnResponse;
        }
        catch (Exception exception)
        {
            cleanupErrors.Add(exception);
        }

        await TryCleanupAsync(
            async () =>
            {
                await page.ScreenshotAsync(new PageScreenshotOptions
                {
                    Path = Path.Combine(
                        config.OutputFolder,
                        $"{sessionId}-close.png"),
                    FullPage = false
                }).ConfigureAwait(false);
            },
            cleanupErrors,
            reportToConsole: true).ConfigureAwait(false);

        Task[] logTasks;
        lock (stateGate)
        {
            logTasks = [.. pendingLogTasks];
            pendingLogTasks.Clear();
        }

        if (logTasks.Length > 0)
        {
            try
            {
                await Task.WhenAll(logTasks).ConfigureAwait(false);
            }
            catch (Exception exception)
            {
                cleanupErrors.Add(exception);
            }
        }

        await TryCleanupAsync(
            () => logger.DisposeAsync().AsTask(),
            cleanupErrors).ConfigureAwait(false);
        await TryCleanupAsync(
            () => page.CloseAsync(),
            cleanupErrors).ConfigureAwait(false);
        await TryCleanupAsync(
            () => context.CloseAsync(),
            cleanupErrors).ConfigureAwait(false);
        await TryCleanupAsync(
            () => browser.CloseAsync(),
            cleanupErrors).ConfigureAwait(false);

        try
        {
            playwright.Dispose();
        }
        catch (Exception exception)
        {
            cleanupErrors.Add(exception);
        }

        if (cleanupErrors.Count > 0)
        {
            throw new AggregateException(
                "One or more browser automation resources failed to close.",
                cleanupErrors);
        }
    }

    private static async Task TryCleanupAsync(
        Func<Task>? cleanup,
        ICollection<Exception> errors,
        bool reportToConsole = false)
    {
        if (cleanup is null)
        {
            return;
        }

        try
        {
            await cleanup().ConfigureAwait(false);
        }
        catch (Exception exception)
        {
            errors.Add(exception);
            if (reportToConsole)
            {
                Console.Error.WriteLine(
                    $"Browser automation cleanup failed: {exception.Message}");
            }
        }
    }
}