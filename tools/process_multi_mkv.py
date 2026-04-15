"""
多相机 MKV 同步裁剪与导出脚本。

运行方式：
    python ./tools/process_multi_mkv.py

预期输入目录结构：
    <run_dir>/
        <run_name>_<device_serial>.mkv
        <run_name>_<device_serial>_system_timestamps.csv

输出目录结构：
    <run_dir>/<output_subdir>/
        <device_serial>/
            color_000000.jpg
            depth_000000.png
            color_000001.jpg
            depth_000001.png
            ...
        <device_serial>_timestamps.csv

功能说明：
    1. 读取 process_mkv.yaml 中的运行目录、serial 列表和 fps。
    2. 遍历所有 MKV，提取每一帧的 device timestamp。
    3. 以“最晚开始帧”作为开始同步标记，以“最早结束帧”作为结束同步标记。
    4. 用最晚开始相机在该时间窗口内的有效帧数，作为全局有效帧数。
    5. 对每个相机，找到最接近开始同步标记的帧，向后导出相同数量的帧。
    6. 每个视角输出 color/depth 图像，以及逐帧 device/system timestamp。

其中 timestamps.csv 包含以下字段：
    frame_index, source_frame_index, device_timestamp_usec, system_timestamp_sec, system_timestamp_ns
"""

from __future__ import annotations

import csv
from bisect import bisect_left
from dataclasses import dataclass
from pathlib import Path

import cv2
import hydra
import pyk4a
from omegaconf import DictConfig
from pyk4a import PyK4APlayback


@dataclass
class FrameInfo:
    frame_index: int
    device_timestamp_usec: int


@dataclass
class CameraInfo:
    serial: str
    mkv_path: Path
    system_timestamp_path: Path
    frame_infos: list[FrameInfo]

    @property
    def first_timestamp_usec(self) -> int:
        return self.frame_infos[0].device_timestamp_usec

    @property
    def last_timestamp_usec(self) -> int:
        return self.frame_infos[-1].device_timestamp_usec


def decode_color_image(color_data):
    if color_data is None:
        return None
    if len(color_data.shape) == 1:
        return cv2.imdecode(color_data, cv2.IMREAD_COLOR)
    if len(color_data.shape) == 3 and color_data.shape[2] == 4:
        return color_data[:, :, :3]
    return color_data


def is_end_of_playback_exception(exc: Exception) -> bool:
    message = str(exc).lower()
    return isinstance(exc, EOFError) or "eof" in message or "end of file" in message


def collect_frame_infos(mkv_path: Path) -> list[FrameInfo]:
    playback = PyK4APlayback(path=str(mkv_path))
    playback.open()

    frame_infos: list[FrameInfo] = []
    frame_index = 0
    try:
        while True:
            capture = playback.get_next_capture()
            timestamp_usec = int(capture.color_timestamp_usec)
            if timestamp_usec <= 0:
                continue
            frame_infos.append(FrameInfo(frame_index=frame_index, device_timestamp_usec=timestamp_usec))
            frame_index += 1
    except Exception as exc:  # noqa: BLE001
        if not is_end_of_playback_exception(exc):
            raise
    finally:
        playback.close()

    if not frame_infos:
        raise RuntimeError(f"No valid frames found in {mkv_path}")

    return frame_infos


def load_system_timestamps(timestamp_csv_path: Path) -> list[dict[str, str]]:
    if not timestamp_csv_path.exists():
        raise FileNotFoundError(f"System timestamp file not found: {timestamp_csv_path}")

    with timestamp_csv_path.open("r", encoding="utf-8", newline="") as file:
        rows = list(csv.DictReader(file))

    if not rows:
        raise RuntimeError(f"No system timestamp rows found in {timestamp_csv_path}")

    return rows


def build_camera_info(run_dir: Path, serial: str) -> CameraInfo:
    mkv_path = run_dir / f"{run_dir.name}_{serial}.mkv"
    if not mkv_path.exists():
        raise FileNotFoundError(f"MKV file not found: {mkv_path}")

    system_timestamp_path = run_dir / f"{run_dir.name}_{serial}_system_timestamps.csv"
    frame_infos = collect_frame_infos(mkv_path)

    return CameraInfo(
        serial=serial,
        mkv_path=mkv_path,
        system_timestamp_path=system_timestamp_path,
        frame_infos=frame_infos,
    )


def find_nearest_frame_index(frame_infos: list[FrameInfo], target_timestamp_usec: int) -> int:
    timestamps = [frame.device_timestamp_usec for frame in frame_infos]
    insert_pos = bisect_left(timestamps, target_timestamp_usec)

    if insert_pos == 0:
        return 0
    if insert_pos >= len(frame_infos):
        return len(frame_infos) - 1

    before = frame_infos[insert_pos - 1]
    after = frame_infos[insert_pos]
    if abs(before.device_timestamp_usec - target_timestamp_usec) <= abs(after.device_timestamp_usec - target_timestamp_usec):
        return insert_pos - 1
    return insert_pos


def count_valid_frames(frame_infos: list[FrameInfo], start_flag_usec: int, end_flag_usec: int) -> int:
    return sum(
        1
        for frame in frame_infos
        if start_flag_usec <= frame.device_timestamp_usec <= end_flag_usec
    )


def save_timestamp_rows(
    output_root: Path,
    serial: str,
    selected_frames: list[FrameInfo],
    system_rows: list[dict[str, str]],
) -> None:
    timestamp_path = output_root / f"{serial}_timestamps.csv"
    with timestamp_path.open("w", encoding="utf-8", newline="") as file:
        writer = csv.writer(file)
        writer.writerow(
            [
                "frame_index",
                "source_frame_index",
                "device_timestamp_usec",
                "system_timestamp_sec",
                "system_timestamp_ns",
            ]
        )

        for export_index, frame in enumerate(selected_frames):
            if frame.frame_index >= len(system_rows):
                raise RuntimeError(
                    f"System timestamp row missing for source frame {frame.frame_index}"
                )
            system_row = system_rows[frame.frame_index]
            writer.writerow(
                [
                    export_index,
                    frame.frame_index,
                    frame.device_timestamp_usec,
                    system_row.get("system_timestamp_sec", ""),
                    system_row.get("system_timestamp_ns", ""),
                ]
            )


def export_camera_frames(
    camera_info: CameraInfo,
    selected_frames: list[FrameInfo],
    system_rows: list[dict[str, str]],
    output_root: Path,
    output_dir: Path,
) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    save_timestamp_rows(output_root, camera_info.serial, selected_frames, system_rows)

    selected_by_source_index = {frame.frame_index: export_idx for export_idx, frame in enumerate(selected_frames)}

    playback = PyK4APlayback(path=str(camera_info.mkv_path))
    playback.open()

    source_frame_index = 0
    saved_count = 0
    expected_count = len(selected_frames)

    try:
        while saved_count < expected_count:
            capture = playback.get_next_capture()
            timestamp_usec = int(capture.color_timestamp_usec)
            if timestamp_usec <= 0:
                continue

            export_index = selected_by_source_index.get(source_frame_index)
            if export_index is None:
                source_frame_index += 1
                continue

            color_image = decode_color_image(capture.color)
            depth_image = capture.transformed_depth
            if color_image is None or depth_image is None:
                raise RuntimeError(
                    f"Missing color or depth image for serial {camera_info.serial}, frame {source_frame_index}"
                )

            color_path = output_dir / f"color_{export_index:06d}.jpg"
            depth_path = output_dir / f"depth_{export_index:06d}.png"

            if not cv2.imwrite(str(color_path), color_image):
                raise RuntimeError(f"Failed to write {color_path}")
            if not cv2.imwrite(str(depth_path), depth_image):
                raise RuntimeError(f"Failed to write {depth_path}")

            saved_count += 1
            source_frame_index += 1
    except Exception as exc:  # noqa: BLE001
        if not is_end_of_playback_exception(exc):
            raise
        raise RuntimeError(
            f"Unexpected EOF while exporting frames for serial {camera_info.serial}"
        ) from exc
    finally:
        playback.close()


def summarize_camera(camera_info: CameraInfo, fps: int) -> None:
    duration_sec = (camera_info.last_timestamp_usec - camera_info.first_timestamp_usec) / 1_000_000
    actual_fps = len(camera_info.frame_infos) / duration_sec if duration_sec > 0 else 0.0
    print(f"Serial: {camera_info.serial}")
    print(f"  MKV: {camera_info.mkv_path}")
    print(f"  Total frames: {len(camera_info.frame_infos)}")
    print(f"  Start device timestamp: {camera_info.first_timestamp_usec}")
    print(f"  End device timestamp: {camera_info.last_timestamp_usec}")
    print(f"  Duration (sec): {duration_sec:.4f}")
    print(f"  Actual FPS: {actual_fps:.4f}")

    if abs(actual_fps - fps) > 0.5:
        print(
            f"  Warning: actual FPS {actual_fps:.4f} differs from configured FPS {fps}. "
            "The video may be corrupted or have dropped/invalid frames."
        )


@hydra.main(version_base=None, config_path="../config", config_name="process_mkv")
def main(cfg: DictConfig) -> None:
    run_dir_value = str(cfg.run_dir).strip()
    if not run_dir_value or run_dir_value == "CHANGE_ME":
        raise ValueError("Please set run_dir in config/process_mkv.yaml")

    serials = [str(serial) for serial in cfg.all_serials]
    fps = int(cfg.fps)

    project_root = Path(hydra.utils.get_original_cwd())
    run_dir = Path(run_dir_value)
    if not run_dir.is_absolute():
        run_dir = project_root / run_dir
    run_dir = run_dir.resolve()
    if not run_dir.exists():
        raise FileNotFoundError(f"Recording directory not found: {run_dir}")

    output_subdir = str(cfg.output_subdir).strip()
    if not output_subdir or output_subdir == "CHANGE_ME":
        raise ValueError("Please set output_subdir in config/process_mkv.yaml")

    export_root = run_dir / output_subdir
    export_root.mkdir(parents=True, exist_ok=True)

    cameras: list[CameraInfo] = []
    for serial in serials:
        camera_info = build_camera_info(run_dir, serial)
        summarize_camera(camera_info, fps)
        cameras.append(camera_info)

    latest_start_camera = max(cameras, key=lambda camera: camera.first_timestamp_usec)
    earliest_end_camera = min(cameras, key=lambda camera: camera.last_timestamp_usec)
    start_flag_usec = latest_start_camera.first_timestamp_usec
    end_flag_usec = earliest_end_camera.last_timestamp_usec

    if start_flag_usec >= end_flag_usec:
        raise RuntimeError(
            f"Invalid sync window: start_flag={start_flag_usec}, end_flag={end_flag_usec}"
        )

    valid_frames_num = count_valid_frames(
        latest_start_camera.frame_infos,
        start_flag_usec,
        end_flag_usec,
    )
    if valid_frames_num <= 0:
        raise RuntimeError("No valid frames found in the global sync window.")

    print(f"Start flag serial: {latest_start_camera.serial}, timestamp: {start_flag_usec}")
    print(f"End flag serial: {earliest_end_camera.serial}, timestamp: {end_flag_usec}")
    print(f"Global valid frames: {valid_frames_num}")

    for camera_info in cameras:
        start_idx = find_nearest_frame_index(camera_info.frame_infos, start_flag_usec)
        end_idx = start_idx + valid_frames_num
        if end_idx > len(camera_info.frame_infos):
            raise RuntimeError(
                f"Serial {camera_info.serial} does not have enough frames after sync start. "
                f"start_idx={start_idx}, valid_frames={valid_frames_num}, total={len(camera_info.frame_infos)}"
            )

        selected_frames = camera_info.frame_infos[start_idx:end_idx]
        system_rows = load_system_timestamps(camera_info.system_timestamp_path)
        output_dir = export_root / camera_info.serial

        print(
            f"Exporting serial {camera_info.serial}: start source frame {start_idx}, "
            f"frames {len(selected_frames)} -> {output_dir}"
        )
        export_camera_frames(camera_info, selected_frames, system_rows, export_root, output_dir)

    print(f"Done. Exported synchronized frames to: {export_root}")


if __name__ == "__main__":
    main()
