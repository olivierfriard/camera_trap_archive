"""Convert photos and retain native metadata plus exact source metadata blocks."""

import base64
import json
import struct
import zlib
from pathlib import Path
from tempfile import NamedTemporaryFile

from PIL import Image

from photo_media import md5_file


def source_metadata(path):
    """Read original container metadata, including unknown JPEG/PNG extensions."""
    data = Path(path).read_bytes()
    blocks = []
    if data.startswith(b'\x89PNG\r\n\x1a\n'):
        kind, offset = 'PNG', 8
        while offset < len(data):
            length = struct.unpack_from('>I', data, offset)[0]
            end = offset + length + 12
            if end > len(data):
                raise ValueError('Truncated PNG chunk')
            tag = data[offset+4:offset+8]
            if tag in (b'acTL', b'fcTL', b'fdAT'):
                raise ValueError('Animated PNG conversion is not supported')
            if tag != b'IDAT':
                blocks.append((tag.decode('ascii'), data[offset:end]))
            offset = end
            if tag == b'IEND':
                break
    elif data.startswith(b'\xff\xd8'):
        kind, offset = 'JPEG', 2
        while offset < len(data):
            if data[offset] != 255:
                raise ValueError('Invalid JPEG marker')
            start = offset
            while offset < len(data) and data[offset] == 255:
                offset += 1
            marker = data[offset]
            offset += 1
            if marker == 0xd9:
                break
            if marker in (0x01, 0xd8) or 0xd0 <= marker <= 0xd7:
                continue
            length = struct.unpack_from('>H', data, offset)[0]
            if length < 2 or offset + length > len(data):
                raise ValueError('Truncated JPEG segment')
            offset += length
            if 0xe0 <= marker <= 0xef or marker == 0xfe:
                blocks.append((f'{marker:02x}', data[start:offset]))
            if marker == 0xda:
                # Entropy-coded scans escape literal FF and contain restart markers.
                while offset < len(data):
                    offset = data.index(b'\xff', offset)
                    following = offset + 1
                    while data[following] == 255:
                        following += 1
                    if data[following] == 0 or 0xd0 <= data[following] <= 0xd7:
                        offset = following + 1
                    else:
                        break
    else:
        raise ValueError('Unsupported source photo')
    if offset < len(data):
        blocks.append(('trailer', data[offset:]))
    return dict(version=1, format=kind, blocks=[[tag, base64.b64encode(value).decode('ascii')] for tag, value in blocks])


def read_preserved_metadata(path):
    """Read the CTMD RIFF chunk containing exact source metadata blocks (JSON v1)."""
    with Path(path).open('rb') as stream:
        header = stream.read(12)
        if header[:4] != b'RIFF' or header[8:12] != b'WEBP':
            raise ValueError('Not a WebP file')
        while header := stream.read(8):
            if len(header) != 8:
                raise ValueError('Truncated WebP chunk')
            tag, size = struct.unpack('<4sI', header)
            if tag == b'CTMD':
                return json.loads(zlib.decompress(stream.read(size)))
            stream.seek(size + size % 2, 1)
    raise ValueError('Preserved source metadata is missing')


def convert_photo(source, target, expected_md5):
    """Publish a verified WebP atomically; leave the original untouched on failure."""
    source, target = Path(source), Path(target)
    if md5_file(source) != expected_md5:
        raise ValueError('Original photo no longer matches its registered hash')
    metadata = source_metadata(source)
    temporary = None
    try:
        with Image.open(source) as image:
            image.load()
            if image.mode not in ('RGB', 'RGBA', 'L', 'LA', 'P') or getattr(image, 'n_frames', 1) != 1:
                raise ValueError('Photo mode or animation cannot be safely converted to WebP')
            native = {key: image.info[key] for key in ('exif', 'icc_profile', 'xmp') if image.info.get(key)}
            if 'xmp' not in native and image.info.get('XML:com.adobe.xmp'):
                native['xmp'] = image.info['XML:com.adobe.xmp'].encode('utf8')
            dimensions = image.size
            with NamedTemporaryFile(dir=target.parent, prefix='.photo-webp-', suffix='.webp', delete=False) as file:
                temporary = Path(file.name)
            # Keep the source pixel orientation: its original EXIF orientation is retained.
            image.save(temporary, 'WEBP', quality=85, method=6, **native)
        payload = zlib.compress(json.dumps(metadata, separators=(',', ':')).encode('utf8'))
        with temporary.open('r+b') as output:
            output.seek(12)
            if output.read(4) != b'VP8X':
                # Custom chunks require the extended container, even without EXIF/ICC/XMP.
                output.seek(12)
                chunks = output.read()
                canvas = b'\0' * 4 + (dimensions[0]-1).to_bytes(3, 'little') + (dimensions[1]-1).to_bytes(3, 'little')
                output.seek(12)
                output.write(struct.pack('<4sI', b'VP8X', 10) + canvas + chunks)
            output.seek(0, 2)
            output.write(struct.pack('<4sI', b'CTMD', len(payload)))
            output.write(payload)
            if len(payload) % 2:
                output.write(b'\0')
            size = output.tell()
            if size - 8 > 0xffffffff:
                raise ValueError('WebP container exceeds its maximum size')
            output.seek(4)
            output.write(struct.pack('<I', size - 8))
        if read_preserved_metadata(temporary) != metadata:
            raise ValueError('Source metadata verification failed')
        with Image.open(temporary) as check:
            check.load()
            if check.format != 'WEBP' or check.size != dimensions:
                raise ValueError('WebP image verification failed')
            for key, value in native.items():
                actual = check.info.get(key, b'')
                if key == 'exif':
                    actual, value = actual.removeprefix(b'Exif\0\0'), value.removeprefix(b'Exif\0\0')
                if actual != value:
                    raise ValueError(f'WebP {key} metadata verification failed')
        if md5_file(source) != expected_md5:
            raise ValueError('Original photo changed during conversion')
        digest = md5_file(temporary)
        temporary.replace(target)
        return digest
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
