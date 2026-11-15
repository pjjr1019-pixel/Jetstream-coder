namespace Deskhand.Windows.App;

internal static class AssistantLaunchVerification
{
    // Shell dispatch is not evidence. Only a unique, new, still-current window
    // attributable to this target can upgrade a launch to completed. Existing
    // windows reused by an editor remain uncertain; never guess their identity.
    internal static async Task<LaunchWindow?> ObserveAsync(string target,
        IReadOnlyList<LaunchWindow> before, ILaunchWindows windows, Func<bool> authorized,
        int attempts = 30)
    {
        if (attempts is < 1 or > 30) throw new ArgumentOutOfRangeException(nameof(attempts));
        for (var i = 0; i < attempts && authorized(); i++)
        {
            var found = WindowLaunchHistory.FindTarget(target, target, null, before, windows.Snapshot());
            if (found is not null && found.ProcessId > 0 && !string.IsNullOrWhiteSpace(found.ProcessName) &&
                windows.IsCurrent(found) && authorized()) return found;
            if (i + 1 < attempts) await Task.Delay(100);
        }
        return null;
    }
}
