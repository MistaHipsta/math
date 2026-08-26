using System.Globalization;
using System.Text;
using System.Text.Json;

namespace SlotAutoPlay.Models;

public enum RoundParseStatus
{
    Unparsed,
    Partial,
    Parsed,
    Error
}

public sealed record SafeError(string Code, string Message);

public sealed record SymbolPosition(
    string? Symbol,
    int Reel,
    int Row,
    int? Index = null,
    string? RawValue = null,
    int? Column = null);

public sealed record WinLine(string? Id, decimal? Amount, IReadOnlyList<SymbolPosition> Positions);

public sealed record FeatureState(
    bool? IsBonus = null,
    bool? IsFreeSpin = null,
    string? Name = null,
    int? FreeSpinsRemaining = null,
    string? Status = null);

public sealed record RoundRequestRecord(
    DateTimeOffset TimestampUtc,
    string CorrelationId,
    string Url,
    string Method,
    string? ContentType,
    int? BodyBytes,
    string? SafeBodyJson);

public sealed record RoundResponseRecord(
    DateTimeOffset TimestampUtc,
    string CorrelationId,
    string Url,
    string Method,
    int Status,
    string? ContentType,
    int BodyBytes,
    string? RawJson,
    SafeError? Error);

public sealed record NormalizedRoundResult(
    RoundParseStatus Status,
    string? RoundId,
    string? CorrelationId,
    DateTimeOffset TimestampUtc,
    decimal? Stake,
    decimal? Payout,
    decimal? Balance,
    string? Currency,
    IReadOnlyList<SymbolPosition> Symbols,
    IReadOnlyList<WinLine> WinLines,
    FeatureState? Feature,
    string ParserName,
    string ParserVersion,
    string? RawJson,
    SafeError? Error,
    string? RoundIdSource = null);

public sealed record RoundRecord(
    RoundRequestRecord? Request,
    RoundResponseRecord Response,
    NormalizedRoundResult Result);

public sealed record RoundParserRequest(
    string Url,
    string Method,
    string? ContentType,
    string CorrelationId);

public sealed record RoundParserResponse(
    string Url,
    string Method,
    int Status,
    string? ContentType,
    string? Body,
    string CorrelationId,
    DateTimeOffset TimestampUtc);

public interface IRoundDataParser
{
    string Name { get; }
    string Version { get; }
    bool CanParse(RoundParserRequest request, RoundParserResponse response);
    NormalizedRoundResult Parse(RoundParserRequest request, RoundParserResponse response);
}

public static class JsonSafety
{
    private static readonly string[] SensitiveNames =
    [
        "token", "session", "authorization", "auth", "cookie", "secret",
        "signature", "mgckey", "password", "credential", "apikey", "api_key"
    ];

    public static string SanitizeUrl(string? value)
    {
        if (!Uri.TryCreate(value, UriKind.Absolute, out var uri))
        {
            return "[redacted-url]";
        }

        var path = uri.AbsolutePath;
        var segments = path.Split('/');
        for (var index = 1; index < segments.Length - 1; index++)
        {
            if (string.Equals(segments[index - 1], "desktop",
                    StringComparison.OrdinalIgnoreCase) &&
                string.Equals(segments[index + 1], "demo",
                    StringComparison.OrdinalIgnoreCase) &&
                !string.IsNullOrWhiteSpace(segments[index]))
            {
                segments[index] = "[redacted-session]";
                break;
            }
        }

        return $"{uri.Scheme}://{uri.Authority}{string.Join("/", segments)}";
    }

    public static string? RedactAndLimitJson(string? body, int maxBytes)
    {
        if (string.IsNullOrWhiteSpace(body) || maxBytes <= 0)
        {
            return null;
        }

        try
        {
            using var document = JsonDocument.Parse(body);
            var sanitized = RedactElement(document.RootElement);
            var json = JsonSerializer.Serialize(sanitized);
            return LimitUtf8(json, maxBytes);
        }
        catch (JsonException)
        {
            return null;
        }
    }

    public static string LimitUtf8(string value, int maxBytes)
    {
        var bytes = Encoding.UTF8.GetBytes(value);
        if (bytes.Length <= maxBytes)
        {
            return value;
        }

        var suffix = "...[truncated]";
        var suffixBytes = Encoding.UTF8.GetBytes(suffix);
        var length = Math.Max(0, maxBytes - suffixBytes.Length);
        return Encoding.UTF8.GetString(bytes, 0, length) + suffix;
    }

    private static object? RedactElement(JsonElement element)
    {
        return element.ValueKind switch
        {
            JsonValueKind.Object => element.EnumerateObject().ToDictionary(
                property => property.Name,
                property => IsSensitive(property.Name)
                    ? "[redacted]"
                    : RedactElement(property.Value)),
            JsonValueKind.Array => element.EnumerateArray()
                .Select(RedactElement).ToArray(),
            JsonValueKind.String => element.GetString(),
            JsonValueKind.Number when element.TryGetDecimal(out var number) => number,
            JsonValueKind.True => true,
            JsonValueKind.False => false,
            _ => null
        };
    }

    private static bool IsSensitive(string name) =>
        SensitiveNames.Any(item =>
            name.Contains(item, StringComparison.OrdinalIgnoreCase));
}

public class GenericJsonRoundParser : IRoundDataParser
{
    private static readonly string[] RoundIdNames =
        ["roundId", "roundID", "round_id", "transactionId", "transactionID", "transaction_id"];
    private static readonly string[] StakeNames =
        ["stake", "bet", "betAmount", "bet_amount", "wager"];
    private static readonly string[] PayoutNames =
        ["payout", "win", "winAmount", "win_amount", "totalWin", "total_win"];
    private static readonly string[] BalanceNames =
        ["balance", "playerBalance", "walletBalance", "cashBalance"];
    private static readonly string[] CurrencyNames = ["currency", "currencyCode", "currency_code"];
    private static readonly string[] ReelsNames = ["reels", "symbols", "results", "grid"];
    private static readonly string[] BonusNames = ["bonus", "freeSpins", "free_spins", "feature"];

    public virtual string Name => "generic-json";
    public virtual string Version => "1.0";
    public int MaxRawJsonBytes { get; }

    public GenericJsonRoundParser(int maxRawJsonBytes = 1_048_576)
    {
        MaxRawJsonBytes = Math.Max(1024, maxRawJsonBytes);
    }

    public virtual bool CanParse(RoundParserRequest request, RoundParserResponse response) =>
        response.Status is >= 200 and < 300 &&
        response.ContentType?.Contains("json", StringComparison.OrdinalIgnoreCase) == true &&
        TryParse(response.Body, out _);

    public virtual NormalizedRoundResult Parse(
        RoundParserRequest request,
        RoundParserResponse response)
    {
        var raw = JsonSafety.RedactAndLimitJson(response.Body, MaxRawJsonBytes);
        if (!TryParse(response.Body, out var root))
        {
            return new(
                RoundParseStatus.Unparsed, null, response.CorrelationId,
                response.TimestampUtc, null, null, null, null, [],
                [], null, Name, Version, raw,
                new SafeError("not_json", "Response body is not valid JSON."));
        }

        var roundId = FindString(root, RoundIdNames);
        var roundIdSource = roundId is not null
            ? "response"
            : string.IsNullOrWhiteSpace(response.CorrelationId)
                ? null
                : "correlationId";
        roundId ??= string.IsNullOrWhiteSpace(response.CorrelationId)
            ? null
            : response.CorrelationId;
        var stake = FindDecimal(root, StakeNames);
        var payout = FindDecimal(root, PayoutNames);
        var balance = FindDecimal(root, BalanceNames);
        var currency = FindString(root, CurrencyNames);
        var symbols = ExtractSymbols(root);
        var feature = ExtractFeature(root);
        var found = roundId is not null || stake is not null || payout is not null ||
                    balance is not null || currency is not null || symbols.Count > 0 ||
                    feature is not null;

        var hasMinimumRoundMath = stake is >= 0 &&
                                   payout is >= 0;

        return new(
            hasMinimumRoundMath
                ? RoundParseStatus.Parsed
                : found
                    ? RoundParseStatus.Partial
                    : RoundParseStatus.Unparsed,
            roundId, response.CorrelationId, response.TimestampUtc, stake, payout,
            balance, currency, symbols, [], feature, Name, Version, raw,
            found ? null : new SafeError(
                "unknown_schema",
                "JSON parsed but no allowlisted round fields were found."),
            roundIdSource);
    }

    protected static bool TryParse(string? body, out JsonElement root)
    {
        if (string.IsNullOrWhiteSpace(body))
        {
            root = default;
            return false;
        }

        try
        {
            using var document = JsonDocument.Parse(body);
            root = document.RootElement.Clone();
            return true;
        }
        catch (JsonException)
        {
            root = default;
            return false;
        }
    }

    protected static JsonElement? Find(JsonElement root, IEnumerable<string> names)
    {
        var wanted = names.ToHashSet(StringComparer.OrdinalIgnoreCase);
        if (root.ValueKind == JsonValueKind.Object)
        {
            foreach (var property in root.EnumerateObject())
            {
                if (wanted.Contains(property.Name))
                {
                    return property.Value;
                }

                var nested = Find(property.Value, names);
                if (nested is not null)
                {
                    return nested;
                }
            }
        }
        else if (root.ValueKind == JsonValueKind.Array)
        {
            foreach (var item in root.EnumerateArray())
            {
                var nested = Find(item, names);
                if (nested is not null)
                {
                    return nested;
                }
            }
        }

        return null;
    }

    protected static string? FindString(JsonElement root, IEnumerable<string> names)
    {
        var value = Find(root, names);
        return value is { ValueKind: JsonValueKind.String } ? value.Value.GetString() : null;
    }

    private static decimal? FindDecimal(JsonElement root, IEnumerable<string> names)
    {
        var value = Find(root, names);
        if (value is null)
        {
            return null;
        }

        if (value.Value.ValueKind == JsonValueKind.Number &&
            value.Value.TryGetDecimal(out var number))
        {
            return number;
        }

        return value.Value.ValueKind == JsonValueKind.String &&
               decimal.TryParse(value.Value.GetString(), NumberStyles.Any,
                   CultureInfo.InvariantCulture, out number)
            ? number
            : null;
    }

    private static IReadOnlyList<SymbolPosition> ExtractSymbols(JsonElement root)
    {
        var value = Find(root, ReelsNames);
        if (value is null)
        {
            return [];
        }

        var result = new List<SymbolPosition>();
        if (value.Value.ValueKind == JsonValueKind.Array)
        {
            var reel = 0;
            foreach (var item in value.Value.EnumerateArray())
            {
                if (item.ValueKind == JsonValueKind.Array)
                {
                    var row = 0;
                    foreach (var symbol in item.EnumerateArray())
                    {
                        if (symbol.ValueKind == JsonValueKind.String)
                        {
                            result.Add(new(symbol.GetString()!, reel, row));
                        }
                        row++;
                    }
                }
                else if (item.ValueKind == JsonValueKind.String)
                {
                    result.Add(new(item.GetString()!, reel, 0));
                }
                reel++;
            }
        }

        return result;
    }

    protected static FeatureState? ExtractFeature(JsonElement root)
    {
        var value = Find(root, BonusNames);
        if (value is null)
        {
            return null;
        }

        if (value.Value.ValueKind == JsonValueKind.String)
        {
            return new(Name: value.Value.GetString());
        }

        if (value.Value.ValueKind != JsonValueKind.Object)
        {
            return new();
        }

        var isBonus = FindBoolean(value.Value, ["isBonus", "bonus", "is_bonus"]);
        var isFreeSpin = FindBoolean(value.Value, ["isFreeSpin", "freeSpin", "free_spin"]);
        var remaining = FindInt(value.Value, ["remaining", "freeSpinsRemaining", "free_spins_remaining"]);
        var name = FindString(value.Value, ["name", "type", "feature"]);
        return new(isBonus, isFreeSpin, name, remaining);
    }

    private static bool? FindBoolean(JsonElement root, IEnumerable<string> names)
    {
        var value = Find(root, names);
        return value is { ValueKind: JsonValueKind.True or JsonValueKind.False }
            ? value.Value.GetBoolean()
            : null;
    }

    private static int? FindInt(JsonElement root, IEnumerable<string> names)
    {
        var value = FindDecimal(root, names);
        return value is { } number ? (int)number : null;
    }
}

public sealed class MoonSistersRoundParser : GenericJsonRoundParser
{
    public MoonSistersRoundParser(int maxRawJsonBytes = 1_048_576)
        : base(maxRawJsonBytes)
    {
    }

    public override string Name => "moon-sisters";
    public override string Version => "1.0";

    public override bool CanParse(
        RoundParserRequest request,
        RoundParserResponse response)
    {
        return response.Status is >= 200 and < 300 &&
               string.Equals(request.Method, "POST", StringComparison.OrdinalIgnoreCase) &&
               response.ContentType?.Contains(
                   "json",
                   StringComparison.OrdinalIgnoreCase) == true &&
               TryReadRoot(response.Body, out var root) &&
               string.Equals(
                   GetString(root, "command"),
                   "play",
                   StringComparison.OrdinalIgnoreCase) &&
               HasMoonRoundContainer(root);
    }

    public override NormalizedRoundResult Parse(
        RoundParserRequest request,
        RoundParserResponse response)
    {
        var raw = JsonSafety.RedactAndLimitJson(response.Body, MaxRawJsonBytes);
        if (!TryReadRoot(response.Body, out var root))
        {
            return CreateResult(
                RoundParseStatus.Unparsed,
                response,
                raw,
                error: new SafeError(
                    "not_json",
                    "Response body is not valid JSON."));
        }

        var command = GetString(root, "command");
        var context = GetObject(root, "context");
        var spins = context is { } contextValue
            ? GetObject(contextValue, "spins")
            : null;
        var bonus = context is { } bonusContext
            ? GetObject(bonusContext, "bonus")
            : null;
        var roundContainer = spins ?? bonus;

        var user = GetObject(root, "user");

        if (!string.Equals(command, "play", StringComparison.OrdinalIgnoreCase) ||
            roundContainer is null)
        {
            // A sync (or other non-round) response still represents a real
            // wager: round_bet is deducted and user.balance changes. Keep its
            // stake and balance (status Partial) so per-round metrics do not
            // silently drop these spins from the RTP denominator.
            var syncStake = spins is { } syncSpins
                ? GetDecimal(syncSpins, "round_bet")
                : null;
            var syncBalance = user is { } syncUser
                ? GetDecimal(syncUser, "balance")
                : null;
            var syncCurrency = user is { } syncUserCurrency
                ? GetString(syncUserCurrency, "currency")
                : null;
            var syncHasFields = syncStake is not null ||
                                syncBalance is not null ||
                                syncCurrency is not null;

            return CreateResult(
                syncHasFields
                    ? RoundParseStatus.Partial
                    : RoundParseStatus.Unparsed,
                response,
                raw,
                roundId: null,
                roundIdSource: null,
                stake: syncStake,
                payout: null,
                balance: syncBalance,
                currency: syncCurrency,
                error: syncHasFields
                    ? new SafeError(
                        "sync_response",
                        "Response is a Moon Sisters sync/non-round response; " +
                        "stake and balance recorded, payout and symbols unknown.")
                    : new SafeError(
                        "not_round_response",
                        "Response is a Moon Sisters sync/non-round response."));
        }

        var roundId = FindString(root, [
            "roundId", "roundID", "round_id",
            "transactionId", "transactionID", "transaction_id"]);
        var roundIdSource = roundId is not null
            ? "response"
            : string.IsNullOrWhiteSpace(response.CorrelationId)
                ? null
                : "correlationId";
        roundId ??= string.IsNullOrWhiteSpace(response.CorrelationId)
            ? null
            : response.CorrelationId;

        var stake = GetDecimal(roundContainer.Value, "round_bet");
        var payout = context is { } contextForWin
            ? GetDecimal(contextForWin, "last_win")
            : null;
        payout ??= GetDecimal(roundContainer.Value, "round_win");
        payout ??= GetDecimal(roundContainer.Value, "total_win");
        var balance = user is { } userValue
            ? GetDecimal(userValue, "balance")
            : null;
        var currency = user is { } userForCurrency
            ? GetString(userForCurrency, "currency")
            : null;
        var symbols = ExtractBoard(spins, bonus);
        var feature = ExtractMoonFeature(context, spins, bonus);
        var boardPresent = HasBoard(roundContainer.Value);

        var hasRoundMath = boardPresent &&
                           stake is >= 0 &&
                           payout is >= 0;
        var hasObservedRoundField = stake is not null ||
                                     payout is not null ||
                                     balance is not null ||
                                     currency is not null ||
                                     symbols.Count > 0;

        return CreateResult(
            hasRoundMath
                ? RoundParseStatus.Parsed
                : hasObservedRoundField
                    ? RoundParseStatus.Partial
                    : RoundParseStatus.Unparsed,
            response,
            raw,
            roundId,
            roundIdSource,
            stake,
            payout,
            balance,
            currency,
            symbols,
            feature,
            error: CreateMoonError(
                hasObservedRoundField,
                boardPresent,
                stake,
                payout));
    }

    private static SafeError? CreateMoonError(
        bool hasObservedRoundField,
        bool boardPresent,
        decimal? stake,
        decimal? payout)
    {
        if (!hasObservedRoundField)
        {
            return new SafeError(
                "missing_round_fields",
                "Play response contains no mapped round fields.");
        }

        if (!boardPresent)
        {
            return new SafeError(
                "missing_board",
                "Play response does not contain context.spins.board.");
        }

        if (stake is null)
        {
            return new SafeError(
                "missing_stake",
                "Play response does not contain a numeric stake.");
        }

        if (payout is null)
        {
            return new SafeError(
                "missing_payout",
                "Play response does not contain a numeric payout.");
        }

        return null;
    }

    private NormalizedRoundResult CreateResult(
        RoundParseStatus status,
        RoundParserResponse response,
        string? raw,
        string? roundId = null,
        string? roundIdSource = null,
        decimal? stake = null,
        decimal? payout = null,
        decimal? balance = null,
        string? currency = null,
        IReadOnlyList<SymbolPosition>? symbols = null,
        FeatureState? feature = null,
        SafeError? error = null) =>
        new(
            Status: status,
            RoundId: roundId,
            CorrelationId: response.CorrelationId,
            TimestampUtc: response.TimestampUtc,
            Stake: stake,
            Payout: payout,
            Balance: balance,
            Currency: currency,
            Symbols: symbols ?? [],
            WinLines: [],
            Feature: feature,
            ParserName: Name,
            ParserVersion: Version,
            RawJson: raw,
            Error: error,
            RoundIdSource: roundIdSource);

    private static bool TryReadRoot(string? body, out JsonElement root)
    {
        if (string.IsNullOrWhiteSpace(body))
        {
            root = default;
            return false;
        }

        try
        {
            using var document = JsonDocument.Parse(body);
            root = document.RootElement.Clone();
            return true;
        }
        catch (JsonException)
        {
            root = default;
            return false;
        }
    }

    private static JsonElement? GetObject(JsonElement? root, string name)
    {
        if (root is not { ValueKind: JsonValueKind.Object } value ||
            !value.TryGetProperty(name, out var property) ||
            property.ValueKind != JsonValueKind.Object)
        {
            return null;
        }

        return property;
    }

    private static string? GetString(JsonElement? root, string name)
    {
        if (root is not { ValueKind: JsonValueKind.Object } value ||
            !value.TryGetProperty(name, out var property) ||
            property.ValueKind != JsonValueKind.String)
        {
            return null;
        }

        return property.GetString();
    }

    private static decimal? GetDecimal(JsonElement root, string name)
    {
        if (!root.TryGetProperty(name, out var property))
        {
            return null;
        }

        if (property.ValueKind == JsonValueKind.Number &&
            property.TryGetDecimal(out var number))
        {
            return number;
        }

        return property.ValueKind == JsonValueKind.String &&
               decimal.TryParse(
                   property.GetString(),
                   NumberStyles.Any,
                   CultureInfo.InvariantCulture,
                   out number)
            ? number
            : null;
    }

    private static bool? GetBoolean(JsonElement root, string name) =>
        root.TryGetProperty(name, out var property) &&
        property.ValueKind is JsonValueKind.True or JsonValueKind.False
            ? property.GetBoolean()
            : null;

    private static FeatureState? ExtractMoonFeature(
        JsonElement? context,
        JsonElement? spins,
        JsonElement? bonus)
    {
        if (bonus is { } bonusValue)
        {
            var remaining = GetInt(bonusValue, "rounds_left");
            var status = GetString(context, "current") ??
                         (remaining is > 0 ? "respin" : "complete");
            return new(
                IsBonus: true,
                Name: "hold-and-win",
                FreeSpinsRemaining: remaining,
                Status: status);
        }

        foreach (var container in new[] { context, spins })
        {
            if (container is not { ValueKind: JsonValueKind.Object } value)
            {
                continue;
            }

            foreach (var name in new[] { "feature", "bonus", "freeSpins", "free_spins" })
            {
                if (value.TryGetProperty(name, out var feature) &&
                    feature.ValueKind != JsonValueKind.Null)
                {
                    return ParseFeatureValue(feature);
                }
            }
        }

        return null;
    }

    private static FeatureState ParseFeatureValue(JsonElement value)
    {
        if (value.ValueKind == JsonValueKind.String)
        {
            return new(Name: value.GetString());
        }

        if (value.ValueKind != JsonValueKind.Object)
        {
            return new();
        }

        var isBonus = value.TryGetProperty("isBonus", out var bonus) &&
                      bonus.ValueKind is JsonValueKind.True or JsonValueKind.False
            ? bonus.GetBoolean()
            : (bool?)null;
        var isFreeSpin = value.TryGetProperty("isFreeSpin", out var freeSpin) &&
                         freeSpin.ValueKind is JsonValueKind.True or JsonValueKind.False
            ? freeSpin.GetBoolean()
            : (bool?)null;
        var name = value.TryGetProperty("name", out var featureName) &&
                   featureName.ValueKind == JsonValueKind.String
            ? featureName.GetString()
            : null;
        var status = value.TryGetProperty("status", out var featureStatus) &&
                     featureStatus.ValueKind == JsonValueKind.String
            ? featureStatus.GetString()
            : null;
        var remaining = value.TryGetProperty("remaining", out var remainingValue) &&
                        remainingValue.ValueKind == JsonValueKind.Number &&
                        remainingValue.TryGetInt32(out var count)
            ? count
            : (int?)null;

        return new(isBonus, isFreeSpin, name, remaining, status);
    }

    private static IReadOnlyList<SymbolPosition> ExtractBoard(
        JsonElement? spins,
        JsonElement? bonus)
    {
        JsonElement board;
        if (spins is { } spinsValue &&
            spinsValue.TryGetProperty("board", out board) &&
            board.ValueKind == JsonValueKind.Array)
        {
        }
        else if (bonus is { } bonusValue &&
                 bonusValue.TryGetProperty("board", out board) &&
                 board.ValueKind == JsonValueKind.Array)
        {
        }
        else
        {
            return [];
        }

        var result = new List<SymbolPosition>();
        if (board.ValueKind == JsonValueKind.Array)
        {
            var reel = 0;
            var rawIndex = 0;
            foreach (var reelValue in board.EnumerateArray())
            {
                var row = 0;
                FlattenBoardValue(reelValue, reel, ref row, ref rawIndex, result);
                reel++;
            }
        }
        else
        {
            var rawIndex = 0;
            var row = 0;
            FlattenBoardValue(board, 0, ref row, ref rawIndex, result);
        }

        return result;
    }

    private static bool HasMoonRoundContainer(JsonElement root)
    {
        var context = GetObject(root, "context");
        return context is { } contextValue &&
               (GetObject(contextValue, "spins") is not null ||
                GetObject(contextValue, "bonus") is not null);
    }

    private static bool HasBoard(JsonElement container) =>
        container.TryGetProperty("board", out var board) &&
        board.ValueKind == JsonValueKind.Array;

    private static int? GetInt(JsonElement root, string name)
    {
        var value = GetDecimal(root, name);
        return value is { } number ? (int)number : null;
    }

    private static void FlattenBoardValue(
        JsonElement value,
        int reel,
        ref int row,
        ref int rawIndex,
        ICollection<SymbolPosition> result)
    {
        if (value.ValueKind == JsonValueKind.Array)
        {
            foreach (var child in value.EnumerateArray())
            {
                FlattenBoardValue(child, reel, ref row, ref rawIndex, result);
            }

            return;
        }

        var rawValue = SafeBoardValue(value);
        result.Add(new(
            Symbol: SafeBoardSymbol(value),
            Reel: reel,
            Row: row++,
            Index: rawIndex++,
            RawValue: rawValue,
            Column: reel));
    }

    private static string? SafeBoardSymbol(JsonElement value)
    {
        if (value.ValueKind is JsonValueKind.String or
            JsonValueKind.Number or
            JsonValueKind.True or
            JsonValueKind.False)
        {
            return value.ToString();
        }

        if (value.ValueKind != JsonValueKind.Object)
        {
            return null;
        }

        foreach (var name in new[] { "symbol", "name", "id", "code", "value" })
        {
            if (value.TryGetProperty(name, out var property) &&
                property.ValueKind is JsonValueKind.String or
                JsonValueKind.Number)
            {
                return property.ToString();
            }
        }

        return null;
    }

    private static string? SafeBoardValue(JsonElement value)
    {
        if (value.ValueKind is JsonValueKind.String or
            JsonValueKind.Number or
            JsonValueKind.True or
            JsonValueKind.False)
        {
            return value.ToString();
        }

        if (value.ValueKind != JsonValueKind.Object)
        {
            return null;
        }

        var allowed = new Dictionary<string, string?>(StringComparer.Ordinal);
        foreach (var name in new[] { "symbol", "name", "id", "code", "value" })
        {
            if (value.TryGetProperty(name, out var property) &&
                property.ValueKind is JsonValueKind.String or JsonValueKind.Number)
            {
                allowed[name] = property.ToString();
            }
        }

        return allowed.Count == 0 ? null : JsonSerializer.Serialize(allowed);
    }
}

public sealed record RoundAnalysis(
    int Count,
    int ParsedCount,
    decimal TotalStake,
    decimal TotalPayout,
    decimal? Rtp,
    IReadOnlyDictionary<string, int> Symbols,
    IReadOnlyDictionary<string, int> Features);

public sealed record RoundAnalysisReport(
    int EventCount,
    int ParsedCount,
    int PartialCount,
    int UnparsedCount,
    int ErrorCount,
    RoundAnalysis Analysis);

public static class RoundAnalyzer
{
    public static RoundAnalysis Analyze(IEnumerable<NormalizedRoundResult> rounds)
    {
        var parsed = rounds.Where(item =>
            item.Status == RoundParseStatus.Parsed &&
            item.Payout is >= 0).ToArray();

        // All spins that carried a wager (parsed results with a payout, and
        // sync/partial spins whose stake was recorded) contribute to the RTP
        // denominator so sync-only spins are not silently dropped.
        var staked = parsed.Where(item => item.Stake is >= 0)
            .Concat(rounds.Where(item =>
                item.Status == RoundParseStatus.Partial &&
                item.Stake is >= 0));

        var totalStake = staked.Sum(item => item.Stake!.Value);
        var payout = parsed.Sum(item => item.Payout!.Value);
        var symbols = parsed.SelectMany(item => item.Symbols)
            .Where(item => item.Symbol is not null)
            .GroupBy(item => item.Symbol!, StringComparer.Ordinal)
            .ToDictionary(group => group.Key, group => group.Count(), StringComparer.Ordinal);
        var features = parsed.Select(item => item.Feature?.Name)
            .Where(item => !string.IsNullOrWhiteSpace(item))
            .GroupBy(item => item!, StringComparer.Ordinal)
            .ToDictionary(group => group.Key, group => group.Count(), StringComparer.Ordinal);
        return new(
            parsed.Length + rounds.Count(item => item.Status == RoundParseStatus.Partial),
            parsed.Length,
            totalStake,
            payout,
            totalStake == 0 ? null : payout / totalStake,
            symbols,
            features);
    }

    public static RoundAnalysisReport AnalyzeJsonl(string path)
    {
        var results = File.ReadLines(path)
            .Select(TryReadRoundResult)
            .Where(result => result is not null)
            .Select(result => result!)
            .ToArray();

        return new(
            results.Length,
            results.Count(result => result.Status == RoundParseStatus.Parsed),
            results.Count(result => result.Status == RoundParseStatus.Partial),
            results.Count(result => result.Status == RoundParseStatus.Unparsed),
            results.Count(result => result.Error is not null),
            Analyze(results));
    }

    public static IEnumerable<NormalizedRoundResult> ReadJsonl(string path)
    {
        foreach (var line in File.ReadLines(path))
        {
            var parsed = TryReadRoundResult(line);
            if (parsed is not null)
            {
                yield return parsed;
            }
        }
    }

    private static NormalizedRoundResult? TryReadRoundResult(string line)
    {
        try
        {
            using var document = JsonDocument.Parse(line);
            if (!document.RootElement.TryGetProperty("type", out var type) ||
                type.GetString() != "round_result" ||
                !document.RootElement.TryGetProperty("result", out var result))
            {
                return null;
            }

            return result.Deserialize<NormalizedRoundResult>();
        }
        catch (JsonException)
        {
            // Malformed or unrelated event lines are intentionally skipped.
            return null;
        }
    }
}