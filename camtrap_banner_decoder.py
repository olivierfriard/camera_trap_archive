"""
camtrap_banner_decoder
Copyright 2024-2025 Olivier Friard


Rename file using date and time extracted from the camera-trap bottom video/picture banner
Video files can be re-encoded with ffmpeg (if present)
Metadata 'Date/Time Original' and 'Media Create Date' are set with exiftool (if present)

Require: tesseract program (see https://github.com/tesseract-ocr/tesseract)

Usage:
python camtrap_banner_decoder.py.py -d INPUT_DIRECTORY
    show extracted information

python camtrap_banner_decoder.py.py -d INPUT_DIRECTORY --debug
    show more information

python camtrap_banner_decoder.py.py -d INPUT_DIRECTORY --rename
    show extracted information and rename file like YYYY-MM-DD_hhmmss_CAMTRAP-ID_OLD-FILE-NAME

python camtrap_banner_decoder.py.py -d INPUT_DIRECTORY -o OUTPUT_DIRECTORY --rename
    show extracted information and rename file like YYYY-MM-DD_hhmmss_CAMTRAP-ID_OLD-FILE-NAME in the OUTPUT_DIRECTORY



exiftool -DateTimeOriginal="2025-01-21 15:34:00" -overwrite_original 06080002.mp4

For moon phase see:
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
import subprocess
import sys
from pathlib import Path

import cv2
import pytesseract

__version__ = "0.0.2"

EXTENSIONS = {".avi", ".mp4", ".jpg", ".jpeg"}


def banner_text_from_frame(
    frame, roi_height_fraction: float = 0.15, debug=False, file_path=""
):
    """
    extract text from frame banner
    """

    extracted_text = None
    # Get frame dimensions
    frame_height, frame_width, _ = frame.shape

    if debug:
        print(f"image original dimention: {frame_height}x{frame_width}")

    if frame_width > 2592:
        aspect_ratio = frame_height / frame_width
        new_width = 1280
        new_height = int(new_width * aspect_ratio)
        frame = cv2.resize(frame, (new_width, new_height))
        frame_height, frame_width, _ = frame.shape

        if debug:
            print(f"image resized dimention: {frame_height}x{frame_width}")

    # Define the region of interest (ROI)
    roi_height = int(frame_height * roi_height_fraction)
    roi = frame[frame_height - roi_height : frame_height, 0:frame_width]

    # Save the first sampled frame for inspection

    if file_path:
        cv2.imwrite(Path(file_path).with_suffix(".jpeg"), roi)

    # Convert ROI to grayscale
    roi_gray = cv2.cvtColor(roi, cv2.COLOR_BGR2GRAY)

    # Apply OCR to extract text
    try:
        extracted_text = pytesseract.image_to_string(roi_gray, config="--psm 6")
    except Exception:
        print("Tesseract error")
        sys.exit()

    if debug:
        print(f"{extracted_text=}")

    return extracted_text


def extract_banner_text_from_video(
    video_path, frame_interval=30, roi_height_fraction=0.15, debug=False
):
    """
    Extracts text from the bottom banner of a video.

    Args:
        video_path (str): Path to the video file.
        output_frame_path (str): Path to save a sample frame for inspection.
        frame_interval (int): Interval at which frames are sampled.
        roi_height_fraction (float): Fraction of the frame height that contains the banner.

    Returns:
        str: Extracted date and time from the video banner.
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
            extracted_text = banner_text_from_frame(frame, roi_height_fraction, debug)
            break

        frame_count += 1

    # Release video capture
    video_capture.release()

    return extracted_text


def extract_banner_text_from_image(image_path, roi_height_fraction=0.15, debug=False):
    """
    extract text contained in the bottom banner of an image
    """
    frame = cv2.imread(image_path)
    if frame is None:
        return "Error: Unable to load the image. Check the file path."

    extracted_text = banner_text_from_frame(
        frame, roi_height_fraction, debug=debug, file_path=image_path
    )

    return extracted_text


def extract_date_time(path_file, debug=False):
    """
    extract info from the picture/video banner
    """

    banner_text = None

    if Path(path_file).suffix.lower() in (".avi", ".mp4"):
        banner_text = extract_banner_text_from_video(path_file, debug=debug)

    if Path(path_file).suffix.lower() in (".jpg", ".jpeg"):
        banner_text = extract_banner_text_from_image(path_file, debug=debug)

    if banner_text is None:
        return {"error": ""}

    patterns = [r"\d{2}-\d{2}-\d{4}", r"\d{2}/\d{2}/\d{4}"]

    for text in banner_text.split("\n"):
        # The input string
        # text = "@ FOSA_01 73F 23C @ 06-09-2023 13:41:51"
        # text = "oo) @M-5°C / 23°F. 17/02/2025 00:09:12 = S.F. Attimis\n"

        flag_info = False
        flag_date_found = False

        # Try to extract date
        date = None
        for pattern in patterns:
            date_match = re.search(pattern, text)
            if date_match:
                raw_date = date_match.group(0)
                if debug:
                    print(f"extracted date: {raw_date}")
                match pattern:
                    case r"\d{2}-\d{2}-\d{4}":
                        raw_date_splitted = raw_date.split("-")
                        date = f"{raw_date_splitted[2]}-{raw_date_splitted[0]}-{raw_date_splitted[1]}"
                    case r"\d{2}/\d{2}/\d{4}":  # ITA
                        raw_date_splitted = raw_date.split("/")
                        date = f"{raw_date_splitted[2]}-{raw_date_splitted[1]}-{raw_date_splitted[0]}"
                flag_info = True
                if debug:
                    print(f"ISO date: {date}")

        if date is None:
            continue

        # Extract time (HH:MM:SS)
        time_match = re.search(r"\d{2}:\d{2}:\d{2}", text)
        if time_match:
            raw_time = time_match.group(0)
            if debug:
                print(f"extracted time: {raw_time}")
            hhmmss = raw_time.replace(":", "")
            if debug:
                print(f"HHMMSS: {hhmmss}")
            flag_info = True
        else:
            continue

        # Extract temperature in Fahrenheit (e.g., 73F)
        temperature_f = None
        temp_f_match = re.search(r" \d+F ", text)
        if temp_f_match:
            raw_temperature_f = temp_f_match.group(0)
            temperature_f = raw_temperature_f.strip()
            flag_info = True

        # Extract temperature in Celsius (e.g., 23C)
        temperature_c = None
        temp_c_match = re.search(r" \d+C ", text)
        if temp_c_match:
            raw_temperature_c = temp_c_match.group(0)
            temperature_c = raw_temperature_c.strip()
            flag_info = True

        if flag_info:
            # check for camera ID
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

            if debug:
                print(f"{text=}")
                print(f"{text2=}")

            cam_id = None
            try:
                cam_id = sorted(text2.split(" "), key=len, reverse=True)[0]
            except Exception:
                pass

            if debug:
                print(f"{cam_id=}")

            return {
                "text": text,
                "cam_id": cam_id,
                "date": date,
                "time": hhmmss,
                "temperature_c": temperature_c,
                "temperature_f": temperature_f,
            }
        else:
            return {"error": ""}

    return {"error": ""}


def get_new_file_path(args, file_path: Path, data: dict) -> Path:
    """
    returns new file path
    """
    if args.output_directory:
        dir = Path(args.output_directory)
    else:
        dir = Path(file_path).parent
    new_file_path = (
        dir
        / f"{data['date']}_{data['time']}_{data['cam_id']}{'_' if data['cam_id'] else ''}{file_path.name}"
    )
    return new_file_path


def parse_arguments():
    """
    parse command line arguments
    """

    parser = argparse.ArgumentParser(
        description="Extract and rename picture and video files with date/time extracted from banner"
    )

    parser.add_argument(
        "-d",
        "--directory",
        action="store",
        dest="input_directory",
        default="",
        help="Directory with media files",
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
        help="Pattern for file selection",
    )
    parser.add_argument(
        "--cam-id", action="store", dest="cam_id", default="NO", help="CAM_ID default"
    )
    parser.add_argument(
        "--rename", action="store_true", dest="rename", default="", help="Rename files"
    )
    parser.add_argument(
        "--tesseract",
        action="store",
        dest="tesseract_cmd",
        default="tesseract",
        help="Path for tesseract executable",
    )
    parser.add_argument(
        "--ffmpeg",
        action="store",
        dest="ffmpeg_path",
        default="ffmpeg",
        help="Path for the ffmpeg executable",
    )

    parser.add_argument(
        "--exiftool",
        action="store",
        dest="exiftool_path",
        default="exiftool",
        help="Path for the exiftool executable",
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
        "-v", "--version", action="store_true", dest="version", help="Display version"
    )

    # Parse the command-line arguments
    return parser.parse_args()


def main():
    args = parse_arguments()

    print(f"{args.cam_id=}")

    if args.version:
        print(f"camtrap_banner_decoder v. {__version__}\n")
        sys.exit()

    if args.tesseract_cmd:
        if args.tesseract_cmd == "tesseract" or Path(args.tesseract_cmd).is_file():
            pytesseract.pytesseract.tesseract_cmd = args.tesseract_cmd
            if args.tesseract_cmd != "tesseract":
                print(f"Using {args.tesseract_cmd}")
        else:
            print(f"The tesseract path {args.tesseract_cmd} was not found")
            sys.exit()

    if Path(args.input_directory).is_dir():
        input_dir = args.input_directory
    else:
        print(f"Directory {args.input_directory} not found")
        sys.exit()

    if args.debug:
        print(f"{input_dir=}")

    if args.output_directory and not Path(args.output_directory).is_dir():
        print(f"Output directory {args.output_directory} not found")
        sys.exit()

    files = sorted(list(Path(input_dir).glob(args.pattern)))

    for file_path in files:
        if args.debug:
            print(f"{file_path=}")

        data = extract_date_time(str(file_path), debug=args.debug)
        if "error" in data:
            print(f"Date and time not found in {file_path}")
            print("-" * 30)
            continue

        if args.debug:
            print(f"{data['temperature_c']=}   {data['temperature_f']=}")

        if data["date"] and data["time"]:
            if args.cam_id == "NO":  # , "EXTRACT"):
                data["cam_id"] = ""
            elif args.cam_id != "EXTRACT":
                data["cam_id"] = args.cam_id
            else:
                if data["cam_id"] is None:
                    data["cam_id"] = "CAM-ID"

            new_file_path = get_new_file_path(args, file_path, data)

            # check if file already renamed
            if str(Path(file_path).name).count("-") == 2:
                print(f"{Path(file_path).name} already renammed")
            else:
                if args.reencode:
                    if file_path.with_suffix(".mp4").is_file():
                        print(
                            f"Error re-encoding: {file_path.with_suffix('.mp4')} already exists"
                        )
                    else:
                        command = f'{args.ffmpeg_path} -i "{file_path}" "{file_path.with_suffix(".mp4")}"'
                        print(
                            f"re-encoding {file_path.name} to {file_path.with_suffix('.mp4').name}"
                        )
                        p = subprocess.Popen(
                            command,
                            stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE,
                            shell=True,
                        )
                        out, error = p.communicate()

                        file_path = file_path.with_suffix(".mp4")

                        new_file_path = get_new_file_path(args, file_path, data)

                if args.rename:
                    if new_file_path.is_file():
                        print(f"{Path(new_file_path).name} already exists")
                    else:
                        file_path.rename(new_file_path)
                        print(
                            f"{Path(file_path).name} renamed to {Path(new_file_path).name}"
                        )
                        # save into metadata
                        time_exiftool = f"{data['time'][0:2]}:{data['time'][2:4]}:{data['time'][4:6]}"
                        command = (
                            f"{args.exiftool_path} "
                            f'-DateTimeOriginal="{data["date"]} {time_exiftool}" '
                            f'-CreateDate="{data["date"]} {time_exiftool}" '
                            f'-ModifyDate="{data["date"]} {time_exiftool}" '
                            f'-MediaCreateDate="{data["date"]} {time_exiftool}" '
                            f'-MediaModifyDate="{data["date"]} {time_exiftool}" '
                            f'-TrackCreateDate="{data["date"]} {time_exiftool}" '
                            f'-TrackModifyDate="{data["date"]} {time_exiftool}" '
                            f"-overwrite_original {new_file_path}"
                        )

                        p = subprocess.Popen(
                            command,
                            stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE,
                            shell=True,
                        )
                        out, error = p.communicate()
                else:
                    if new_file_path.is_file():
                        print(f"{Path(new_file_path).name} already exists")
                    else:
                        print(
                            f"rename {Path(file_path).name} to {Path(new_file_path).name}"
                        )
        print("-" * 30)


if __name__ == "__main__":
    main()
