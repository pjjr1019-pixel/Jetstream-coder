using System.Security.Cryptography;
using System.Text;

namespace Deskhand.Core;

/// <summary>Single-use pairing serial: 20 Crockford base32 characters (100 bits), shown as XXXXX-XXXXX-XXXXX-XXXXX.</summary>
public static class Serial
{
    private const string Alphabet = "0123456789ABCDEFGHJKMNPQRSTVWXYZ";
    public const int Length = 20;

    public static string Generate()
    {
        var bytes = RandomNumberGenerator.GetBytes(Length);
        var chars = new char[Length];
        for (var i = 0; i < Length; i++)
        {
            chars[i] = Alphabet[bytes[i] & 31];
        }
        return new string(chars);
    }

    public static string Format(string normalized) =>
        string.Join('-', Enumerable.Range(0, 4).Select(i => normalized.Substring(i * 5, 5)));

    /// <summary>Returns the 20-character canonical form, or null if the input is not a valid serial.</summary>
    public static string? Normalize(string? input)
    {
        if (input is null) return null;
        var sb = new StringBuilder();
        foreach (var c in input.ToUpperInvariant())
        {
            if (c is '-' or ' ') continue;
            var m = c switch { 'I' or 'L' => '1', 'O' => '0', _ => c };
            if (Alphabet.IndexOf(m) < 0) return null;
            sb.Append(m);
        }
        return sb.Length == Length ? sb.ToString() : null;
    }

    public static byte[] ToSecret(string normalized) => Encoding.UTF8.GetBytes(normalized);
}
