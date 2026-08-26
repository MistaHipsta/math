using System.Text.Json;
using SlotAutoPlay.Automation;
using SlotAutoPlay.Models;
using Xunit;

namespace SlotAutoPlay.Tests;

public sealed class RoundDataTests
{
    [Fact]
    public void GenericParserReadsAllowlistedNestedFields()
    {
        var parser = new GenericJsonRoundParser();
        var request = new RoundParserRequest(
            "https://game.test/api/round", "POST", "application/json", "corr-1");
        var response = new RoundParserResponse(
            request.Url, request.Method, 200, "application/json",
            """
            {
              "result": {
                "roundId": "r-7",
                "bet": 1.25,
                "winAmount": "4.50",
                "balance": 99.5,
                "currency": "EUR",
                "reels": [["A", "K"], ["Q", "A"]],
                "feature": {"isFreeSpin": true, "name": "free-spins"}
              }
            }
            """,
            request.CorrelationId,
            DateTimeOffset.UtcNow);

        var result = parser.Parse(request, response);

        Assert.Equal(RoundParseStatus.Parsed, result.Status);
        Assert.Equal("r-7", result.RoundId);
        Assert.Equal(1.25m, result.Stake);
        Assert.Equal(4.50m, result.Payout);
        Assert.Equal(99.5m, result.Balance);
        Assert.Equal("EUR", result.Currency);
        Assert.Equal(4, result.Symbols.Count);
        Assert.True(result.Feature?.IsFreeSpin);
        Assert.Equal("generic-json", result.ParserName);
    }

    [Fact]
    public void GenericParserSupportsAlternativeShapeWithoutInventingMissingFields()
    {
        var parser = new GenericJsonRoundParser();
        var request = new RoundParserRequest(
            "https://game.test/round", "POST", "application/json", "corr-2");
        var response = new RoundParserResponse(
            request.Url, request.Method, 200, "application/json",
            """{"transaction_id":"tx-1","wager":"2","free_spins":{"remaining":3}}""",
            request.CorrelationId,
            DateTimeOffset.UtcNow);

        var result = parser.Parse(request, response);

        Assert.Equal("tx-1", result.RoundId);
        Assert.Equal(2m, result.Stake);
        Assert.Null(result.Payout);
        Assert.Null(result.Balance);
        Assert.Equal(3, result.Feature?.FreeSpinsRemaining);
    }

    [Theory]
    [InlineData("<html>not json</html>")]
    [InlineData("{ malformed")]
    [InlineData("")]
    public void ParserDoesNotThrowOnNonJson(string body)
    {
        var parser = new GenericJsonRoundParser();
        var request = new RoundParserRequest(
            "https://game.test/round", "POST", "text/html", "corr-3");
        var response = new RoundParserResponse(
            request.Url, request.Method, 200, "text/html", body,
            request.CorrelationId, DateTimeOffset.UtcNow);

        var result = parser.Parse(request, response);

        Assert.Equal(RoundParseStatus.Unparsed, result.Status);
        Assert.Null(result.Stake);
        Assert.Equal("not_json", result.Error?.Code);
    }

    [Fact]
    public void RedactionRemovesSensitiveKeysRecursively()
    {
        var sanitized = JsonSafety.RedactAndLimitJson(
            """{"session":"s","nested":{"token":"t","signature":"sig","value":4},"safe":"yes"}""",
            10_000);

        Assert.NotNull(sanitized);
        Assert.DoesNotContain("\"s\"", sanitized);
        Assert.DoesNotContain("\"t\"", sanitized);
        Assert.DoesNotContain("\"sig\"", sanitized);
        Assert.Contains("\"value\":4", sanitized);
        Assert.Contains("\"safe\":\"yes\"", sanitized);
    }

[Fact]
    public void SanitizeUrlRedactsMoonSistersSessionPathAndQuery()
    {
        var sanitized = JsonSafety.SanitizeUrl(
            "https://betman-demo.head.3oaks.com/betman-demo/gs/moon_sisters/desktop/opaque-session/demo/?token=secret");

        Assert.Equal(
            "https://betman-demo.head.3oaks.com/betman-demo/gs/moon_sisters/desktop/[redacted-session]/demo/",
            sanitized);
        Assert.DoesNotContain("opaque-session", sanitized);
        Assert.DoesNotContain("token", sanitized);
        Assert.DoesNotContain("secret", sanitized);
    }
    [Fact]
    public void RedactionHonorsUtf8Limit()
    {
        var sanitized = JsonSafety.RedactAndLimitJson(
            """{"safe":"abcdefghijklmnopqrstuvwxyz"}""", 24);

        Assert.NotNull(sanitized);
        Assert.True(System.Text.Encoding.UTF8.GetByteCount(sanitized!) <= 24);
    }

    [Fact]
    public async Task CaptureStateMatchesOnlyOneRequestAndTimesOut()
    {
        var state = new RoundCaptureState();
        var spin = state.BeginSpin(DateTimeOffset.UtcNow);

        Assert.Equal(spin.CorrelationId, state.CurrentCorrelationId);
        Assert.True(state.TryMatchRequest(
            "https://game.test/round", "POST", "application/json", true));
        Assert.False(state.TryMatchRequest(
            "https://game.test/other", "POST", "application/json", true));

        var matched = await state.WaitForRequestAsync(
            TimeSpan.FromMilliseconds(20), CancellationToken.None);
        Assert.Equal(spin.CorrelationId, matched?.Spin.CorrelationId);
        state.Complete();
        Assert.False(state.HasPending);

        state.BeginSpin();
        var timeout = await state.WaitForRequestAsync(
            TimeSpan.FromMilliseconds(20), CancellationToken.None);
        Assert.Null(timeout);
        Assert.False(state.HasPending);
    }

    [Fact]
    public void AnalyzerUsesOnlyParsedNonNegativeRoundsAndSafeRtp()
    {
        var now = DateTimeOffset.UtcNow;
        var rounds = new[]
        {
            new NormalizedRoundResult(
                RoundParseStatus.Parsed, "r1", "c1", now, 2m, 5m, null,
                "EUR", [new SymbolPosition("A", 0, 0)],
                [], new FeatureState(Name: "bonus"), "fixture", "1", "{}", null),
            new NormalizedRoundResult(
                RoundParseStatus.Unparsed, null, "c2", now, 100m, 100m, null,
                null, [], [], null, "fixture", "1", "{}", null),
            new NormalizedRoundResult(
                RoundParseStatus.Parsed, "r2", "c3", now, 0m, 4m, null,
                "EUR", [], [], null, "fixture", "1", "{}", null)
        };

        var analysis = RoundAnalyzer.Analyze(rounds);

        Assert.Equal(2, analysis.Count);
        Assert.Equal(2m, analysis.TotalStake);
        Assert.Equal(9m, analysis.TotalPayout);
        Assert.Equal(4.5m, analysis.Rtp);
        Assert.Equal(1, analysis.Symbols["A"]);
        Assert.Equal(1, analysis.Features["bonus"]);
    }

    [Fact]
    public void MoonSistersParserMapsCapturedPlayFixtureWithoutInventingFields()
    {
        var body = File.ReadAllText(Path.Combine(
            AppContext.BaseDirectory,
            "Fixtures",
            "moon-sisters-play-response.json"));
        var request = new RoundParserRequest(
            "https://betman-demo.head.3oaks.com/betman-demo/gs/moon_sisters/desktop/demo/",
            "POST",
            "text/plain",
            "corr-moon-play");
        var response = new RoundParserResponse(
            request.Url,
            request.Method,
            200,
            "application/json",
            body,
            request.CorrelationId,
            DateTimeOffset.UtcNow);

        var parser = new MoonSistersRoundParser();

        Assert.True(parser.CanParse(request, response));
        var result = parser.Parse(request, response);

        Assert.Equal(RoundParseStatus.Parsed, result.Status);
        Assert.Equal("corr-moon-play", result.RoundId);
        Assert.Equal("correlationId", result.RoundIdSource);
        Assert.Equal(100m, result.Stake);
        Assert.Equal(160m, result.Payout);
        Assert.Equal(99660m, result.Balance);
        Assert.Equal("FUN", result.Currency);
        Assert.Equal(15, result.Symbols.Count);
        Assert.Equal((0, 0, "1", 0), (
            result.Symbols[0].Reel,
            result.Symbols[0].Row,
            result.Symbols[0].Symbol,
            result.Symbols[0].Index));
        Assert.Equal((4, 2, "1", 14), (
            result.Symbols[^1].Reel,
            result.Symbols[^1].Row,
            result.Symbols[^1].Symbol,
            result.Symbols[^1].Index));
        Assert.All(result.Symbols, symbol => Assert.NotNull(symbol.RawValue));
        Assert.Empty(result.WinLines);
        Assert.Null(result.Feature);
        Assert.Equal("moon-sisters", result.ParserName);
        Assert.Equal("1.0", result.ParserVersion);
    }

    [Fact]
    public void MoonSistersParserPreservesZeroPayout()
    {
        var parser = new MoonSistersRoundParser();
        var request = new RoundParserRequest(
            "https://game.test/round", "POST", "application/json", "corr-zero");
        var response = new RoundParserResponse(
            request.Url, request.Method, 200, "application/json",
            """{"command":"play","context":{"spins":{"board":[["A"]],"round_bet":"1.25"},"last_win":0,"round_finished":false},"user":{"balance":"5","currency":"EUR"}}""",
            request.CorrelationId, DateTimeOffset.UtcNow);

        var result = parser.Parse(request, response);

        Assert.Equal(RoundParseStatus.Parsed, result.Status);
        Assert.Equal(0m, result.Payout);
        Assert.Equal(1.25m, result.Stake);
        Assert.Equal("corr-zero", result.RoundId);
    }

    [Fact]
    public void MoonSistersParserMapsHoldAndWinBonusRespin()
    {
        var parser = new MoonSistersRoundParser();
        var request = new RoundParserRequest(
            "https://game.test/round", "POST", "application/json", "corr-bonus");
        var response = new RoundParserResponse(
            request.Url,
            request.Method,
            200,
            "application/json",
            """{"command":"play","context":{"current":"bonus","actions":["respin"],"bonus":{"board":[[10,10,10],[7,10,2],[9,9,9],[1,10,10],[10,10,10]],"round_bet":100,"round_win":0,"rounds_granted":3,"rounds_left":3,"total_win":0},"spins":{"board":[[1,10,10],[7,2,2],[9,9,9],[1,6,10],[10,10,10]],"round_bet":100},"last_win":40,"round_finished":false},"user":{"balance":99340,"currency":"FUN"}}""",
            request.CorrelationId,
            DateTimeOffset.UtcNow);

        Assert.True(parser.CanParse(request, response));
        var result = parser.Parse(request, response);

        Assert.Equal(RoundParseStatus.Parsed, result.Status);
        Assert.Equal(100m, result.Stake);
        Assert.Equal(40m, result.Payout);
        Assert.Equal(15, result.Symbols.Count);
        Assert.NotNull(result.Feature);
        Assert.True(result.Feature!.IsBonus);
        Assert.Equal("hold-and-win", result.Feature.Name);
        Assert.Equal("bonus", result.Feature.Status);
        Assert.Equal(3, result.Feature.FreeSpinsRemaining);
        Assert.DoesNotContain("session_id", result.RawJson ?? "");
        Assert.DoesNotContain("request_id", result.RawJson ?? "");
    }

    [Fact]
    public void MoonSistersParserKeepsMissingPayoutNullAndPartial()
    {
        var parser = new MoonSistersRoundParser();
        var request = new RoundParserRequest(
            "https://game.test/round", "POST", "application/json", "corr-missing");
        var response = new RoundParserResponse(
            request.Url, request.Method, 200, "application/json",
            """{"command":"play","context":{"spins":{"board":[[7]],"round_bet":1}},"user":{"balance":5,"currency":"EUR"}}""",
            request.CorrelationId, DateTimeOffset.UtcNow);

        var result = parser.Parse(request, response);

        Assert.Equal(RoundParseStatus.Partial, result.Status);
        Assert.Null(result.Payout);
        Assert.Equal(1m, result.Stake);
        Assert.Equal("correlationId", result.RoundIdSource);
    }

    [Fact]
    public void MoonSistersParserRedactsUnknownBoardObjectFields()
    {
        var parser = new MoonSistersRoundParser();
        var request = new RoundParserRequest(
            "https://game.test/round", "POST", "application/json", "corr-object");
        var response = new RoundParserResponse(
            request.Url, request.Method, 200, "application/json",
            """{"command":"play","context":{"spins":{"board":[[{"symbol":"W","token":"secret"}]],"round_bet":1},"last_win":0}}""",
            request.CorrelationId, DateTimeOffset.UtcNow);

        var result = parser.Parse(request, response);

        Assert.Single(result.Symbols);
        Assert.Equal("W", result.Symbols[0].Symbol);
        Assert.DoesNotContain("secret", result.Symbols[0].RawValue);
        Assert.Equal(0, result.Symbols[0].Index);
    }

    [Fact]
    public void MoonSistersParserRejectsSyncAsNonRound()
    {
        var request = new RoundParserRequest(
            "https://betman-demo.head.3oaks.com/betman-demo/gs/moon_sisters/desktop/demo/",
            "POST",
            "text/plain",
            "corr-moon-sync");
        var response = new RoundParserResponse(
            request.Url,
            request.Method,
            200,
            "application/json",
            """{"command":"sync","status":{"code":"OK"},"user":{"balance":100,"currency":"FUN"}}""",
            request.CorrelationId,
            DateTimeOffset.UtcNow);
        var parser = new MoonSistersRoundParser();

        Assert.False(parser.CanParse(request, response));
        var result = parser.Parse(request, response);

        Assert.Equal(RoundParseStatus.Unparsed, result.Status);
        Assert.Null(result.Stake);
        Assert.Null(result.Payout);
        Assert.Equal("not_round_response", result.Error?.Code);
    }

    [Fact]
    public void JsonlReaderSkipsMalformedAndNonResultEvents()
    {
        var path = Path.Combine(Path.GetTempPath(), $"rounds-{Guid.NewGuid():N}.jsonl");
        try
        {
            var result = new NormalizedRoundResult(
                RoundParseStatus.Parsed, "r", "c", DateTimeOffset.UtcNow,
                1m, 2m, null, "EUR", [], [], null, "fixture", "1", "{}", null);
            File.WriteAllLines(path,
            [
                """{"schemaVersion":1,"type":"session_started"}""",
                "{ malformed",
                JsonSerializer.Serialize(new { schemaVersion = 1, type = "round_result", result })
            ]);

            Assert.Single(RoundAnalyzer.ReadJsonl(path));
        }
        finally
        {
            File.Delete(path);
        }
    }
}
public sealed class NetworkRequestClassifierTests
{
    [Theory]
    [InlineData("https://game.test/assets/spin.js")]
    [InlineData("https://game.test/assets/image.png")]
    [InlineData("https://game.test/assets/font.woff2")]
    public void AssetUrlsAreExcluded(string url)
    {
        Assert.False(NetworkRequestClassifier.IsCandidate(url, "POST"));
        Assert.False(NetworkRequestClassifier.IsAuthoritative(
            url, "POST", "game.test", "/api/round"));
    }

    [Theory]
    [InlineData("GET")]
    [InlineData("DELETE")]
    [InlineData("OPTIONS")]
    public void NonMutationMethodsAreNotCandidates(string method)
    {
        Assert.False(NetworkRequestClassifier.IsCandidate(
            "https://game.test/api/round", method));
    }

[Fact]
    public void AuthoritativeMatchSupportsConfiguredSessionPlaceholder()
    {
        Assert.True(NetworkRequestClassifier.IsAuthoritative(
            "https://betman-demo.head.3oaks.com/betman-demo/gs/moon_sisters/desktop/opaque-session/demo/",
            "POST",
            "text/plain",
            "betman-demo.head.3oaks.com",
            "/betman-demo/gs/moon_sisters/desktop/{session}/demo/",
            "POST",
            "text/plain"));

        Assert.False(NetworkRequestClassifier.IsAuthoritative(
            "https://betman-demo.head.3oaks.com/betman-demo/gs/moon_sisters/desktop/opaque-session/other/",
            "POST",
            "text/plain",
            "betman-demo.head.3oaks.com",
            "/betman-demo/gs/moon_sisters/desktop/{session}/demo/",
            "POST",
            "text/plain"));
    }
    [Fact]
    public void AuthoritativeMatchRequiresExactHostPathMethodAndContentType()
    {
        const string url = "https://game.test/api/round?session=redacted";

        Assert.True(NetworkRequestClassifier.IsAuthoritative(
            url,
            "POST",
            "text/plain; charset=utf-8",
            "game.test",
            "/api/round",
            "POST",
            "text/plain"));
        Assert.False(NetworkRequestClassifier.IsAuthoritative(
            url,
            "POST",
            "application/json",
            "game.test",
            "/api/round",
            "POST",
            "text/plain"));
        Assert.False(NetworkRequestClassifier.IsAuthoritative(
            url,
            "PUT",
            "text/plain",
            "game.test",
            "/api/round",
            "POST",
            "text/plain"));
        Assert.False(NetworkRequestClassifier.IsAuthoritative(
            "https://game.test/assets/round.js",
            "POST",
            "text/plain",
            "game.test",
            "/assets/round.js",
            "POST",
            "text/plain"));
    }

    [Fact]
    public void AuthoritativeMatchRequiresExactHostAndPath()
    {
        const string url = "https://game.test/api/round?session=redacted";

        Assert.True(NetworkRequestClassifier.IsAuthoritative(
            url, "POST", "game.test", "/api/round"));
        Assert.False(NetworkRequestClassifier.IsAuthoritative(
            url, "POST", "other.test", "/api/round"));
        Assert.False(NetworkRequestClassifier.IsAuthoritative(
            url, "POST", "game.test", "/api/other"));
    }
}