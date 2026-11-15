using System.Buffers.Binary;
using System.Security.Cryptography;
using System.Text;

namespace Deskhand.Core;

public enum Role : byte { Host = 1, Client = 2, Connector = 3 }

public static class Kdf
{
    private static readonly byte[] Salt = Encoding.UTF8.GetBytes("deskhand-v1");

    public static byte[] Derive(byte[] ikm, string info, int length) =>
        HKDF.DeriveKey(HashAlgorithmName.SHA256, ikm, length, Salt, Encoding.UTF8.GetBytes(info));

    /// <summary>Opaque rendezvous id the relay sees; reveals nothing about the secret.</summary>
    public static string RoomId(byte[] secret) => Convert.ToHexString(Derive(secret, "room", 16)).ToLowerInvariant();

    public static byte[] AuthKey(byte[] secret) => Derive(secret, "auth", 32);
}

public static class Wire
{
    public const byte HelloMarker = 0x01;
    public const byte RecordMarker = 0x02;
}

/// <summary>Message types carried inside encrypted records (first plaintext byte).</summary>
public static class Msg
{
    public const byte VideoFrame = 21;
    public const byte VideoAck = 22;
    public const byte VideoFailed = 23;
    public const byte UpdateCheck = 40;
    public const byte UpdateInfo = 41;
    public const byte UpdateGet = 42;
    public const byte UpdateChunk = 43;
    public const byte UpdateError = 44;

    public const byte Info = 1;
    public const byte Frame = 2;
    public const byte Ack = 3;
    public const byte Input = 4;
    public const byte TextFocus = 20;
    public const byte PairRequest = 5;
    public const byte PairAccept = 6;
    public const byte PairReject = 7;
    public const byte Start = 8;
    public const byte Bye = 9;
    public const byte DesktopList = 10;
    public const byte DesktopListResult = 11;
    public const byte DesktopOpen = 12;
    public const byte DesktopOpenResult = 13;
    public const byte ChatRequest = 14;
    public const byte ChatResult = 15;
    public const byte DesktopBack = 16;
    public const byte DesktopBackResult = 17;
    // 18/19 are STOP-only. Two previously released phone builds also used
    // 18 for foreground-window requests; the host still accepts that legacy
    // shape (see HostEngine.IsForegroundWindowRequest) but no new sender
    // must use 18/19 for foreground work.
    public const byte StopRequest = 18;
    public const byte StopStatus = 19;
    // Foreground-window request/result. Allocated 24/25 (the first free
    // values after VideoFailed=23) so they no longer collide with the
    // STOP types above. Android must add the matching constants
    // (FOREGROUND_WINDOW=24, FOREGROUND_WINDOW_RESULT=25) and send
    // foreground work on 24; until it does, the host keeps replying to
    // foreground requests on 19, the value shipped phone builds listen on.
    public const byte ForegroundWindow = 24;
    public const byte ForegroundWindowResult = 25;
    // Stream (100.017): push channel for runtime_stream events over the
    // relay socket. First free value after ForegroundWindowResult=25.
    // Android adds the matching STREAM=26 constant.
    public const byte Stream = 26;
    public const byte JettReplySubmit = 60;
}

/// <summary>
/// Authenticated key agreement. Both sides know a shared secret (serial for pairing, device secret afterwards).
/// Hello = marker | role | nonce16 | P-256 public key (65) | HMAC(authKey, preceding bytes).
/// An attacker without the secret cannot produce a valid hello, so the relay cannot man-in-the-middle.
/// </summary>
public sealed class Handshake : IDisposable
{
    public const int HelloLength = 1 + 1 + 16 + 65 + 32;
    private readonly Role _role;
    private readonly Role? _expectedPeerRole;
    private readonly byte[] _authKey;
    private readonly ECDiffieHellman _ecdh;
    private readonly byte[] _helloBody;

    public Handshake(Role role, byte[] secret, Role? expectedPeerRole = null)
    {
        _role = role;
        _expectedPeerRole = expectedPeerRole;
        _authKey = Kdf.AuthKey(secret);
        _ecdh = ECDiffieHellman.Create(ECCurve.NamedCurves.nistP256);
        var p = _ecdh.ExportParameters(false);
        var body = new byte[1 + 1 + 16 + 65];
        body[0] = Wire.HelloMarker;
        body[1] = (byte)role;
        RandomNumberGenerator.Fill(body.AsSpan(2, 16));
        body[18] = 0x04;
        p.Q.X!.CopyTo(body, 19);
        p.Q.Y!.CopyTo(body, 19 + 32);
        _helloBody = body;
    }

    public byte[] CreateHello()
    {
        var hello = new byte[HelloLength];
        _helloBody.CopyTo(hello, 0);
        HMACSHA256.HashData(_authKey, _helloBody).CopyTo(hello, _helloBody.Length);
        return hello;
    }

    /// <summary>Validates the peer hello and derives the session. Returns null for anything invalid.</summary>
    public SecureSession? Complete(byte[] peerHello)
    {
        if (peerHello.Length != HelloLength || peerHello[0] != Wire.HelloMarker) return null;
        var peerRole = (Role)peerHello[1];
        var supportedRole = peerRole is Role.Host or Role.Client or Role.Connector;
        if (!supportedRole || peerRole == _role
            || (_expectedPeerRole is { } expected ? peerRole != expected : peerRole is not (Role.Host or Role.Client)))
            return null;
        var body = peerHello.AsSpan(0, HelloLength - 32);
        var mac = HMACSHA256.HashData(_authKey, body);
        if (!CryptographicOperations.FixedTimeEquals(mac, peerHello.AsSpan(HelloLength - 32))) return null;
        if (peerHello[18] != 0x04) return null;

        byte[] shared;
        try
        {
            using var peer = ECDiffieHellman.Create(new ECParameters
            {
                Curve = ECCurve.NamedCurves.nistP256,
                Q = new ECPoint { X = peerHello[19..51], Y = peerHello[51..83] },
            });
            shared = _ecdh.DeriveRawSecretAgreement(peer.PublicKey);
        }
        catch (CryptographicException)
        {
            return null;
        }

        var localIsHost = _role == Role.Host;
        var hostBody = localIsHost ? _helloBody : body.ToArray();
        var clientBody = localIsHost ? body.ToArray() : _helloBody;
        var transcript = new byte[hostBody.Length - 1 + clientBody.Length - 1];
        hostBody.AsSpan(1).CopyTo(transcript);
        clientBody.AsSpan(1).CopyTo(transcript.AsSpan(hostBody.Length - 1));
        var salt = SHA256.HashData([.. _authKey, .. transcript]);
        var okm = HKDF.DeriveKey(HashAlgorithmName.SHA256, shared, 68, salt, Encoding.UTF8.GetBytes("deskhand-v1 session"));
        var hostToClient = okm[..32];
        var clientToHost = okm[32..64];
        var code = (BinaryPrimitives.ReadUInt32BigEndian(okm.AsSpan(64, 4)) % 1_000_000u).ToString("D6");
        return localIsHost
            ? new SecureSession(hostToClient, clientToHost, code, _role, peerRole)
            : new SecureSession(clientToHost, hostToClient, code, _role, peerRole);
    }

    public void Dispose() => _ecdh.Dispose();
}

/// <summary>AES-256-GCM records with strictly increasing 64-bit counters (replay and reorder are rejected).</summary>
public sealed class SecureSession
{
    private readonly byte[] _sendKey;
    private readonly byte[] _receiveKey;
    private readonly Role _role;
    private readonly Role _peerRole;
    private long _sendCounter;
    private long _lastReceived = -1;
    // Serializes Seal: the counter IS the AES-GCM nonce, so two seals
    // must never read the same counter value. The Android port gets
    // this right with a @Synchronized seal(); this lock is the C#
    // equivalent, inside the session so no caller can forget it.
    private readonly object _sealLock = new();

    internal SecureSession(byte[] sendKey, byte[] receiveKey, string confirmCode, Role role, Role peerRole)
    {
        _sendKey = sendKey;
        _receiveKey = receiveKey;
        ConfirmCode = confirmCode;
        _role = role;
        _peerRole = peerRole;
    }

    /// <summary>Six digits both devices show so the user can spot a wrong-device pairing.</summary>
    public string ConfirmCode { get; }

    public byte[] Seal(byte type, ReadOnlySpan<byte> payload)
    {
        lock (_sealLock)
        {
            var counter = _sendCounter++;
            var plain = new byte[1 + payload.Length];
            plain[0] = type;
            payload.CopyTo(plain.AsSpan(1));
            var record = new byte[1 + 8 + plain.Length + 16];
            record[0] = Wire.RecordMarker;
            BinaryPrimitives.WriteInt64BigEndian(record.AsSpan(1, 8), counter);
            using var gcm = new AesGcm(_sendKey, 16);
            gcm.Encrypt(Nonce(counter), plain, record.AsSpan(9, plain.Length), record.AsSpan(9 + plain.Length, 16), Aad(_role));
            return record;
        }
    }

    /// <summary>Returns false if the record is malformed, tampered with, or replayed.</summary>
    public bool TryOpen(byte[] record, out byte type, out byte[] payload)
    {
        type = 0;
        payload = [];
        if (record.Length < 1 + 8 + 1 + 16 || record[0] != Wire.RecordMarker) return false;
        var counter = BinaryPrimitives.ReadInt64BigEndian(record.AsSpan(1, 8));
        if (counter <= _lastReceived) return false;
        var plain = new byte[record.Length - 9 - 16];
        try
        {
            using var gcm = new AesGcm(_receiveKey, 16);
            gcm.Decrypt(Nonce(counter), record.AsSpan(9, plain.Length), record.AsSpan(9 + plain.Length, 16), plain,
                Aad(_peerRole));
        }
        catch (CryptographicException)
        {
            return false;
        }
        _lastReceived = counter;
        type = plain[0];
        payload = plain[1..];
        return true;
    }

    private static byte[] Nonce(long counter)
    {
        var n = new byte[12];
        BinaryPrimitives.WriteInt64BigEndian(n.AsSpan(4), counter);
        return n;
    }

    private static byte[] Aad(Role sender) => [Wire.RecordMarker, (byte)sender];
}
