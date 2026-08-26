using SlotAutoPlay.Models;

namespace SlotAutoPlay.Automation;

public sealed record PendingSpin(string CorrelationId, DateTimeOffset StartedAtUtc);

public sealed record MatchedRequest(
    PendingSpin Spin,
    string Url,
    string Method,
    string? ContentType,
    DateTimeOffset TimestampUtc,
    bool IsAuthoritative);

public sealed class RoundCaptureState
{
    private readonly object gate = new();
    private PendingSpin? pending;
    private string? lastCompletedCorrelationId;
    private MatchedRequest? matchedRequest;
    private TaskCompletionSource<MatchedRequest?>? completion;
    private TaskCompletionSource<RoundResponseRecord?>? responseCompletion;

    public string? CurrentCorrelationId
    {
        get
        {
            lock (gate)
            {
                return pending?.CorrelationId;
            }
        }
    }

    public string? LastCompletedCorrelationId
    {
        get
        {
            lock (gate)
            {
                return lastCompletedCorrelationId;
            }
        }
    }

    public bool HasPending
    {
        get
        {
            lock (gate)
            {
                return pending is not null;
            }
        }
    }

    public PendingSpin BeginSpin(DateTimeOffset? timestampUtc = null)
    {
        lock (gate)
        {
            if (pending is not null)
            {
                throw new InvalidOperationException("A spin is already pending.");
            }

            pending = new(
                Guid.NewGuid().ToString("N"),
                timestampUtc ?? DateTimeOffset.UtcNow);
            matchedRequest = null;
            completion = new TaskCompletionSource<MatchedRequest?>(
                TaskCreationOptions.RunContinuationsAsynchronously);
            responseCompletion = new TaskCompletionSource<RoundResponseRecord?>(
                TaskCreationOptions.RunContinuationsAsynchronously);
            return pending;
        }
    }

    public bool TryMatchRequest(
        string url,
        string method,
        string? contentType,
        bool isAuthoritative,
        DateTimeOffset? timestampUtc = null)
    {
        lock (gate)
        {
            if (pending is null || matchedRequest is not null)
            {
                return false;
            }

            matchedRequest = new(
                pending,
                url,
                method,
                contentType,
                timestampUtc ?? DateTimeOffset.UtcNow,
                isAuthoritative);
            completion!.TrySetResult(matchedRequest);
            return true;
        }
    }

    public MatchedRequest? GetMatchedRequest()
    {
        lock (gate)
        {
            return matchedRequest;
        }
    }

    /// <summary>
    /// Returns the completion source a spin's response record is delivered to.
    /// It is created once in <see cref="BeginSpin"/> and cleared by
    /// <see cref="Complete"/>, so the response handler and the awaiting worker
    /// always observe the same task regardless of scheduling order.
    /// </summary>
    public TaskCompletionSource<RoundResponseRecord?>? GetResponseCompletion()
    {
        lock (gate)
        {
            return responseCompletion;
        }
    }

    public async Task<MatchedRequest?> WaitForRequestAsync(
        TimeSpan timeout,
        CancellationToken cancellationToken)
    {
        Task<MatchedRequest?> task;
        lock (gate)
        {
            if (pending is null)
            {
                return null;
            }

            if (matchedRequest is not null)
            {
                return matchedRequest;
            }

            task = completion!.Task;
        }

        try
        {
            return await task.WaitAsync(timeout, cancellationToken)
                .ConfigureAwait(false);
        }
        catch (TimeoutException)
        {
            Complete();
            return null;
        }
    }

    public void Complete()
    {
        lock (gate)
        {
            lastCompletedCorrelationId = pending?.CorrelationId;
            pending = null;
            matchedRequest = null;
            completion?.TrySetResult(null);
            completion = null;
            responseCompletion = null;
        }
    }

    public bool IsCorrelation(string correlationId)
    {
        lock (gate)
        {
            return pending?.CorrelationId == correlationId;
        }
    }
}