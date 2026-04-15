"""
Sound of Body — biometric sonification installation
====================================================
Reads 30 seconds of heart-rate / blood-pressure data from a wrist-worn
sensor over USB serial, synthesises a personalised music track, and
renders a final video that plays automatically.

Signal → sound mapping
──────────────────────
  Heart rate  (67–86 BPM)   →  pitch    (harmonic tone, note A2–G4)
  Diastolic                 →  tempo    (beat interval + playback speed)
  Systolic                  →  timbre   (gasp / sigh / heartbeat drum)

Note on sensor units
─────────────────────
The MKB0805 module outputs scaled integer values, not clinical mmHg readings.
  • systolic  : small categorical integer (typical range 0–5)
  • diastolic : small integer (typical range 0–60)
These are used as relative indices to select audio assets; do not interpret
them as standard blood-pressure measurements.

Platform: Windows (COM port naming).  Python 3.11 required.
"""

import os
import re
import sys
import time
import random
from datetime import datetime
from math import ceil

import ffmpy
import imageio_ffmpeg
import serial
from dotenv import load_dotenv
from pydub import AudioSegment
from moviepy import AudioFileClip, VideoFileClip

# ---------------------------------------------------------------------------
# ffmpeg — point both ffmpy and pydub at the bundled binary so no separate
# system-level ffmpeg installation is required.
# ---------------------------------------------------------------------------

_FFMPEG_EXE = imageio_ffmpeg.get_ffmpeg_exe()
_FFPROBE_EXE = os.path.join(
    os.path.dirname(_FFMPEG_EXE),
    os.path.basename(_FFMPEG_EXE).replace("ffmpeg", "ffprobe"),
)

AudioSegment.converter = _FFMPEG_EXE
AudioSegment.ffmpeg    = _FFMPEG_EXE
if os.path.exists(_FFPROBE_EXE):
    AudioSegment.ffprobe = _FFPROBE_EXE

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

load_dotenv()

SERIAL_PORT    = os.getenv("SERIAL_PORT", "COM3")
BAUD_RATE      = int(os.getenv("BAUD_RATE", "115200"))
SAMPLE_COUNT   = 30    # number of sensor readings to collect
MUSIC_DURATION = 60    # seconds of audio to synthesise

# Resolve music assets directory whether running from source or frozen exe.
if getattr(sys, "frozen", False):
    _BASE = os.path.dirname(os.path.realpath(sys.executable))
else:
    _BASE = os.path.dirname(os.path.abspath(__file__))

MUSIC_DIR = os.path.join(_BASE, "music")


# ---------------------------------------------------------------------------
# Step 1 — Sensor data collection
# ---------------------------------------------------------------------------

def measure_data() -> str:
    """
    Collect ``SAMPLE_COUNT`` biometric readings from the serial sensor.

    The microcontroller streams one line per reading in the format:
        ``HeartRate: <int> Systolic: <int>, Diastolic: <int>, H``

    Readings are saved to ``music/data/<timestamp>.txt``.

    Returns:
        Absolute path to the saved data file.
    """
    print("Measuring heart rate and blood pressure, please wait...")

    port = serial.Serial(
        port=SERIAL_PORT,
        baudrate=BAUD_RATE,
        bytesize=8,
        parity=serial.PARITY_NONE,
        stopbits=1,
        timeout=0.005,
    )

    samples: list[str] = []
    try:
        while len(samples) < SAMPLE_COUNT:
            if port.in_waiting:
                time.sleep(0.05)
                raw = port.readline()
                if not raw:
                    time.sleep(0.05)
                    raw = port.readline()
                # Skip the 0xFF sentinel byte (not valid UTF-8).
                if raw == b"\xff":
                    continue
                line = raw.decode("utf-8", errors="ignore").strip()
                digits = "".join(filter(str.isdigit, line.split(":")[-1]))
                if digits and digits != "255":
                    samples.append(line)
            else:
                # Avoid busy-waiting when the buffer is empty.
                time.sleep(0.05)
    finally:
        port.close()

    data_dir = os.path.normpath(os.path.join(MUSIC_DIR, "data"))
    os.makedirs(data_dir, exist_ok=True)

    timestamp   = time.strftime("%Y-%m-%d %H_%M_%S", time.localtime())
    output_file = os.path.join(data_dir, timestamp + ".txt")
    with open(output_file, "w", encoding="utf-8") as f:
        f.write("\n".join(samples))

    print("Data collection complete.")
    return output_file


# ---------------------------------------------------------------------------
# Step 2 — Parse sensor file
# ---------------------------------------------------------------------------

def read_data(data_file: str) -> tuple[int, int, int]:
    """
    Parse a sensor data file and return averaged biometric values.

    Args:
        data_file: Path produced by :func:`measure_data`.

    Returns:
        ``(heart_rate, systolic, diastolic)`` as integers.

    Raises:
        ValueError: If no valid biometric readings are found in the file.
    """
    with open(data_file, encoding="utf-8") as f:
        lines = f.readlines()

    heart_rates, systolics, diastolics = [], [], []
    for line in lines:
        hr  = re.search(r"HeartRate: (.*?) ",   line)
        sys = re.search(r"Systolic: (.*?), D",  line)
        dia = re.search(r"Diastolic: (.*?), H", line)
        if hr and sys and dia:
            heart_rates.append(int(hr.group(1)))
            systolics.append(int(sys.group(1)))
            diastolics.append(int(dia.group(1)))

    if not heart_rates:
        raise ValueError(
            f"No valid biometric readings found in '{data_file}'. "
            "Expected lines containing HeartRate, Systolic, and Diastolic values."
        )

    n = len(heart_rates)
    return sum(heart_rates) // n, sum(systolics) // n, sum(diastolics) // n


# ---------------------------------------------------------------------------
# Step 3 — Tempo adjustment
# ---------------------------------------------------------------------------

def change_velocity(file_paths: list[str], diastolic: int) -> list[str]:
    """
    Re-encode harmonic WAV files at a diastolic-dependent playback speed
    using ffmpy's ``atempo`` filter.

    Diastolic → speed mapping:
        < 24  →  0.5×   (calm / slow)
        < 34  →  1.0×   (normal)
        < 44  →  1.5×   (brisk)
        ≥ 44  →  2.0×   (intense)

    Args:
        file_paths: Paths to the harmonic WAV files to re-encode.
        diastolic:  Averaged diastolic reading.

    Returns:
        Paths to the tempo-adjusted output WAV files.
    """
    if diastolic < 24:
        speed = 0.5
    elif diastolic < 34:
        speed = 1.0
    elif diastolic < 44:
        speed = 1.5
    else:
        speed = 2.0

    timestamp = datetime.now().strftime("%Y_%m_%d_%H_%M_%S")
    out_dir   = os.path.normpath(os.path.join(MUSIC_DIR, "tharm_" + timestamp))
    os.makedirs(out_dir, exist_ok=True)

    output_files: list[str] = []
    for path in file_paths:
        name     = os.path.basename(path)
        out_name = name.replace("tharm", "_tharm", 1)
        out_path = os.path.normpath(os.path.join(out_dir, out_name))
        ffmpy.FFmpeg(
            executable=_FFMPEG_EXE,
            inputs={path: None},
            outputs={out_path: f"-filter:a atempo={speed}"},
        ).run()
        output_files.append(out_path)

    return output_files


# ---------------------------------------------------------------------------
# Step 4 — Music synthesis
# ---------------------------------------------------------------------------

def produce_music(heart_rate: int, systolic: int, diastolic: int) -> str:
    """
    Compose a biometric music track by layering a rhythmic drum layer
    and a harmonic tone layer.

    Drumbeat selection (blood_pressure/ samples)
    ─────────────────────────────────────────────
    Timbre is determined by systolic; level suffix by diastolic:

        systolic == 2  AND  diastolic % 10 == 0   →  gasp
        systolic in {2, 3}  AND  diastolic % 10 ≠ 0  →  sigh
        otherwise                                     →  heartbeat

        diastolic < 24  →  " 24"
        diastolic < 34  →  " 34"
        diastolic < 44  →  " 44"
        diastolic ≥ 44  →  " 54"

    Harmonic selection (heart_rate/ samples)
    ─────────────────────────────────────────
    Each file is named ``tharm <NOTE> <BPM>.wav``; the file whose BPM
    matches ``heart_rate`` is selected (range 67–86). If the measured
    heart rate falls outside this range it is clamped to the nearest
    available BPM.

    Args:
        heart_rate: Averaged heart rate (67–86 BPM).
        systolic:   Averaged systolic reading (sensor-specific scaled value).
        diastolic:  Averaged diastolic reading (sensor-specific scaled value).

    Returns:
        Path to the synthesised output WAV file.
    """
    print("Composing music, please wait...")

    # ── Diastolic → level tag ─────────────────────────────────────────────
    if diastolic < 24:
        dia_tag = " 24"
    elif diastolic < 34:
        dia_tag = " 34"
    elif diastolic < 44:
        dia_tag = " 44"
    else:
        dia_tag = " 54"

    # ── Systolic → drumbeat timbre ────────────────────────────────────────
    if systolic == 2 and diastolic % 10 == 0:
        sys_tag = "gasp"
    elif systolic in (2, 3) and diastolic % 10 != 0:
        sys_tag = "sigh"
    else:
        sys_tag = "heartbeat"

    # ── Build drumbeat layer ──────────────────────────────────────────────
    bp_dir     = os.path.normpath(os.path.join(MUSIC_DIR, "blood_pressure"))
    drum_wav   = os.path.join(bp_dir, sys_tag + dia_tag + ".wav")
    per_beat   = AudioSegment.from_wav(drum_wav)
    drum_track = per_beat * ceil(MUSIC_DURATION / per_beat.duration_seconds)

    # ── Build harmonic layer ──────────────────────────────────────────────
    hr_dir = os.path.normpath(os.path.join(MUSIC_DIR, "heart_rate"))
    available = {
        int(name[-6:-4]): os.path.join(hr_dir, name)
        for name in os.listdir(hr_dir)
        if name.endswith(".wav")
    }
    if not available:
        raise FileNotFoundError(f"No harmonic WAV files found in '{hr_dir}'.")

    # Clamp heart_rate to the nearest available BPM.
    closest_bpm = min(available, key=lambda bpm: abs(bpm - heart_rate))
    candidates  = [available[closest_bpm]]

    adjusted    = change_velocity(candidates, diastolic)
    per_tharm   = AudioSegment.from_wav(adjusted[0])
    tharm_track = per_tharm * ceil(MUSIC_DURATION / per_tharm.duration_seconds)

    # ── Mix ───────────────────────────────────────────────────────────────
    mixed = drum_track.overlay(tharm_track)

    timestamp = datetime.now().strftime("%Y_%m_%d_%H_%M_%S")
    out_dir   = os.path.normpath(os.path.join(MUSIC_DIR, "output_" + timestamp))
    os.makedirs(out_dir, exist_ok=True)
    out_file  = os.path.join(out_dir, "output_" + timestamp + ".wav")
    mixed.export(out_file, format="wav")

    return out_file


# ---------------------------------------------------------------------------
# Step 5 — Video rendering
# ---------------------------------------------------------------------------

def produce_video(wav_file: str) -> None:
    """
    Combine the synthesised audio with a randomly selected background clip.

    Three clip lengths are available (25 s, 39 s, 56 s); one is chosen at
    random.  The audio is looped and trimmed to match the clip, rendered as
    an MP4, and opened automatically for playback.

    Args:
        wav_file: Path to the synthesised WAV file from :func:`produce_music`.
    """
    print("Synthesising video, please wait...")

    audio         = AudioSegment.from_wav(wav_file)
    audio_seconds = audio.duration_seconds

    clips = {
        1: ("25s.mp4", 25, 25_000),
        2: ("39s.mp4", 39, 39_000),
        3: ("56s.mp4", 56, 56_000),
    }
    clip_name, clip_sec, clip_ms = clips[random.randint(1, 3)]
    video_path = os.path.normpath(os.path.join(MUSIC_DIR, "video", clip_name))

    looped = (audio * ceil(clip_sec / audio_seconds))[:clip_ms]

    out_dir = os.path.normpath(os.path.join(MUSIC_DIR, "output"))
    os.makedirs(out_dir, exist_ok=True)

    timestamp = datetime.now().strftime("%Y_%m_%d_%H_%M_%S")
    audio_tmp = os.path.join(out_dir, "audio_" + timestamp + ".wav")
    looped.export(audio_tmp, format="wav")

    output_mp4 = os.path.join(out_dir, "output_" + timestamp + ".mp4")
    try:
        with VideoFileClip(video_path) as video_clip, \
             AudioFileClip(audio_tmp) as audio_clip:
            final = video_clip.with_audio(audio_clip)
            final.write_videofile(output_mp4, audio_codec="aac")
            final.close()
    finally:
        # Remove the intermediate WAV regardless of success or failure.
        if os.path.exists(audio_tmp):
            os.remove(audio_tmp)

    os.startfile(os.path.normpath(output_mp4))


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    heart_rate, systolic, diastolic = read_data(measure_data())
    produce_video(produce_music(heart_rate, systolic, diastolic))
