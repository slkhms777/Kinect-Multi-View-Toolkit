"""
Extract Azure Kinect intrinsics from MKV files and estimate multi-view extrinsics.

The selected checkerboard frame defines the world coordinate system:
world origin is the first checkerboard corner, X/Y follow the checkerboard
corner order, and Z is perpendicular to the board plane.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import cv2
import hydra
import numpy as np
import yaml
from omegaconf import DictConfig
from pyk4a import CalibrationType, PyK4APlayback

cv2.ocl.setUseOpenCL(False)


@dataclass
class CameraIntrinsics:
    color_matrix: np.ndarray
    color_distortion: np.ndarray
    color_size: tuple[int, int]
    depth_matrix: np.ndarray
    depth_distortion: np.ndarray
    depth_size: tuple[int, int]
    depth2color: np.ndarray


@dataclass
class CameraCalibration:
    serial: str
    mkv_path: Path
    color_path: Path
    depth_path: Path
    intrinsics: CameraIntrinsics
    corners: np.ndarray
    rvec: np.ndarray
    tvec: np.ndarray
    world2cam: np.ndarray
    cam2world: np.ndarray
    reprojection_error_px: float


def resolve_path(project_root: Path, value: str, field_name: str) -> Path:
    path_value = str(value).strip()
    if not path_value or path_value == "CHANGE_ME":
        raise ValueError(f"Please set {field_name} in config/calib.yaml")

    path = Path(path_value)
    if not path.is_absolute():
        path = project_root / path
    return path.resolve()


def discover_serials(run_dir: Path) -> list[str]:
    prefix = f"{run_dir.name}_"
    serials: list[str] = []
    for mkv_path in sorted(run_dir.glob(f"{prefix}*.mkv")):
        serial = mkv_path.stem.removeprefix(prefix)
        if serial:
            serials.append(serial)

    if not serials:
        raise RuntimeError(f"No MKV files found in {run_dir}")
    return serials


def load_serials(cfg: DictConfig, run_dir: Path) -> list[str]:
    configured = [str(serial).strip() for serial in cfg.get("all_serials", [])]
    serials = [serial for serial in configured if serial and not serial.startswith("#")]
    return serials or discover_serials(run_dir)


def load_image_size(image_path: Path, flags: int) -> tuple[int, int]:
    image = cv2.imread(str(image_path), flags)
    if image is None:
        raise FileNotFoundError(f"Failed to read image: {image_path}")
    height, width = image.shape[:2]
    return width, height


def get_depth_to_color_matrix(calibration: Any) -> np.ndarray:
    if hasattr(calibration, "get_extrinsic_parameters"):
        rotation, translation = calibration.get_extrinsic_parameters(CalibrationType.DEPTH, CalibrationType.COLOR)
        rotation_matrix = np.asarray(rotation, dtype=np.float64).reshape(3, 3)
        translation_vector = (np.asarray(translation, dtype=np.float64) * 1000.0).reshape(3, 1)
        return np.hstack([rotation_matrix, translation_vector])

    if not hasattr(calibration, "get_extrinsics"):
        raise RuntimeError(
            "This pyk4a Calibration object does not expose depth-to-color extrinsics. "
            "Expected get_extrinsic_parameters(...) or get_extrinsics(...)."
        )

    extrinsics = calibration.get_extrinsics(CalibrationType.DEPTH, CalibrationType.COLOR)
    matrix = np.asarray(extrinsics, dtype=np.float64)

    if matrix.shape == (4, 4):
        return matrix[:3, :4]
    if matrix.shape == (3, 4):
        return matrix
    if matrix.size == 12:
        return matrix.reshape(3, 4)

    rotation = getattr(extrinsics, "rotation", None)
    translation = getattr(extrinsics, "translation", None)
    if rotation is not None and translation is not None:
        rotation_matrix = np.asarray(rotation, dtype=np.float64).reshape(3, 3)
        translation_vector = (np.asarray(translation, dtype=np.float64) * 1000.0).reshape(3, 1)
        return np.hstack([rotation_matrix, translation_vector])

    raise RuntimeError(f"Unexpected depth2color extrinsics shape: {matrix.shape}")


def extract_intrinsics(mkv_path: Path, color_size: tuple[int, int], depth_size: tuple[int, int]) -> CameraIntrinsics:
    playback = PyK4APlayback(path=str(mkv_path))
    playback.open()
    try:
        calibration = playback.calibration
        color_matrix = np.asarray(calibration.get_camera_matrix(CalibrationType.COLOR), dtype=np.float64)
        color_distortion = np.asarray(
            calibration.get_distortion_coefficients(CalibrationType.COLOR),
            dtype=np.float64,
        ).reshape(-1)
        depth_matrix = np.asarray(calibration.get_camera_matrix(CalibrationType.DEPTH), dtype=np.float64)
        depth_distortion = np.asarray(
            calibration.get_distortion_coefficients(CalibrationType.DEPTH),
            dtype=np.float64,
        ).reshape(-1)
        depth2color = get_depth_to_color_matrix(calibration)
    finally:
        playback.close()

    if color_matrix.shape != (3, 3):
        raise RuntimeError(f"Unexpected color camera matrix shape for {mkv_path}: {color_matrix.shape}")
    if depth_matrix.shape != (3, 3):
        raise RuntimeError(f"Unexpected depth camera matrix shape for {mkv_path}: {depth_matrix.shape}")

    return CameraIntrinsics(
        color_matrix=color_matrix,
        color_distortion=color_distortion,
        color_size=color_size,
        depth_matrix=depth_matrix,
        depth_distortion=depth_distortion,
        depth_size=depth_size,
        depth2color=depth2color,
    )


def make_checkerboard_points(checkerboard_size: tuple[int, int], square_size: float) -> np.ndarray:
    cols, rows = checkerboard_size
    if cols <= 0 or rows <= 0:
        raise ValueError(f"Invalid checkerboard size: {checkerboard_size}")
    if square_size <= 0:
        raise ValueError(f"Invalid checkerboard square_size: {square_size}")

    points = np.zeros((rows * cols, 3), np.float32)
    points[:, :2] = np.mgrid[0:cols, 0:rows].T.reshape(-1, 2)
    points *= float(square_size)
    return points


def find_checkerboard_corners(color_path: Path, checkerboard_size: tuple[int, int]) -> tuple[np.ndarray, np.ndarray]:
    image = cv2.imread(str(color_path), cv2.IMREAD_COLOR)
    if image is None:
        raise FileNotFoundError(f"Failed to read calibration image: {color_path}")

    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    flags = cv2.CALIB_CB_ADAPTIVE_THRESH | cv2.CALIB_CB_NORMALIZE_IMAGE
    found, corners = cv2.findChessboardCorners(gray, checkerboard_size, flags)

    if not found and hasattr(cv2, "findChessboardCornersSB"):
        found, corners = cv2.findChessboardCornersSB(gray, checkerboard_size)

    if not found or corners is None:
        raise RuntimeError(
            f"Checkerboard {checkerboard_size} not found in {color_path}. "
            "Choose a calibration_frame where the full board is visible."
        )

    corners = np.asarray(corners, dtype=np.float32).reshape(-1, 1, 2)
    criteria = (
        cv2.TERM_CRITERIA_EPS + cv2.TERM_CRITERIA_MAX_ITER,
        30,
        0.001,
    )
    corners = cv2.cornerSubPix(gray, corners, (11, 11), (-1, -1), criteria)
    return image, corners


def invert_transform(transform: np.ndarray) -> np.ndarray:
    inverse = np.eye(4, dtype=np.float64)
    rotation = transform[:3, :3]
    translation = transform[:3, 3]
    inverse[:3, :3] = rotation.T
    inverse[:3, 3] = -rotation.T @ translation
    return inverse


def estimate_camera_pose(
    object_points: np.ndarray,
    image_points: np.ndarray,
    camera_matrix: np.ndarray,
    distortion: np.ndarray,
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray, float]:
    success, rvec, tvec = cv2.solvePnP(
        object_points,
        image_points,
        camera_matrix,
        distortion,
        flags=cv2.SOLVEPNP_ITERATIVE,
    )
    if not success:
        raise RuntimeError("cv2.solvePnP failed to estimate checkerboard pose")

    rotation, _ = cv2.Rodrigues(rvec)
    world2cam = np.eye(4, dtype=np.float64)
    world2cam[:3, :3] = rotation
    world2cam[:3, 3] = tvec.reshape(3)
    cam2world = invert_transform(world2cam)

    projected, _ = cv2.projectPoints(object_points, rvec, tvec, camera_matrix, distortion)
    reprojection_error = float(
        np.sqrt(np.mean(np.sum((projected.reshape(-1, 2) - image_points.reshape(-1, 2)) ** 2, axis=1)))
    )
    return rvec, tvec, world2cam, cam2world, reprojection_error


def calibrate_camera(
    run_dir: Path,
    image_root: Path,
    serial: str,
    calibration_frame: int,
    checkerboard_size: tuple[int, int],
    object_points: np.ndarray,
) -> CameraCalibration:
    mkv_path = run_dir / f"{run_dir.name}_{serial}.mkv"
    if not mkv_path.exists():
        raise FileNotFoundError(f"MKV file not found for serial {serial}: {mkv_path}")

    color_path = image_root / serial / f"color_{calibration_frame:06d}.jpg"
    depth_path = image_root / serial / f"depth_{calibration_frame:06d}.png"
    color_size = load_image_size(color_path, cv2.IMREAD_COLOR)
    depth_size = load_image_size(depth_path, cv2.IMREAD_UNCHANGED)
    intrinsics = extract_intrinsics(mkv_path, color_size, depth_size)
    _, corners = find_checkerboard_corners(color_path, checkerboard_size)
    rvec, tvec, world2cam, cam2world, reprojection_error = estimate_camera_pose(
        object_points,
        corners,
        intrinsics.color_matrix,
        intrinsics.color_distortion,
    )

    return CameraCalibration(
        serial=serial,
        mkv_path=mkv_path,
        color_path=color_path,
        depth_path=depth_path,
        intrinsics=intrinsics,
        corners=corners,
        rvec=rvec.reshape(3),
        tvec=tvec.reshape(3),
        world2cam=world2cam,
        cam2world=cam2world,
        reprojection_error_px=reprojection_error,
    )


def to_plain(value: Any) -> Any:
    if isinstance(value, np.ndarray):
        return to_plain(value.tolist())
    if isinstance(value, np.generic):
        return value.item()
    if isinstance(value, list):
        return [to_plain(item) for item in value]
    if isinstance(value, tuple):
        return [to_plain(item) for item in value]
    if isinstance(value, dict):
        return {key: to_plain(item) for key, item in value.items()}
    return value


def opencv_coeffs(distortion: np.ndarray) -> list[float]:
    coeffs = np.asarray(distortion, dtype=np.float64).reshape(-1)
    return [float(value) for value in coeffs.tolist()]


def camera_model_payload(size: tuple[int, int], camera_matrix: np.ndarray, distortion: np.ndarray) -> dict[str, Any]:
    return {
        "width": size[0],
        "height": size[1],
        "fx": float(camera_matrix[0, 0]),
        "fy": float(camera_matrix[1, 1]),
        "ppx": float(camera_matrix[0, 2]),
        "ppy": float(camera_matrix[1, 2]),
        "coeffs": opencv_coeffs(distortion),
    }


def extrinsics_payload(calibration: CameraCalibration) -> dict[str, Any]:
    return {
        "serial": str(calibration.serial),
        "cam2world": calibration.cam2world.tolist(),
        "world2cam": calibration.world2cam.tolist(),
    }


def intrinsics_payload(calibration: CameraCalibration) -> dict[str, Any]:
    intrinsics = calibration.intrinsics
    return {
        "serial": str(calibration.serial),
        "color": camera_model_payload(
            intrinsics.color_size,
            intrinsics.color_matrix,
            intrinsics.color_distortion,
        ),
        "depth": camera_model_payload(
            intrinsics.depth_size,
            intrinsics.depth_matrix,
            intrinsics.depth_distortion,
        ),
        "depth2color": intrinsics.depth2color.reshape(-1).tolist(),
    }


def write_yaml(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as file:
        yaml.safe_dump(
            to_plain(payload),
            file,
            sort_keys=False,
            default_flow_style=False,
            allow_unicode=True,
        )


def draw_projected_axis(
    image: np.ndarray,
    projected_points: np.ndarray,
    start: int,
    end: int,
    color: tuple[int, int, int],
    label: str,
) -> None:
    p0 = tuple(np.rint(projected_points[start]).astype(int))
    p1 = tuple(np.rint(projected_points[end]).astype(int))
    cv2.arrowedLine(image, p0, p1, color, 3, cv2.LINE_AA, tipLength=0.08)
    cv2.putText(image, label, (p1[0] + 6, p1[1] + 6), cv2.FONT_HERSHEY_SIMPLEX, 0.8, color, 2, cv2.LINE_AA)


def draw_calibration_debug(
    output_path: Path,
    calibration: CameraCalibration,
    object_points: np.ndarray,
) -> None:
    image = cv2.imread(str(calibration.color_path), cv2.IMREAD_COLOR)
    if image is None:
        raise FileNotFoundError(f"Failed to read calibration image: {calibration.color_path}")

    cv2.drawChessboardCorners(
        image,
        (len(np.unique(object_points[:, 0])), len(np.unique(object_points[:, 1]))),
        calibration.corners,
        True,
    )

    axis_length = float(np.linalg.norm(object_points[1] - object_points[0]) * 3.0)
    axis_points = np.asarray(
        [
            [0.0, 0.0, 0.0],
            [axis_length, 0.0, 0.0],
            [0.0, axis_length, 0.0],
            [0.0, 0.0, -axis_length],
        ],
        dtype=np.float32,
    )
    axis_pixels, _ = cv2.projectPoints(
        axis_points,
        calibration.rvec,
        calibration.tvec,
        calibration.intrinsics.color_matrix,
        calibration.intrinsics.color_distortion,
    )
    axis_pixels = axis_pixels.reshape(-1, 2)
    draw_projected_axis(image, axis_pixels, 0, 1, (0, 0, 255), "X")
    draw_projected_axis(image, axis_pixels, 0, 2, (0, 255, 0), "Y")
    draw_projected_axis(image, axis_pixels, 0, 3, (255, 0, 0), "Z")

    origin = tuple(np.rint(axis_pixels[0]).astype(int))
    cv2.circle(image, origin, 6, (0, 0, 255), -1, cv2.LINE_AA)
    cv2.putText(
        image,
        f"reprojection error: {calibration.reprojection_error_px:.4f}px",
        (16, 32),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.8,
        (0, 255, 255),
        2,
        cv2.LINE_AA,
    )

    output_path.parent.mkdir(parents=True, exist_ok=True)
    if not cv2.imwrite(str(output_path), image):
        raise RuntimeError(f"Failed to write debug image: {output_path}")


def save_calibration_outputs(calibration_dir: Path, calibration: CameraCalibration, object_points: np.ndarray) -> None:
    serial = calibration.serial
    write_yaml(calibration_dir / "extrinsics" / f"{serial}.yaml", extrinsics_payload(calibration))
    write_yaml(calibration_dir / "intrinsics" / f"{serial}.yaml", intrinsics_payload(calibration))
    draw_calibration_debug(calibration_dir / "test" / f"{serial}.jpg", calibration, object_points)


@hydra.main(version_base=None, config_path="../config", config_name="calib")
def main(cfg: DictConfig) -> None:
    project_root = Path(hydra.utils.get_original_cwd())
    run_dir = resolve_path(project_root, str(cfg.run_dir), "run_dir")
    if not run_dir.exists():
        raise FileNotFoundError(f"Recording directory not found: {run_dir}")

    sub_dir = str(cfg.sub_dir).strip()
    if not sub_dir or sub_dir == "CHANGE_ME":
        raise ValueError("Please set sub_dir in config/calib.yaml")

    image_root = Path(sub_dir)
    if not image_root.is_absolute():
        image_root = run_dir / image_root
    image_root = image_root.resolve()
    if not image_root.exists():
        raise FileNotFoundError(f"Image directory not found: {image_root}")

    calibration_frame = int(cfg.calibration_frame)
    if calibration_frame < 0:
        raise ValueError("calibration_frame must be >= 0")

    checkerboard_size_config = cfg.get("checkerboard_size", cfg.get("checkerboard", {}).get("size"))
    if checkerboard_size_config is None:
        raise ValueError("Please set checkerboard_size in config/calib.yaml")
    checkerboard_size = tuple(int(value) for value in checkerboard_size_config)
    if len(checkerboard_size) != 2:
        raise ValueError("checkerboard_size must contain two integers: [columns, rows]")
    square_size = float(cfg.get("square_size", cfg.get("checkerboard", {}).get("square_size", 0.03)))

    serials = load_serials(cfg, run_dir)
    object_points = make_checkerboard_points(checkerboard_size, square_size)
    calibration_dir = image_root / "calibration_info"

    for serial in serials:
        print(f"Calibrating serial {serial} with frame {calibration_frame}")
        camera_calibration = calibrate_camera(
            run_dir=run_dir,
            image_root=image_root,
            serial=serial,
            calibration_frame=calibration_frame,
            checkerboard_size=checkerboard_size,
            object_points=object_points,
        )
        save_calibration_outputs(calibration_dir, camera_calibration, object_points)
        print(f"  reprojection error: {camera_calibration.reprojection_error_px:.4f} px")

    print(f"Done. Calibration saved to: {calibration_dir}")


if __name__ == "__main__":
    main()
