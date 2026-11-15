namespace Deskhand.Windows.App;

// The encoder inserts access-unit delimiters. A start code may cross any pipe read boundary.
public static class H264AccessUnits
{
    public static void Read(Stream source, Action<byte[], bool> onFrame)
    {
        using var input = new BufferedStream(source, 64 * 1024);
        using var nal = new MemoryStream();
        using var unit = new MemoryStream();
        var zeros = 0;
        var hasPicture = false;
        var keyFrame = false;

        void FinishNal()
        {
            if (nal.Length <= 4) { nal.SetLength(0); nal.Position = 0; return; }
            var type = nal.GetBuffer()[4] & 0x1f;
            if (type == 9 && hasPicture)
            {
                onFrame(unit.ToArray(), keyFrame);
                unit.SetLength(0);
                unit.Position = 0;
                hasPicture = false;
                keyFrame = false;
            }
            if (type is 1 or 5) hasPicture = true;
            if (type == 5) keyFrame = true;
            nal.Position = 0;
            nal.CopyTo(unit);
            nal.SetLength(0);
            nal.Position = 0;
            if (unit.Length > 4 * 1024 * 1024) throw new InvalidDataException("Video access unit exceeds the maximum size.");
        }

        int next;
        while ((next = input.ReadByte()) >= 0)
        {
            if (next == 1 && zeros >= 2)
            {
                nal.SetLength(nal.Length - zeros);
                FinishNal();
                nal.WriteByte(0); nal.WriteByte(0); nal.WriteByte(0); nal.WriteByte(1);
                zeros = 0;
            }
            else
            {
                nal.WriteByte((byte)next);
                zeros = next == 0 ? zeros + 1 : 0;
                if (nal.Length > 4 * 1024 * 1024) throw new InvalidDataException("Video NAL unit exceeds the maximum size.");
            }
        }
        FinishNal();
        if (hasPicture) onFrame(unit.ToArray(), keyFrame);
    }
}
