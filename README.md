# Azure Kinect 多机录制与同步导出

这个项目用于 Azure Kinect 的多设备同步录制，以及录制完成后的离线帧导出。

当前仓库主要包含两条工作流：

- `multi_record.py`：多机同步录制，输出 `.mkv` 和逐帧系统时间戳
- `tools/process_multi_mkv.py`：对一轮录制结果做同步裁剪，并导出 color/depth 图像与时间戳

## 1. 环境准备

建议环境：

- Windows
- Python 3.10 及以上
- Azure Kinect SDK
- `pyk4a`
- `opencv-python`
- `hydra-core`
- `numpy`

如果你使用虚拟环境，建议先创建并激活环境后再安装依赖。

## 2. 录制配置

录制脚本使用 `config/config.yaml`。

常用配置项：

- `record.master_serial`：主设备序列号
- `record.start_rank`：从设备同步延迟的起始 rank
- `record.resolution`：彩色分辨率，例如 `720p`
- `record.fps`：录制帧率，例如 `15` 或 `30`
- `record.output_dir`：录制输出根目录
- `preview.width` / `preview.height`：预览窗口尺寸

## 3. 开始录制

运行：

```powershell
python .\multi_record.py
```

启动后按键说明：

- `r`：开始录制
- `s`：停止录制并立即退出
- `q`：直接退出

注意：

- 每次运行只允许录制一次
- 每台设备都会输出一个 `.mkv`
- 每台设备还会输出一个逐帧系统时间戳文件

录制输出目录结构示例：

```text
recordings/
    20260415_120000/
        20260415_120000_000033103112.mkv
        20260415_120000_000033103112_system_timestamps.csv
        20260415_120000_000192203112.mkv
        20260415_120000_000192203112_system_timestamps.csv
        ...
```

其中 `*_system_timestamps.csv` 包含：

```text
frame_index,system_timestamp_sec,system_timestamp_ns
```

- `system_timestamp_sec`：秒级时间戳，保留 4 位小数
- `system_timestamp_ns`：原始纳秒级时间戳

## 4. 后处理配置

同步导出脚本使用 `config/process_mkv.yaml`。

常用配置项：

- `run_dir`：某一轮录制数据目录，例如 `recordings/20260415_120000`
- `output_subdir`：导出结果目录名，例如 `processed`
- `fps`：该轮录制对应的目标帧率
- `all_serials`：本轮需要处理的全部设备序列号

## 5. 导出同步后的图像和时间戳

运行：

```powershell
python .\tools\process_multi_mkv.py
```

脚本会执行以下逻辑：

1. 遍历所有 `.mkv`，提取每一帧的 device timestamp
2. 找到最晚开始的相机，作为开始同步标记
3. 找到最早结束的相机，作为结束同步标记
4. 用最晚开始相机在同步窗口内的有效帧数，作为全局有效帧数
5. 对每个相机找到最接近开始同步标记的帧，并连续导出相同数量的帧
6. 同时保存每一帧对应的 device timestamp 和 system timestamp

导出目录结构示例：

```text
recordings/
    20260415_120000/
        processed/
            000033103112/
                color_000000.jpg
                depth_000000.png
                color_000001.jpg
                depth_000001.png
                ...
            000192203112/
                color_000000.jpg
                depth_000000.png
                ...
            000033103112_timestamps.csv
            000192203112_timestamps.csv
            ...
```

其中 `<device_serial>_timestamps.csv` 包含：

```text
frame_index,source_frame_index,device_timestamp_usec,system_timestamp_sec,system_timestamp_ns
```

字段说明：

- `frame_index`：导出后的连续帧序号
- `source_frame_index`：该帧在原始 `.mkv` 中的帧序号
- `device_timestamp_usec`：设备时间戳，单位微秒
- `system_timestamp_sec`：录制时保存的秒级系统时间戳
- `system_timestamp_ns`：录制时保存的纳秒级系统时间戳

## 6. 异常检查

`tools/process_multi_mkv.py` 会打印每个相机的摘要信息，包括：

- 总帧数
- 起止 device timestamp
- 时长
- 实际 FPS

实际 FPS 通过 `num_frames / duration` 计算。如果它和配置中的 `fps` 明显不一致，脚本会给出警告。这通常意味着：

- 视频损坏
- 存在掉帧
- 存在无效帧
- 当前配置和实际录制参数不一致

## 7. 适用场景

这个仓库适合以下工作：

- 多台 Azure Kinect 的同步录制
- 基于硬件同步时间戳的离线对齐
- 为三维重建、人体动作捕捉、深度融合等任务准备多视角数据
