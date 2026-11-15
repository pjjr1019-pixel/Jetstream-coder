import importlib.util
from pathlib import Path
import struct
import tempfile
import unittest
import zipfile

spec=importlib.util.spec_from_file_location('native_verify',Path(__file__).resolve().parents[1]/'scripts'/'verify-android-native.py')
native=importlib.util.module_from_spec(spec)
spec.loader.exec_module(native)

def elf(alignment=16384,offset=0,address=0):
    data=bytearray(128)
    data[:6]=b'\x7fELF\x02\x01'
    struct.pack_into('<Q',data,32,64)
    struct.pack_into('<HH',data,54,56,1)
    struct.pack_into('<I',data,64,1)
    struct.pack_into('<QQ',data,72,offset,address)
    struct.pack_into('<Q',data,112,alignment)
    return bytes(data)

class NativeAlignmentTests(unittest.TestCase):
    def test_rejects_4k_load_segments(self):
        self.assertFalse(native.inspect_elf(elf(4096))['compatible'])

    def test_rejects_incongruent_offsets_despite_large_alignment(self):
        self.assertFalse(native.inspect_elf(elf(offset=4096,address=0))['compatible'])

    def test_final_apk_checks_zip_alignment_separately(self):
        with tempfile.TemporaryDirectory() as folder:
            file=Path(folder)/'bad.apk'
            with zipfile.ZipFile(file,'w',compression=zipfile.ZIP_STORED) as archive:
                archive.writestr('lib/arm64-v8a/libfixture.so',elf())
            result=native.inspect_archive(file)[0]
            self.assertTrue(result['compatible'])
            self.assertFalse(result['zipAligned'])

    def test_rejects_non_elf_and_empty_archives(self):
        with self.assertRaises(ValueError):native.inspect_elf(b'not a library')
        with tempfile.TemporaryDirectory() as folder:
            file=Path(folder)/'empty.aar'
            with zipfile.ZipFile(file,'w'):pass
            with self.assertRaises(ValueError):native.inspect_archive(file)
