"""Read a transcript whatever the exporter saved it as.

WHY THIS EXISTS. A transcript arrives as whatever Windows wrote when
somebody pressed Save. Notepad's "Unicode" is UTF-16 LE; Word and
PowerShell redirection produce their own variants; a paste through an
older tool can land as cp1252. Reading them all as UTF-8 fails on the
first byte with

    UnicodeDecodeError: 'utf-8' codec can't decode byte 0xff in
    position 0: invalid start byte

which names the byte and not the cause, at the end of a rebuild, to
somebody who did nothing wrong except save a file the ordinary way.

The byte-order mark identifies the encoding unambiguously when present;
without one, UTF-8 is tried and then cp1252, which is Windows' default
and cannot fail (every byte is a character) -- so this returns text or
raises something that actually explains itself.
"""
import codecs

_BOMS = (
    (codecs.BOM_UTF8, "utf-8-sig"),
    (codecs.BOM_UTF32_LE, "utf-32"),      # before UTF-16 LE: FF FE 00 00
    (codecs.BOM_UTF32_BE, "utf-32"),      # shares its prefix with UTF-16 LE
    (codecs.BOM_UTF16_LE, "utf-16"),
    (codecs.BOM_UTF16_BE, "utf-16"),
)


def detect_encoding(raw):
    """-> (encoding, why) for a bytes prefix."""
    for bom, enc in _BOMS:
        if raw.startswith(bom):
            return enc, "byte-order mark"
    # NUL first, before any decode is attempted. A BOM-less UTF-16 file
    # is ASCII text with a zero byte beside every character, and there are
    # two ways that fools a decoder: pure-ASCII content DECODES as UTF-8
    # (nulls read as NUL characters -- silent garbage, worse than the
    # error it replaced), while content with any accent FAILS to decode
    # and lands in the single-byte fallback as mojibake. Testing for NUL
    # up front catches both, and costs nothing: real single-byte text
    # never contains a zero byte.
    head = raw[:4096]
    if b"\x00" in head:
        even = sum(1 for i, b in enumerate(head) if b == 0 and i % 2 == 0)
        odd = sum(1 for i, b in enumerate(head) if b == 0 and i % 2 == 1)
        return (("utf-16-be", "no BOM; nulls lead each pair")
                if even > odd else
                ("utf-16-le", "no BOM; nulls follow each pair"))
    try:
        raw.decode("utf-8")
        return "utf-8", "valid utf-8"
    except UnicodeDecodeError:
        return "cp1252", "not valid utf-8; read as Windows cp1252"


def read_text(path, verbose=False):
    """A transcript's text, from any of the encodings exporters produce.

    Newlines come back as \n whatever the file used. This function reads
    BYTES (it must -- encoding detection needs them), which sidesteps
    Python's universal-newline translation, so a Windows file would
    otherwise hand every caller lines ending in \r. Most parsers split
    with splitlines() and never notice; anything that splits on \n
    inherits an invisible carriage return glued to every utterance --
    and a quote or a content hash then differs between two machines by
    a byte nobody can see. Caught the first time the suite ever ran on
    Windows, which was also the first day the kits were built there.
    """
    with open(path, "rb") as f:
        raw = f.read()
    enc, why = detect_encoding(raw)
    if verbose and enc not in ("utf-8", "utf-8-sig"):
        print("  ({0} read as {1}: {2})".format(path, enc, why))
    return raw.decode(enc).replace("\r\n", "\n").replace("\r", "\n")
