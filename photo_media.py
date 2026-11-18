"""Create photo previews and extract capture dates without changing originals."""

import hashlib
import io
import logging
import struct
import warnings
from argparse import Namespace
from datetime import datetime, timezone
from pathlib import Path
from tempfile import NamedTemporaryFile
from zoneinfo import ZoneInfo

from PIL import Image, ImageOps

import camtrap_banner_decoder

LOGGER = logging.getLogger(__name__)
PHOTO_EXTENSIONS = {'.jpg', '.jpeg', '.png'}


def photo_path(folder, filename):
    """Resolve only regular, non-symlink photo files directly inside the upload folder."""
    if not filename or Path(filename).name != filename or Path(filename).suffix.lower() not in PHOTO_EXTENSIONS:
        raise ValueError('Invalid photo filename')
    if filename.endswith('.preview-1280.jpg'):
        raise ValueError('A preview cannot be used as an original photo')
    path = Path(folder) / filename
    if path.is_symlink():
        raise ValueError('Photo symlinks are not allowed')
    return path


def md5_file(path):
    with Path(path).open('rb') as source:
        return hashlib.file_digest(source, 'md5').hexdigest()


def capture_candidate(value, offset, source, label, zone):
    """Normalize an EXIF or OCR capture time into the application's local timezone."""
    if not value:
        return None
    try:
        if isinstance(value, bytes):
            value = value.decode('ascii')
        stamp = datetime.strptime(str(value).strip().strip('\x00'), '%Y:%m:%d %H:%M:%S')
        tz = ZoneInfo(zone)
        if offset:
            if isinstance(offset, bytes):
                offset = offset.decode('ascii')
            tz = datetime.strptime(str(offset).strip().strip('\x00'), '%z').tzinfo
        stamp = stamp.replace(tzinfo=tz)
        if stamp.astimezone(timezone.utc).astimezone(tz).replace(tzinfo=None) != stamp.replace(tzinfo=None):
            return None
        local = stamp.astimezone(ZoneInfo(zone))
        return dict(source=source, label=label, date=local.strftime('%Y-%m-%d'), time=local.strftime('%H:%M:%S'), timestamp=stamp.isoformat())
    except (ValueError, TypeError, UnicodeError):
        return None


def prepare_photo(path, zone, ocr_timeout=5):
    """Validate an image, create an oriented JPEG preview and compare EXIF/OCR dates."""
    path = Path(path)
    preview = path.with_name(path.name + '.preview-1280.jpg')
    candidates = []
    messages = []
    temporary = None
    with warnings.catch_warnings():
        warnings.simplefilter('error', Image.DecompressionBombWarning)
        with Image.open(path) as image:
            if image.format not in ('JPEG', 'PNG'):
                raise ValueError('Formato non supportato. Usa JPEG o PNG.')
            if (image.format == 'JPEG' and path.suffix.lower() not in ('.jpg', '.jpeg')) or (image.format == 'PNG' and path.suffix.lower() != '.png'):
                raise ValueError('Il contenuto della foto non corrisponde alla sua estensione.')
            image.load()
            try:
                exif = image.getexif()
                nested = exif.get_ifd(0x8769) if exif else {}
                for tag, offset, source, label in (
                    (36867, 36881, 'exif_original', 'EXIF originale'),
                    (36868, 36882, 'exif_creation', 'EXIF creazione'),
                ):
                    candidate = capture_candidate(nested.get(tag, exif.get(tag)), nested.get(offset, exif.get(offset)), source, label, zone)
                    if candidate:
                        candidates.append(candidate)
            except (ValueError, TypeError, KeyError, AttributeError, OSError, SyntaxError, struct.error):
                messages.append('Metadati EXIF non leggibili; verifica la data manualmente.')
            try:
                oriented = ImageOps.exif_transpose(image).convert('RGB')
            except (ValueError, TypeError, KeyError, AttributeError, OSError, SyntaxError, struct.error):
                oriented = image.convert('RGB')
                messages.append("Orientamento EXIF non leggibile; verifica l'anteprima.")
            oriented.thumbnail((1280, 1280), Image.Resampling.LANCZOS)
            try:
                with NamedTemporaryFile(dir=path.parent, prefix='.photo-preview-', suffix='.jpg', delete=False) as file:
                    temporary = Path(file.name)
                oriented.save(temporary, 'JPEG', quality=85)
                temporary.replace(preview)
            finally:
                if temporary:
                    temporary.unlink(missing_ok=True)
                oriented.close()
        image.close()
    args = Namespace(debug=False, ocr_timeout=ocr_timeout, ocr_error=None)
    try:
        data = camtrap_banner_decoder.extract_date_time(path, args)
        if data.get('date') and data.get('time'):
            date = datetime.strptime(data['date'], '%Y-%m-%d')
            clock = datetime.strptime(data['time'], '%H%M%S')
            candidate = capture_candidate(
                date.strftime('%Y:%m:%d') + clock.strftime(' %H:%M:%S'), None,
                'ocr', 'Banda OCR', zone,
            )
            if candidate:
                candidates.append(candidate)
        if args.ocr_error:
            messages.append('Lettura della banda non disponibile; puoi inserire data e ora manualmente.')
    except Exception:
        LOGGER.exception('Photo banner extraction failed: %s', path.name)
        messages.append('Lettura della banda non disponibile; puoi inserire data e ora manualmente.')
    conflict = len({(item['date'], item['time']) for item in candidates}) > 1
    if conflict:
        messages.append('Le date EXIF e della banda non coincidono. Scegli la data corretta.')
    if not candidates:
        messages.append('Data e ora non rilevate. Inseriscile manualmente se disponibili.')
    chosen = candidates[0] if candidates else {}
    return dict(preview_file_name=preview.name, capture_metadata=dict(
        candidates=candidates, initial_source=chosen.get('source'), warnings=messages, conflict=conflict,
    ), date=chosen.get('date', ''), time_=chosen.get('time', ''))


def thumbnail_bytes(preview):
    with Image.open(preview) as image:
        image = image.convert('RGB')
        image.thumbnail((640, 640), Image.Resampling.LANCZOS)
        output = io.BytesIO()
        image.save(output, 'JPEG', quality=85)
        return output.getvalue()
