"""
Azure Kinect 多设备录制脚本。

运行方式：
    python ./multi_record.py

启动后的使用方法：
    - 按 `r` 开始录制。
    - 按 `s` 停止录制，并立即退出程序。
    - 按 `q` 直接退出程序，不完成本次录制。
    - 每次运行只允许录制一次，如需再次录制请重新启动脚本。

输出目录结构：
    recordings/
        YYYYMMDD_HHMMSS/
            YYYYMMDD_HHMMSS_<device_serial>.mkv
            YYYYMMDD_HHMMSS_<device_serial>_system_timestamps.csv

其中每个 `*_system_timestamps.csv` 都会为每一帧保存一条系统时间戳：
    frame_index,system_timestamp_sec,system_timestamp_ns

字段说明：
    - `system_timestamp_sec`：秒级时间戳，保留 4 位小数，便于查看和对齐。
    - `system_timestamp_ns`：原始纳秒级时间戳，保留完整高精度。
"""

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
import time

import cv2
import hydra
import numpy as np
import pyk4a
from omegaconf import DictConfig
from pyk4a import Config, PyK4A, connected_device_count


COLOR_RESOLUTION_MAP = {
    "720p": pyk4a.ColorResolution.RES_720P,
    "1080p": pyk4a.ColorResolution.RES_1080P,
    "1440p": pyk4a.ColorResolution.RES_1440P,
    "1536p": pyk4a.ColorResolution.RES_1536P,
    "2160p": pyk4a.ColorResolution.RES_2160P,
    "3072p": pyk4a.ColorResolution.RES_3072P,
}

FPS_MAP = {
    5: pyk4a.FPS.FPS_5,
    15: pyk4a.FPS.FPS_15,
    30: pyk4a.FPS.FPS_30,
}


@dataclass
class DeviceSession:
    device_id: int
    device: PyK4A
    config: Config
    record: pyk4a.PyK4ARecord
    window_name: str
    frame_timestamps_ns: list[int]


def resolve_color_resolution(name: str):
    key = str(name).lower()
    if key not in COLOR_RESOLUTION_MAP:
        supported = ", ".join(COLOR_RESOLUTION_MAP.keys())
        raise ValueError(f"Unsupported resolution: {name}. Supported values: {supported}")
    return COLOR_RESOLUTION_MAP[key]


def resolve_fps(value: int):
    fps = int(value)
    if fps not in FPS_MAP:
        supported = ", ".join(str(item) for item in FPS_MAP)
        raise ValueError(f"Unsupported fps: {value}. Supported values: {supported}")
    return FPS_MAP[fps]


def build_device_config(cfg: DictConfig, device_id: int, master_id: int, rank: int) -> Config:
    record_cfg = cfg.record
    return Config(
        color_resolution=resolve_color_resolution(record_cfg.resolution),
        color_format=pyk4a.ImageFormat.COLOR_MJPG,
        depth_mode=pyk4a.DepthMode.WFOV_2X2BINNED,
        camera_fps=resolve_fps(record_cfg.fps),
        wired_sync_mode=(
            pyk4a.WiredSyncMode.MASTER
            if device_id == master_id
            else pyk4a.WiredSyncMode.SUBORDINATE
        ),
        subordinate_delay_off_master_usec=(
            0 if device_id == master_id else int(record_cfg.subordinate_delay_usec) * rank
        ),
        synchronized_images_only=bool(record_cfg.synchronized_images_only),
    )


def decode_color_image(capture) -> np.ndarray | None:
    if capture.color is None or not np.any(capture.color):
        return None

    color_image = capture.color
    if len(color_image.shape) == 1:
        return cv2.imdecode(color_image, cv2.IMREAD_COLOR)

    if len(color_image.shape) == 3 and color_image.shape[2] == 4:
        return color_image[:, :, :3]

    return color_image


def flush_frame_timestamps(save_dir: Path, session: DeviceSession, run_name: str) -> None:
    if not session.frame_timestamps_ns:
        return

    timestamp_path = save_dir / f"{run_name}_{session.device.serial}_system_timestamps.csv"
    with timestamp_path.open("w", encoding="utf-8") as file:
        file.write("frame_index,system_timestamp_sec,system_timestamp_ns\n")
        for frame_index, timestamp_ns in enumerate(session.frame_timestamps_ns):
            timestamp_sec = timestamp_ns / 1_000_000_000
            file.write(f"{frame_index},{timestamp_sec:.4f},{timestamp_ns}\n")


def find_master_device(master_serial: str, device_count: int) -> int:
    print(f"Detected device count: {device_count}")
    for device_id in range(device_count):
        device = PyK4A(device_id=device_id)
        device.start()
        try:
            if device.serial == master_serial:
                print(f"Master device ID: {device_id}, Serial: {device.serial}")
                return device_id
        finally:
            device.stop()

    raise RuntimeError(f"Master device with serial {master_serial} was not found.")


def initialize_sessions(cfg: DictConfig, master_id: int, device_count: int, save_dir: Path, run_name: str):
    sessions: list[DeviceSession] = []
    device_ids = [device_id for device_id in range(device_count) if device_id != master_id] + [master_id]
    current_rank = int(cfg.record.start_rank)

    for device_id in device_ids:
        print(f"Initializing device ID: {device_id}")
        device_config = build_device_config(cfg, device_id, master_id, current_rank)
        if device_id != master_id:
            current_rank += 1

        device = PyK4A(config=device_config, device_id=device_id)
        device.start()
        device.whitebalance_mode_auto = True

        record = pyk4a.PyK4ARecord(
            path=str(save_dir / f"{run_name}_{device.serial}.mkv"),
            config=device_config,
            device=device,
        )
        record.create()

        sessions.append(
            DeviceSession(
                device_id=device_id,
                device=device,
                config=device_config,
                record=record,
                window_name=f"Kinect_{device_id}",
                frame_timestamps_ns=[],
            )
        )

    return sessions


def cleanup_sessions(sessions: list[DeviceSession]) -> None:
    for session in sessions:
        try:
            session.record.flush()
            session.record.close()
        except Exception as exc:  # noqa: BLE001
            print(f"Failed to close record for device {session.device_id}: {exc}")

        try:
            session.device.stop()
        except Exception as exc:  # noqa: BLE001
            print(f"Failed to stop device {session.device_id}: {exc}")

    cv2.destroyAllWindows()


def preview_capture(session: DeviceSession, capture, preview_width: int, preview_height: int) -> None:
    color_image = decode_color_image(capture)
    if color_image is None:
        return

    preview = cv2.resize(color_image, (preview_width, preview_height))
    cv2.imshow(session.window_name, preview)


def run_recording_loop(cfg: DictConfig, sessions: list[DeviceSession], save_dir: Path, run_name: str) -> None:
    recording = False
    has_recorded = False
    preview_width = int(cfg.preview.width)
    preview_height = int(cfg.preview.height)

    print("Press 'r' to start recording once, 's' to stop and exit, and 'q' to quit.")

    while True:
        for session in sessions:
            capture = session.device.get_capture()

            if recording:
                session.frame_timestamps_ns.append(time.time_ns())
                session.record.write_capture(capture)
            else:
                preview_capture(session, capture, preview_width, preview_height)

        key = cv2.waitKey(1) & 0xFF
        if key == ord("r"):
            if has_recorded:
                print("Recording is limited to one session. Restart the program to record again.")
                continue
            recording = True
            has_recorded = True
            print("Recording started.")
        elif key == ord("s"):
            if recording:
                print("Recording stopped. Exiting.")
                return
        elif key == ord("q"):
            print("Exiting.")
            return


@hydra.main(version_base=None, config_path="config", config_name="config")
def main(cfg: DictConfig) -> None:
    device_count = connected_device_count()
    if device_count == 0:
        raise RuntimeError("No Azure Kinect devices detected.")

    output_root = Path(hydra.utils.get_original_cwd()) / str(cfg.record.output_dir)
    run_name = datetime.now().strftime("%Y%m%d_%H%M%S")
    save_dir = output_root / run_name
    save_dir.mkdir(parents=True, exist_ok=True)

    master_id = find_master_device(str(cfg.record.master_serial), device_count)
    sessions = initialize_sessions(cfg, master_id, device_count, save_dir, run_name)

    try:
        run_recording_loop(cfg, sessions, save_dir, run_name)
    except KeyboardInterrupt:
        print("CTRL-C pressed. Exiting.")
    finally:
        for session in sessions:
            flush_frame_timestamps(save_dir, session, run_name)
        cleanup_sessions(sessions)


if __name__ == "__main__":
    main()
