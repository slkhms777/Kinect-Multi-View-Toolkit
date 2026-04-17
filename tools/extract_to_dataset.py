"""
Extract a frame range from a processed multi-view recording into a dataset folder.

Expected input layout:
    <input_dir>/
        <serial>/
            color_000000.jpg
            depth_000000.png
            ...
        timestep_info/
            <serial>_timestamps.csv
        calibration_info/
            ...
"""

from __future__ import annotations

import csv
import shutil
from pathlib import Path

import hydra
from omegaconf import DictConfig


def resolve_path(project_root: Path, value: str, field_name: str) -> Path:
    path_value = str(value).strip()
    if not path_value or path_value == "CHANGE_ME":
        raise ValueError(f"Please set {field_name} in config/extract.yaml or override it on the command line")

    path = Path(path_value)
    if not path.is_absolute():
        path = project_root / path
    return path.resolve()


def load_serials(cfg: DictConfig) -> list[str]:
    serials = [str(serial).strip() for serial in cfg.get("all_serials", [])]
    serials = [serial for serial in serials if serial and not serial.startswith("#")]
    if not serials:
        raise ValueError("Please set all_serials in config/extract.yaml")
    return serials


def ensure_empty_output_dir(output_dir: Path) -> None:
    if output_dir.exists() and any(output_dir.iterdir()):
        raise FileExistsError(
            f"Output directory already exists and is not empty: {output_dir}. "
            "Choose a different output_name or remove the existing directory first."
        )
    output_dir.mkdir(parents=True, exist_ok=True)


def parse_frame_index(path: Path, prefix: str) -> int:
    stem = path.stem
    if not stem.startswith(prefix):
        raise ValueError(f"Unexpected frame filename: {path.name}")
    return int(stem.removeprefix(prefix))


def list_available_frames(camera_dir: Path) -> list[int]:
    color_frames = {
        parse_frame_index(path, "color_")
        for path in camera_dir.glob("color_*.jpg")
    }
    depth_frames = {
        parse_frame_index(path, "depth_")
        for path in camera_dir.glob("depth_*.png")
    }
    common_frames = sorted(color_frames & depth_frames)
    if not common_frames:
        raise RuntimeError(f"No matching color/depth frames found in {camera_dir}")
    return common_frames


def select_frames(available_frames: list[int], start_frame: int, end_frame: int) -> list[int]:
    if start_frame < 0:
        raise ValueError("start_frame must be >= 0")
    if end_frame != -1 and end_frame < start_frame:
        raise ValueError("end_frame must be -1 or >= start_frame")

    selected = [
        frame_idx
        for frame_idx in available_frames
        if frame_idx >= start_frame and (end_frame == -1 or frame_idx <= end_frame)
    ]
    if not selected:
        raise RuntimeError(f"No frames selected with start_frame={start_frame}, end_frame={end_frame}")
    return selected


def copy_camera_frames(input_dir: Path, output_dir: Path, serial: str, selected_frames: list[int]) -> None:
    source_camera_dir = input_dir / serial
    target_camera_dir = output_dir / serial
    target_camera_dir.mkdir(parents=True, exist_ok=True)

    for frame_idx in selected_frames:
        color_source = source_camera_dir / f"color_{frame_idx:06d}.jpg"
        depth_source = source_camera_dir / f"depth_{frame_idx:06d}.png"
        if not color_source.exists():
            raise FileNotFoundError(f"Missing color frame: {color_source}")
        if not depth_source.exists():
            raise FileNotFoundError(f"Missing depth frame: {depth_source}")

        shutil.copy2(color_source, target_camera_dir / color_source.name)
        shutil.copy2(depth_source, target_camera_dir / depth_source.name)


def copy_calibration_info(input_dir: Path, output_dir: Path) -> None:
    source = input_dir / "calibration_info"
    if not source.exists():
        return
    shutil.copytree(source, output_dir / "calibration_info", dirs_exist_ok=True)


def copy_filtered_timestamp_csv(input_dir: Path, output_dir: Path, serial: str, selected_frames: list[int]) -> None:
    source = input_dir / "timestep_info" / f"{serial}_timestamps.csv"
    if not source.exists():
        return

    selected_set = set(selected_frames)
    target_dir = output_dir / "timestep_info"
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / source.name

    with source.open("r", encoding="utf-8", newline="") as source_file:
        reader = csv.DictReader(source_file)
        if reader.fieldnames is None:
            raise RuntimeError(f"Timestamp CSV has no header: {source}")
        rows = [
            row
            for row in reader
            if int(row.get("frame_index", "-1")) in selected_set
        ]

    with target.open("w", encoding="utf-8", newline="") as target_file:
        writer = csv.DictWriter(target_file, fieldnames=reader.fieldnames)
        writer.writeheader()
        writer.writerows(rows)


@hydra.main(version_base=None, config_path="../config", config_name="extract")
def main(cfg: DictConfig) -> None:
    project_root = Path(hydra.utils.get_original_cwd())
    input_dir = resolve_path(project_root, str(cfg.input_dir), "input_dir")
    if not input_dir.exists():
        raise FileNotFoundError(f"Input directory not found: {input_dir}")

    output_root = resolve_path(project_root, str(cfg.output_dir), "output_dir")
    output_name = str(cfg.output_name).strip()
    if not output_name or output_name == "CHANGE_ME":
        raise ValueError("Please set output_name in config/extract.yaml or override it on the command line")

    output_dir = output_root / output_name
    ensure_empty_output_dir(output_dir)

    serials = load_serials(cfg)
    start_frame = int(cfg.start_frame)
    end_frame = int(cfg.end_frame)

    reference_frames = list_available_frames(input_dir / serials[0])
    selected_frames = select_frames(reference_frames, start_frame, end_frame)

    print(f"Input: {input_dir}")
    print(f"Output: {output_dir}")
    print(f"Selected frames: {selected_frames[0]} to {selected_frames[-1]} ({len(selected_frames)} frames)")

    for serial in serials:
        camera_dir = input_dir / serial
        if not camera_dir.exists():
            raise FileNotFoundError(f"Camera directory not found: {camera_dir}")

        available_frames = set(list_available_frames(camera_dir))
        missing_frames = [frame_idx for frame_idx in selected_frames if frame_idx not in available_frames]
        if missing_frames:
            raise RuntimeError(f"Serial {serial} is missing selected frames, first missing: {missing_frames[0]}")

        copy_camera_frames(input_dir, output_dir, serial, selected_frames)
        copy_filtered_timestamp_csv(input_dir, output_dir, serial, selected_frames)
        print(f"Copied serial {serial}")

    copy_calibration_info(input_dir, output_dir)
    print("Done.")


if __name__ == "__main__":
    main()
