from __future__ import annotations

import struct
from pathlib import Path

import pytest


@pytest.fixture
def simple_gpx(tmp_path: Path) -> Path:
    path = tmp_path / "route.gpx"
    path.write_text(
        """<?xml version="1.0" encoding="UTF-8"?>
<gpx version="1.1" creator="tests" xmlns="http://www.topografix.com/GPX/1/1">
  <trk><name>Test</name><trkseg>
    <trkpt lat="37.0000" lon="-122.0000"><ele>100</ele></trkpt>
    <trkpt lat="37.0010" lon="-121.9990"><ele>110</ele></trkpt>
    <trkpt lat="37.0020" lon="-121.9980"><ele>120</ele></trkpt>
  </trkseg></trk>
</gpx>
""",
        encoding="utf-8",
    )
    return path


_FIT_CRC_TABLE = (
    0x0000,
    0xCC01,
    0xD801,
    0x1400,
    0xF001,
    0x3C00,
    0x2800,
    0xE401,
    0xA001,
    0x6C00,
    0x7800,
    0xB401,
    0x5000,
    0x9C01,
    0x8801,
    0x4400,
)


def _fit_crc(data: bytes, value: int = 0) -> int:
    for byte in data:
        temporary = _FIT_CRC_TABLE[value & 0xF]
        value = (value >> 4) & 0x0FFF
        value ^= temporary ^ _FIT_CRC_TABLE[byte & 0xF]
        temporary = _FIT_CRC_TABLE[value & 0xF]
        value = (value >> 4) & 0x0FFF
        value ^= temporary ^ _FIT_CRC_TABLE[(byte >> 4) & 0xF]
    return value


def _semicircles(degrees: float) -> int:
    return round(degrees / 180.0 * 2**31)


_FIT_RECORD_DEFINITION = (
    bytes([0x40, 0, 0])
    + struct.pack("<H", 20)
    + bytes(
        [
            3,
            0,
            4,
            0x85,
            1,
            4,
            0x85,
            2,
            2,
            0x84,
        ]
    )
)
_FIT_EVENT_DEFINITION = (
    bytes([0x41, 0, 0])
    + struct.pack("<H", 21)
    + bytes([2, 0, 1, 0x00, 1, 1, 0x00])
)


def _fit_record(latitude: float, longitude: float, elevation: float) -> bytes:
    return bytes([0]) + struct.pack(
        "<iiH",
        _semicircles(latitude),
        _semicircles(longitude),
        round((elevation + 500.0) * 5.0),
    )


def _write_fit(path: Path, messages: tuple[bytes, ...]) -> Path:
    data = b"".join((_FIT_RECORD_DEFINITION, *messages))
    header_without_crc = struct.pack(
        "<BBHI4s",
        14,
        0x20,
        2100,
        len(data),
        b".FIT",
    )
    header = header_without_crc + struct.pack("<H", _fit_crc(header_without_crc))
    contents = header + data
    contents += struct.pack("<H", _fit_crc(contents))
    path.write_bytes(contents)
    return path


@pytest.fixture
def simple_fit(tmp_path: Path) -> Path:
    missing_position = bytes([0]) + struct.pack("<iiH", 0x7FFFFFFF, 0x7FFFFFFF, 0xFFFF)
    return _write_fit(
        tmp_path / "route.fit",
        (
            _fit_record(37.0, -122.0, 100.0),
            _fit_record(37.001, -121.999, 110.0),
            missing_position,
            _fit_record(38.0, -121.0, 120.0),
            _fit_record(38.001, -120.999, 130.0),
        ),
    )


@pytest.fixture
def timer_paused_fit(tmp_path: Path) -> Path:
    timer_stop_all = bytes([1, 0, 4])
    return _write_fit(
        tmp_path / "paused.fit",
        (
            _fit_record(37.0, -122.0, 100.0),
            _fit_record(37.001, -121.999, 110.0),
            _FIT_EVENT_DEFINITION,
            timer_stop_all,
            _fit_record(38.0, -121.0, 120.0),
            _fit_record(38.001, -120.999, 130.0),
        ),
    )
