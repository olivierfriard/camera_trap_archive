"""
camtrap_banner_decoder
Copyright 2024-2026 Olivier Friard


Rename files using the date and time extracted from the bottom banner of camera-trap images and videos.
Video files can be re-encoded with FFmpeg (if available).
The 'Date/Time Original' and 'Media Create Date' metadata fields are set with ExifTool (if available).

Requires: Tesseract (see https://github.com/tesseract-ocr/tesseract)

For moon phase calculations, see:
https://pyorbital.readthedocs.io/en/feature-moon-phase/moon_calculations.html


  camtrap_banner_decoder is free software; you can redistribute it and/or modify
  it under the terms of the GNU General Public License as published by
  the Free Software Foundation; either version 3 of the License, or
  any later version.

  camtrap_banner_decoder is distributed in the hope that it will be useful,
  but WITHOUT ANY WARRANTY; without even the implied warranty of
  MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
  GNU General Public License for more details.

  You should have received a copy of the GNU General Public License
  along with this program; if not see <http://www.gnu.org/licenses/>.


"""

import argparse
import re
import shutil
import subprocess
import sys
from datetime import datetime, time
from pathlib import Path

import cv2
import pytesseract

__version__ = "0.0.3"


VIDEO_EXTENSIONS = (".avi", ".mp4", ".mov")
IMAGES_EXTENSIONS = (".jpg", ".jpeg")
EXTENSIONS = set(VIDEO_EXTENSIONS + IMAGES_EXTENSIONS)


def banner_text_from_frame(
    frame,
    args,
    roi_height_fraction: float = 0.15,
    position: str = "bottom",
    file_path="",
):
    """
    Extract text from the banner of a frame.
    """

    extracted_text = None
    # Get frame dimensions
    frame_height, frame_width, _ = frame.shape

    if args.debug:
        print(
            f"Original image dimensions: {frame_height}x{frame_width}", file=sys.stderr
        )

    if frame_width > 2592:
        aspect_ratio = frame_height / frame_width
        new_width = 1280
        new_height = int(new_width * aspect_ratio)
        frame = cv2.resize(frame, (new_width, new_height))
        frame_height, frame_width, _ = frame.shape

        if args.debug:
            print(
                f"Resized image dimensions: {frame_height}x{frame_width}",
                file=sys.stderr,
            )

    # Define the region of interest (ROI)
    roi_height = int(frame_height * roi_height_fraction)
    if position == "bottom":
        roi = frame[frame_height - roi_height : frame_height, 0:frame_width]
    if position == "up":
        roi = frame[0:roi_height:frame_height, 0:frame_width]

    # Save the first sampled frame for inspection
    if file_path and args.debug:
        cv2.imwrite(Path("/tmp") / Path(file_path).with_suffix(".webp").name, roi)

    # Convert ROI to grayscale
    roi_gray = cv2.cvtColor(roi, cv2.COLOR_BGR2GRAY)

    # Apply OCR to extract text
    try:
        extracted_text = pytesseract.image_to_string(roi_gray, config="--psm 6")
    except Exception as e:
        print("Tesseract error: {e}", file=sys.stderr)
        sys.exit()

    if args.debug:
        print(f"{extracted_text=}", file=sys.stderr)

    return extracted_text


def extract_banner_text_from_video(
    video_path, args, frame_interval=30, roi_height_fraction=0.15
):
    """
    Extract text from the bottom banner of a video.

    Args:
        video_path (str): Path to the video file.
        args: Parsed command-line arguments.
        frame_interval (int): Interval at which frames are sampled.
        roi_height_fraction (float): Fraction of the frame height that contains the banner.

    Returns:
        str: Text extracted from the video banner, or None if no frame is read.
    """

    extracted_text = None

    # Open the video file
    video_capture = cv2.VideoCapture(video_path)

    frame_count = 0

    while True:
        ret, frame = video_capture.read()
        if not ret:
            break

        # Process every `frame_interval` frame
        if frame_count % frame_interval == 0:
            extracted_text = banner_text_from_frame(
                frame, args, roi_height_fraction, position="bottom"
            )
            break

        frame_count += 1

    # Release video capture
    video_capture.release()

    return extracted_text


def extract_banner_text_from_image(image_path, args, roi_height_fraction=0.15):
    """
    Extract text from the bottom banner of an image.
    """
    frame = cv2.imread(image_path)
    if frame is None:
        return "Error: Unable to load the image. Check the file path."

    extracted_text = banner_text_from_frame(
        frame, args, roi_height_fraction, position="bottom", file_path=image_path
    )

    return extracted_text


def extract_date_hhmmss(text, date_orders=("MDY", "DMY", "YMD")):

    DATE_TOKEN = (
        r"(?:\d{4}([/-])\d{2}\1\d{2}"
        r"|\d{2}([/-])\d{2}\2\d{4})"
    )

    DATE = re.compile(r"(?<!\d)" + DATE_TOKEN + r"(?=\d{2}:|[^\d]|$)")

    HMS = re.compile(
        r"(?<!\d)(?P<h>\d{2})\s*:\s*(?P<m>\d{2})"
        r"\s*[:.°]\s*(?P<s>\d{2,})(?!\d)"
    )

    COMPACT = re.compile(r"(?<!\d)(?P<h>\d{2}):(?P<m>\d{2})(?P<s>\d{2})(?!\d)")

    AMPM = re.compile(
        r"(?<![A-Za-z0-9:])"
        r"(?P<h>[0-9OITl]{1,2})\s*:?\s*(?P<m>\d{2})"
        r"\s*(?P<ampm>[APap][Mm])(?![A-Z0-9])"
    )

    formats = {
        "YMD": "%Y/%m/%d",
        "DMY": "%d/%m/%Y",
        "MDY": "%m/%d/%Y",
    }

    # Also accept "M/D/Y", "D-M-Y", etc.
    if isinstance(date_orders, str):
        date_orders = [date_orders]

    orders = list(
        dict.fromkeys(re.sub(r"[/\-\s]", "", order.upper()) for order in date_orders)
    )

    if not orders or any(order not in formats for order in orders):
        raise ValueError("Allowed date orders: YMD, DMY, MDY")

    warnings = []
    selected_dates = set()
    date_candidates = set()

    for match in DATE.finditer(text):
        raw = match.group().replace("-", "/")
        valid = []

        for order in orders:
            try:
                value = datetime.strptime(raw, formats[order]).date().isoformat()
                valid.append((order, value))
            except ValueError:
                continue

        if not valid:
            warnings.append(
                f"Invalid date for the specified date orders: {match.group()}"
            )
            continue

        # Priority follows the order of the list.
        selected_order, selected_date = valid[0]
        selected_dates.add(selected_date)

        alternatives = {value for _, value in valid}
        date_candidates.update(alternatives)

        if len(alternatives) > 1:
            warnings.append(
                f"Ambiguous date: {match.group()} → {selected_date} "
                f"(priority {selected_order})"
            )

    if len(selected_dates) > 1:
        warnings.append("Multiple distinct dates: check the OCR output")

    # Mask dates before searching for times.
    spans = [m.span() for m in re.finditer(DATE_TOKEN, text)]
    masked = list(text)
    for start, end in spans:
        masked[start:end] = " " * (end - start)
    masked = "".join(masked)

    times = {}
    used = []

    for mode, pattern in [
        ("second", HMS),
        ("compact", COMPACT),
        ("minute", AMPM),
    ]:
        for match in pattern.finditer(masked):
            if any(match.start() < end and match.end() > start for start, end in used):
                continue

            # Accept HH:MMSS only when it follows a nearby date.
            if mode == "compact" and not any(
                0 <= match.start() - end <= 4 for _, end in spans
            ):
                continue

            used.append(match.span())

            raw_hour = match["h"]
            hour = int(
                raw_hour.translate(
                    str.maketrans(
                        {
                            "O": "0",
                            "I": "1",
                            "T": "1",
                            "l": "1",
                        }
                    )
                )
            )
            minute = int(match["m"])
            second = 0 if mode == "minute" else int(match["s"])

            if mode != "minute" and len(match["s"]) != 2:
                warnings.append(f"Invalid time: {match.group()}")
                continue

            if mode == "minute":
                if not 1 <= hour <= 12:
                    warnings.append(f"Invalid AM/PM time: {match.group()}")
                    continue

                hour = hour % 12 + (12 if match["ampm"].upper() == "PM" else 0)

            try:
                value = time(hour, minute, second).strftime("%H%M%S")
            except ValueError:
                warnings.append(f"Invalid time: {match.group()}")
                continue

            precision = "minute" if mode == "minute" else "second"
            if value not in times or precision == "second":
                times[value] = precision

            if mode == "minute":
                warnings.append("Seconds missing: using 00 for HHMMSS")
                if not raw_hour.isdigit() or ":" not in match.group():
                    warnings.append(f"OCR time correction: {match.group()}")
            elif mode == "compact" or "." in match.group() or "°" in match.group():
                warnings.append(f"OCR time separator normalized: {match.group()}")

    if len(times) > 1:
        warnings.append("Multiple times: check the OCR output")

    hhmmss = next(iter(times)) if len(times) == 1 else ""

    return {
        "date": (next(iter(selected_dates)) if len(selected_dates) == 1 else ""),
        "date_candidates": sorted(date_candidates),
        "hhmmss": hhmmss,
        "time_precision": times[hhmmss] if hhmmss else None,
        "warnings": warnings,
    }


def extract_temperatures(text):

    NUMBER = r"[+-]?\s*(?:0|[1-9]\d{0,2})(?:[.,]\d+)?"

    TEMPERATURE = re.compile(
        rf"(?<![\w.,:+-])"
        rf"(?P<value>{NUMBER})\s*"
        rf"(?:\.?\s*°\s*%?\s*)?"
        rf"(?P<unit>[CF])\b"
    )

    # Number immediately before a Fahrenheit temperature, with its Celsius unit missing.
    BEFORE_F = re.compile(
        rf"(?<![\w.,:+-])"
        rf"(?P<value>{NUMBER})\s*[°%]?\s*(?:/\s*)?$"
    )

    def as_number(text):
        return float(re.sub(r"\s+", "", text).replace(",", "."))

    text = text.translate(
        str.maketrans(
            {
                "−": "-",
                "–": "-",
                "º": "°",
                "℃": "°C",
                "℉": "°F",
            }
        )
    )

    # Observed cases: @M-4°C and @€M5°C.
    text = re.sub(r"(?<=[@€])M(?=[+-]?\d)", "", text)

    # Limit this correction to the OC 32°F pair.
    text, corrections = re.subn(
        r"(?<!\w)O(?=\s*C\b\s*/?\s*32\s*°?\s*F\b)",
        "0",
        text,
    )

    warnings = ["OCR correction: OC → 0C"] if corrections else []
    hits = {"C": [], "F": []}

    for match in TEMPERATURE.finditer(text):
        value = as_number(match["value"])
        unit = match["unit"]

        # Filter values by temperature unit.
        valid = -100 <= value <= 100 if unit == "C" else -148 <= value <= 212

        if valid:
            hits[unit].append((value, match.start()))
        else:
            warnings.append(f"Value out of range: {match.group().strip()}")

    result = {"c": None, "f": None, "warnings": warnings}

    for unit, candidates in hits.items():
        values = {value for value, _ in candidates}
        if len(values) == 1:
            result[unit.lower()] = values.pop()
        elif values:
            warnings.append(f"Multiple values for {unit}")

    # Recover a value whose Celsius unit is missing.
    if not hits["C"] and result["f"] is not None:
        guesses = set()

        for fahrenheit, start in hits["F"]:
            match = BEFORE_F.search(text[:start])
            if match:
                celsius = as_number(match["value"])
                if (
                    -100 <= celsius <= 100
                    and abs(fahrenheit - (1.8 * celsius + 32)) <= 2
                ):
                    guesses.add(celsius)

        if len(guesses) == 1:
            result["c"] = guesses.pop()
            warnings.append("Celsius unit inferred from the C/F pair")

    if result["c"] is not None and result["f"] is not None:
        if abs(result["f"] - (1.8 * result["c"] + 32)) > 2:
            warnings.append("Inconsistent C/F pair")

    return result


def extract_date_time(path_file, args):
    """
    Extract information from the image or video banner.
    """

    banner_text = None

    if Path(path_file).suffix.lower() in VIDEO_EXTENSIONS:
        banner_text = extract_banner_text_from_video(path_file, args)

    if Path(path_file).suffix.lower() in IMAGES_EXTENSIONS:
        banner_text = extract_banner_text_from_image(path_file, args)

    patterns = [r"\d{2}-\d{2}-\d{4}", r"\d{2}/\d{2}/\d{4}"]

    temperature_f = ""
    date = ""
    temperature_c = ""
    cam_id = ""
    hhmmss = ""
    all_txt = ""

    if banner_text is not None:
        for text in banner_text.split("\n"):
            if args.debug:
                print(f"Extracted text: {text}")
            all_txt += text
            # The input string
            # text = "@ FOSA_01 73F 23C @ 06-09-2023 13:41:51"
            # text = "oo) @M-5°C / 23°F. 17/02/2025 00:09:12 = S.F. Attimis\n"

            # Try to extract the date
            """
            if not date:
                for pattern in patterns:
                    date_match = re.search(pattern, text)
                    if date_match:
                        raw_date = date_match.group(0)
                        if args.debug:
                            print(f"Extracted date: {raw_date}")
                        match pattern:
                            case r"\d{2}-\d{2}-\d{4}":
                                raw_date_splitted = raw_date.split("-")
                                year = raw_date_splitted[2]
                                month = raw_date_splitted[0]
                                day = raw_date_splitted[1]
                                # date = f"{raw_date_splitted[2]}-{raw_date_splitted[0]}-{raw_date_splitted[1]}"
                            case r"\d{2}/\d{2}/\d{4}":
                                raw_date_splitted = raw_date.split("/")
                                year = raw_date_splitted[2]
                                month = raw_date_splitted[0]
                                day = raw_date_splitted[1]
                        if int(month) > 12:
                            day, month = month, day

                        date = f"{year}-{month}-{day}"
                        if args.debug:
                            print(f"ISO date: {date}")
            """
            if not date or not hhmmss:
                dt = extract_date_hhmmss(text)
                if dt["date"]:
                    date = dt["date"]
                if dt["hhmmss"]:
                    hhmmss = dt["hhmmss"]

            """
            if dt["date"] or dt["hhmmss"]:
                dt["file_name"] = str(Path(path_file).name)
                print(dt)
            """

            # Extract time (HH:MM:SS)
            if not hhmmss:
                time_match = re.search(r"\d{2}:\d{2}:\d{2}", text)
                if time_match:
                    raw_time = time_match.group(0)
                    if args.debug:
                        print(f"Extracted time: {raw_time}")
                    hhmmss = raw_time.replace(":", "")
                    if args.debug:
                        print(f"HHMMSS: {hhmmss}")

            # Extract temperature in Fahrenheit (e.g., 73F)
            """
            if not temperature_f:
                temp_f_match = re.search(r" \d+F ", text)
                if temp_f_match:
                    raw_temperature_f = temp_f_match.group(0)
                    temperature_f = raw_temperature_f.strip()

            # Extract temperature in Celsius (e.g., 23C)
            if not temperature_c:
                temp_c_match = re.search(r" \d+C ", text)
                if temp_c_match:
                    raw_temperature_c = temp_c_match.group(0)
                    temperature_c = raw_temperature_c.strip()
            """

            temp = extract_temperatures(text)
            if temp["c"]:
                temperature_c = temp["c"]
            if temp["f"]:
                temperature_f = temp["f"]

            """
            if temp["c"] or temp["f"]:
                temp["file_name"] = str(Path(path_file).name)
                print(extract_temperatures(text))
            """

            # Check for the camera ID
            """
            text2 = text
            if date:
                text2 = text2.replace(raw_date, "")
            if hhmmss:
                text2 = text2.replace(raw_time, "")
            if temperature_c:
                text2 = text2.replace(f"{temperature_c}", "")
            if temperature_f:
                text2 = text2.replace(f"{temperature_f}", "")

            while "  " in text2:
                text2 = text2.replace("  ", " ")

            if args.debug:
                print(f"{text=}", file=sys.stderr)
                print(f"{text2=}", file=sys.stderr)

            if not cam_id:
                if args.cam_id != "NO":
                    try:
                        cam_id = sorted(text2.split(" "), key=len, reverse=True)[0]
                    except Exception:
                        pass

                if args.debug:
                    print(f"{cam_id=}", file=sys.stderr)
            """

    return {
        "file_path": path_file,
        "file_name": Path(path_file).name,
        "text": all_txt,
        "cam_id": cam_id,
        "date": date,
        "time": hhmmss,
        "temperature_c": temperature_c,
        "temperature_f": temperature_f,
    }


def get_new_file_path(args, file_path: Path, data: dict) -> Path:
    """
    Return the new file path.
    """
    if args.output_directory:
        dir = Path(args.output_directory)
    else:
        dir = Path(file_path).parent
    if args.rename and (data["date"] and data["time"]):
        suffix = f"{data['date']}_{data['time']}"
        if data["cam_id"]:
            suffix += f"_{data['cam_id']}"
        new_file_path = dir / f"{suffix}|{file_path.name}"
    else:
        new_file_path = dir / file_path.name
    return new_file_path


def parse_arguments():
    """
    Parse command-line arguments.
    """

    parser = argparse.ArgumentParser(
        description="Extract the date and time from image and video banners and optionally rename the files"
    )

    parser.add_argument(
        "-d",
        "--directory",
        action="store",
        dest="input_directory",
        default="",
        help="Directory containing media files",
    )
    parser.add_argument(
        "-o",
        "--output",
        action="store",
        dest="output_directory",
        default="",
        help="Output directory",
    )

    parser.add_argument(
        "-p",
        "--pattern",
        action="store",
        dest="pattern",
        default="*",
        help="File selection pattern",
    )
    parser.add_argument(
        "--cam-id",
        action="store",
        dest="cam_id",
        default="NO",
        help="Camera ID: NO to omit it, EXTRACT to read it from the banner, or a custom value",
    )
    parser.add_argument(
        "--rename", action="store_true", dest="rename", default="", help="Rename files"
    )
    parser.add_argument(
        "--tesseract",
        action="store",
        dest="tesseract_cmd",
        default="tesseract",
        help="Path to the Tesseract executable",
    )
    parser.add_argument(
        "--ffmpeg",
        action="store",
        dest="ffmpeg_path",
        default="ffmpeg",
        help="Path to the FFmpeg executable",
    )

    parser.add_argument(
        "--exiftool",
        action="store",
        dest="exiftool_path",
        default="exiftool",
        help="Path to the ExifTool executable",
    )

    parser.add_argument(
        "--reencode",
        action="store_true",
        dest="reencode",
        default="",
        help="Re-encode files with FFmpeg",
    )

    parser.add_argument(
        "--debug", action="store_true", dest="debug", help="Enable debug mode"
    )
    parser.add_argument(
        "-v",
        "--version",
        action="store_true",
        dest="version",
        help="Display the version",
    )

    # Parse the command-line arguments
    return parser.parse_args()


def main():
    args = parse_arguments()

    if args.version:
        print(f"camtrap_banner_decoder v. {__version__}\n")
        sys.exit()

    if args.tesseract_cmd:
        if args.tesseract_cmd == "tesseract" or Path(args.tesseract_cmd).is_file():
            pytesseract.pytesseract.tesseract_cmd = args.tesseract_cmd
            if args.tesseract_cmd != "tesseract":
                print(f"Using {args.tesseract_cmd}", file=sys.stderr)
        else:
            print(
                f"Tesseract executable not found: {args.tesseract_cmd}",
                file=sys.stderr,
            )
            sys.exit()

    if Path(args.input_directory).is_dir():
        input_dir = args.input_directory
    else:
        print(f"Directory {args.input_directory} not found", file=sys.stderr)
        sys.exit()

    if args.debug:
        print(f"{input_dir=}")

    if args.output_directory and not Path(args.output_directory).is_dir():
        print(f"Output directory {args.output_directory} not found", file=sys.stderr)
        sys.exit()

    files = sorted(list(Path(input_dir).glob(args.pattern)))

    for file_path in files:
        print(str(file_path), file=sys.stderr)

        data = extract_date_time(str(file_path), args)

        print(data)  # Remove before release

        if args.debug:
            print(f"{data['temperature_c']=}   {data['temperature_f']=}")

        # if data["date"] and data["time"]:

        if args.cam_id == "NO":  # , "EXTRACT"):
            data["cam_id"] = ""
        elif args.cam_id == "EXTRACT":
            if data["cam_id"] is None:
                data["cam_id"] = "CAM-ID"
        else:
            data["cam_id"] = args.cam_id

        new_file_path = get_new_file_path(args, file_path, data)

        if args.output_directory:
            if new_file_path.is_file():
                print(f"{Path(new_file_path).name} already exists")
            else:
                if args.reencode:
                    if Path(file_path).suffix.lower() in VIDEO_EXTENSIONS:
                        next_ext = ".webm"
                    if Path(file_path).suffix.lower() in IMAGES_EXTENSIONS:
                        next_ext = ".webp"
                    if new_file_path.with_suffix(next_ext).is_file():
                        print(
                            f"Error re-encoding: {new_file_path.with_suffix(next_ext)} already exists"
                        )
                    else:
                        command = f'{args.ffmpeg_path} -i "{file_path}" "{new_file_path.with_suffix(next_ext)}"'
                        print(
                            f"Re-encoding {file_path.name} to {new_file_path.with_suffix(next_ext)}"
                        )
                        p = subprocess.Popen(
                            command,
                            stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE,
                            shell=True,
                        )
                        out, error = p.communicate()

                        new_file_path = new_file_path.with_suffix(next_ext)
                else:
                    # Copy the file
                    shutil.copy(file_path, new_file_path)
                    print(f"{Path(file_path).name} copied to {Path(new_file_path)}")

                # Save the date and time in the metadata
                time_exiftool = (
                    f"{data['time'][0:2]}:{data['time'][2:4]}:{data['time'][4:6]}"
                )
                command = (
                    f"{args.exiftool_path} "
                    f'-DateTimeOriginal="{data["date"]} {time_exiftool}" '
                    f'-CreateDate="{data["date"]} {time_exiftool}" '
                    f'-ModifyDate="{data["date"]} {time_exiftool}" '
                    f'-MediaCreateDate="{data["date"]} {time_exiftool}" '
                    f'-MediaModifyDate="{data["date"]} {time_exiftool}" '
                    f'-TrackCreateDate="{data["date"]} {time_exiftool}" '
                    f'-TrackModifyDate="{data["date"]} {time_exiftool}" '
                    f'-overwrite_original "{new_file_path}"'
                )

                p = subprocess.Popen(
                    command,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    shell=True,
                )
                out, error = p.communicate()
        else:
            """
            if new_file_path.is_file():
                print(f"{Path(new_file_path).name} already exists")
            else:
                print(
                    f"Rename {Path(file_path).name} to {Path(new_file_path).name}"
                )
            """

        if args.debug:
            print("-" * 30, file=sys.stderr)


if __name__ == "__main__":
    main()
