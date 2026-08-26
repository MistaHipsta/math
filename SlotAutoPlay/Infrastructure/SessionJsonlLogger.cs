using System.Text;
using System.Text.Json;
using System.Text.Json.Serialization;
using SlotAutoPlay.Models;

namespace SlotAutoPlay.Infrastructure;

public sealed class SessionJsonlLogger : IAsyncDisposable
{
    private static readonly JsonSerializerOptions JsonOptions = new()
    {
        DefaultIgnoreCondition = JsonIgnoreCondition.WhenWritingNull,
        WriteIndented = false
    };

    private readonly SemaphoreSlim gate = new(1, 1);
    private readonly StreamWriter writer;
    private bool disposed;

    private SessionJsonlLogger(StreamWriter writer, string filePath)
    {
        this.writer = writer;
        FilePath = filePath;
    }

    public string FilePath { get; }

    public static async Task<SessionJsonlLogger> CreateAsync(
        string outputFolder,
        string sessionId,
        CancellationToken cancellationToken)
    {
        Directory.CreateDirectory(outputFolder);

        var safeSessionId = SanitizeFileNamePart(sessionId);
        var filePath = Path.Combine(outputFolder, $"{safeSessionId}.jsonl");
        var stream = new FileStream(
            filePath,
            FileMode.Create,
            FileAccess.Write,
            FileShare.Read,
            bufferSize: 4096,
            options: FileOptions.Asynchronous | FileOptions.SequentialScan);

        SessionJsonlLogger? logger = null;
        try
        {
            logger = new SessionJsonlLogger(
                new StreamWriter(
                    stream,
                    new UTF8Encoding(encoderShouldEmitUTF8Identifier: false))
                {
                    AutoFlush = true
                },
                filePath);

            await logger.WriteAsync(new
            {
                schemaVersion = 1,
                timestamp = DateTimeOffset.UtcNow,
                type = "session_started",
                sessionId
            }, cancellationToken).ConfigureAwait(false);

            return logger;
        }
        catch
        {
            if (logger is not null)
            {
                await logger.DisposeAsync().ConfigureAwait(false);
            }
            else
            {
                await stream.DisposeAsync().ConfigureAwait(false);
            }

            throw;
        }
    }

    public Task LogRequestAsync(
        string sessionId,
        string url,
        string method,
        string? postData,
        string? requestUrl,
        CancellationToken cancellationToken) =>
        LogEventAsync("spin_request", new
        {
            sessionId,
            url = JsonSafety.SanitizeUrl(url),
            method,
            postData = JsonSafety.RedactAndLimitJson(postData, 64 * 1024),
            requestUrl = JsonSafety.SanitizeUrl(requestUrl)
        }, cancellationToken);

    public Task LogEventAsync(
        string type,
        object payload,
        CancellationToken cancellationToken)
    {
        var values = new Dictionary<string, object?>
        {
            ["schemaVersion"] = 1,
            ["timestamp"] = DateTimeOffset.UtcNow,
            ["type"] = type
        };

        foreach (var property in payload.GetType().GetProperties())
        {
            values[property.Name] = property.GetValue(payload);
        }

        return WriteAsync(values, cancellationToken);
    }

    public Task LogErrorAsync(
        string sessionId,
        string message,
        CancellationToken cancellationToken) =>
        LogEventAsync("error", new { sessionId, message }, cancellationToken);

    private async Task WriteAsync(object value, CancellationToken cancellationToken)
    {
        await gate.WaitAsync(cancellationToken).ConfigureAwait(false);
        try
        {
            ObjectDisposedException.ThrowIf(disposed, this);
            await writer.WriteLineAsync(JsonSerializer.Serialize(value, JsonOptions))
                .ConfigureAwait(false);
            await writer.FlushAsync(cancellationToken).ConfigureAwait(false);
        }
        finally
        {
            gate.Release();
        }
    }

    private static string SanitizeFileNamePart(string value)
    {
        var invalidCharacters = Path.GetInvalidFileNameChars();
        var sanitized = string.Concat(value.Select(character =>
            invalidCharacters.Contains(character) ? '_' : character));

        return string.IsNullOrWhiteSpace(sanitized) ? "session" : sanitized;
    }

    public async ValueTask DisposeAsync()
    {
        await gate.WaitAsync().ConfigureAwait(false);
        try
        {
            if (disposed)
            {
                return;
            }

            disposed = true;
            await writer.DisposeAsync().ConfigureAwait(false);
        }
        finally
        {
            gate.Release();
        }
    }
}