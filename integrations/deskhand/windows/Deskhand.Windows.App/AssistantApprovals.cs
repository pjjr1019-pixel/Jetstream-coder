namespace Deskhand.Windows.App;

internal sealed record AssistantApproval(Guid Id, Guid TaskId, Guid ActionId, string Operation, string Target, string Purpose, DateTimeOffset ExpiresUtc);

/// <summary>Host-owned, one-use decisions. A decision does not itself grant action authority.</summary>
internal sealed class AssistantApprovals
{
    private readonly object _gate = new();
    private readonly Dictionary<Guid, (AssistantApproval Request, TaskCompletionSource<bool> Decision)> _pending = [];
    internal event Action? Changed;

    internal AssistantApproval[] Pending()
    {
        lock (_gate) return _pending.Values.Select(item => item.Request).ToArray();
    }

    internal async Task<bool> AskAsync(AssistantApproval request, CancellationToken cancellation)
    {
        var decision = new TaskCompletionSource<bool>(TaskCreationOptions.RunContinuationsAsynchronously);
        lock (_gate)
        {
            if (request.Id == Guid.Empty || request.TaskId == Guid.Empty || request.ActionId == Guid.Empty ||
                request.ExpiresUtc <= DateTimeOffset.UtcNow || request.ExpiresUtc > DateTimeOffset.UtcNow.AddMinutes(5) ||
                string.IsNullOrWhiteSpace(request.Operation) || string.IsNullOrWhiteSpace(request.Target) ||
                string.IsNullOrWhiteSpace(request.Purpose) || !_pending.TryAdd(request.Id, (request, decision))) return false;
        }
        Changed?.Invoke();
        try
        {
            var timeout = request.ExpiresUtc - DateTimeOffset.UtcNow;
            return timeout > TimeSpan.Zero && await decision.Task.WaitAsync(timeout, cancellation);
        }
        catch (Exception ex) when (ex is TimeoutException or OperationCanceledException) { return false; }
        finally
        {
            lock (_gate) _pending.Remove(request.Id);
            Changed?.Invoke();
        }
    }

    internal bool Resolve(Guid approvalId, bool approved)
    {
        lock (_gate)
        {
            if (!_pending.Remove(approvalId, out var item)) return false;
            if (item.Request.ExpiresUtc <= DateTimeOffset.UtcNow) approved = false;
            item.Decision.TrySetResult(approved);
        }
        Changed?.Invoke();
        return true;
    }

    internal void CancelAll()
    {
        (AssistantApproval Request, TaskCompletionSource<bool> Decision)[] pending;
        lock (_gate) { pending = _pending.Values.ToArray(); _pending.Clear(); }
        foreach (var item in pending) item.Decision.TrySetResult(false);
        if (pending.Length != 0) Changed?.Invoke();
    }

    internal void CancelTask(Guid taskId)
    {
        (AssistantApproval Request, TaskCompletionSource<bool> Decision)[] pending;
        lock (_gate)
        {
            pending = _pending.Values.Where(item => item.Request.TaskId == taskId).ToArray();
            foreach (var item in pending) _pending.Remove(item.Request.Id);
        }
        foreach (var item in pending) item.Decision.TrySetResult(false);
        if (pending.Length != 0) Changed?.Invoke();
    }
}
