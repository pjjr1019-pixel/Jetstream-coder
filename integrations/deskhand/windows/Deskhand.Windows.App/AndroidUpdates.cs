using System.Buffers.Binary;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json;

namespace Deskhand.Windows.App;

/// <summary>One immutable, verified release snapshot per authenticated phone session.</summary>
public sealed class AndroidUpdates : IDisposable
{
    public const int ChunkSize = 65536;
    public const long MaxSize = 512L * 1024 * 1024;
    private readonly string _directory;
    private readonly object _gate = new();
    private FileStream? _file;
    private Release? _release;
    private bool _disposed;
    public AndroidUpdates(string? directory = null) => _directory = directory ?? Path.Combine(AppContext.BaseDirectory, "android-updates");
    public sealed record Release(string packageId, long versionCode, string versionName, long size, int minSdk, string sha256);

    public byte[] Check(byte[] request)
    {
        lock (_gate)
        {
            ObjectDisposedException.ThrowIf(_disposed, this);
            using var json = JsonDocument.Parse(request);
            var id = json.RootElement.GetProperty("requestId").GetString();
            if (!Guid.TryParse(id, out _)) throw new InvalidDataException("Invalid update request.");
            _file?.Dispose(); _file = null; _release = null;
            var manifest = Path.Combine(_directory, "latest.json");
            if (!File.Exists(manifest)) return JsonSerializer.SerializeToUtf8Bytes(new { requestId = id, available = false });
            if (new FileInfo(manifest).Length > 4096) throw new InvalidDataException("Invalid update manifest.");
            var r = JsonSerializer.Deserialize<Release>(File.ReadAllBytes(manifest)) ?? throw new InvalidDataException("Missing update manifest.");
            if (r.packageId != "com.deskhand.android" || r.versionCode < 1 || r.versionCode > int.MaxValue ||
                string.IsNullOrWhiteSpace(r.versionName) || r.versionName.Length > 80 || r.minSdk < 28 ||
                r.size < 1 || r.size > MaxSize || r.sha256 is null || !System.Text.RegularExpressions.Regex.IsMatch(r.sha256, "^[a-f0-9]{64}$"))
                throw new InvalidDataException("Invalid update manifest.");
            var file = new FileStream(Path.Combine(_directory, r.sha256 + ".apk"), FileMode.Open, FileAccess.Read, FileShare.Read | FileShare.Delete);
            try
            {
                if (file.Length != r.size || Convert.ToHexString(SHA256.HashData(file)).ToLowerInvariant() != r.sha256)
                    throw new InvalidDataException("Update checksum mismatch.");
                file.Position = 0;
                _file = file; _release = r;
                return JsonSerializer.SerializeToUtf8Bytes(new { requestId = id, available = true, release = r });
            }
            catch { file.Dispose(); throw; }
        }
    }

    // Response: SHA-256 ASCII (64 bytes), offset uint64 big-endian, then up to 64 KiB.
    public byte[] Chunk(byte[] request)
    {
        lock (_gate)
        {
            ObjectDisposedException.ThrowIf(_disposed, this);
            using var json = JsonDocument.Parse(request);
            var root = json.RootElement;
            var offset = root.GetProperty("offset").GetInt64();
            if (_file is null || _release is null || root.GetProperty("sha256").GetString() != _release.sha256 ||
                offset < 0 || offset >= _release.size || offset % ChunkSize != 0)
                throw new InvalidDataException("The update is no longer available. Check again.");
            var count = (int)Math.Min(ChunkSize, _release.size - offset);
            var bytes = new byte[72 + count];
            Encoding.ASCII.GetBytes(_release.sha256, bytes.AsSpan(0, 64));
            BinaryPrimitives.WriteInt64BigEndian(bytes.AsSpan(64, 8), offset);
            _file.Position = offset;
            _file.ReadExactly(bytes.AsSpan(72));
            return bytes;
        }
    }

    public void Dispose()
    {
        lock (_gate) { _disposed = true; _file?.Dispose(); _file = null; _release = null; }
    }
}
