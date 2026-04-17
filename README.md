# Azure Kinect 分布式录制工具

本仓库用于 Azure Kinect 多设备同步录制及相关工具集。

## 功能概览

1. 同步录制
2. 帧导出 + 对齐
3. 提取内外参
4. 手动二次对齐（非必需）

## 1. 环境准备

### 1.1 建议环境

- Windows 操作系统
- Python ≥ 3.8

### 1.2 创建 conda 环境

```bash
conda create -n kinect python=3.10
conda activate kinect
```

### 1.3 安装 pyk4a 库

1. 安装 [Azure Kinect SDK v1.4.2](https://download.microsoft.com/download/d/c/1/dc1f8a76-1ef2-4a1a-ac89-a7e22b3da491/Azure%20Kinect%20SDK%201.4.2.exe)

默认 SDK 安装路径为 `C:\Program Files\Azure Kinect SDK v1.4.2`。

2. 安装 pyk4a

```bash
pip install pyk4a
```

如遇问题，请参考 [pyk4a 官方仓库](https://github.com/etiennedub/pyk4a)。

### 1.4 安装其他环境

```bash
pip install -r requirements.txt
```

## 2. 前置知识

### 2.1 节点

`node` / 节点：每台电脑被抽象为一个节点，每个节点可以连接多个相机（不建议超过 3 台）。

### 2.2 多相机硬件同步

所有相机（即使分布在不同节点上）都必须通过 3.5 mm 数据线进行硬件同步，并串行连接所有相机。

连接方式如下：

1. 使用 3.5 mm 数据线连接第一台相机的 `out` 口和第二台相机的 `in` 口。
2. 再使用第二根数据线连接第二台相机的 `out` 口和第三台相机的 `in` 口。
3. 后续相机以此类推。
4. 最后一台相机只有 `in` 口连接数据线，`out` 口闲置。
5. 第一台相机的 `in` 口闲置，并作为 master 相机。

示意图如下：

![Azure Kinect 多相机硬件同步连接示意图](docs/image.png)

### 2.3 时间戳

本项目中主要涉及两类时间戳：

1. 相机时间戳 / device timestamp

相机时间戳是各相机进行同步时参考的时间戳，以相机启动时间为基准。Master 相机初始化后，会通过 3.5 mm 数据线将 device time 传递到从属相机（Subordinate），从而实现相机时间戳的硬件对齐。

2. 系统时间戳 / system timestamp

系统时间戳来自各节点电脑的系统时间，主要用于和其他传感器数据进行同步。不同节点的系统时间戳不一定完全一致，可能存在细微差异，因此最终的 csv 文件会在系统时间戳中附带 node tag，用于区分其所属节点。

## 3. 录制

### 3.1 支持模式

本项目支持：

1. 单节点多相机
2. 多节点多相机

### 3.2 单节点多相机

对于单节点多相机，需要在 `config/record.yaml` 中设置：

```yaml
record:
  master_serial: "xxx"  # 替换为实际的主相机
  start_rank: 1         # 第一个非master相机距离master的距离为1
  machine_tag: "node0"  # 只有一台电脑，记为node0即可
```

运行时直接使用：

```bash
python multi_record.py
```

程序默认进入预览状态，可用于调整相机位置、桌面等。

在终端输入 `r` 开始录制。为确保外参一致，录制期间非常不建议移动桌面或相机。

输入 `s` 停止录制并退出。

### 3.3 多节点多相机

对于多节点多相机，需要在每个节点的 `config/record.yaml` 中设置：

#### 节点 0

```yaml
record:
  master_serial: "xxx"  # 替换为实际的主相机
  start_rank: 1         # 节点0的第一个非master相机距离master的距离为1
  machine_tag: "node0"  # 记为node0
```

#### 节点 1

```yaml
record:
  master_serial: "xxx"  # 替换为实际的主相机
  start_rank: xx        # 替换为该节点的第一台相机距离master的距离
  machine_tag: "node1"  # 记为node1
```

#### 节点 2 及后续节点

以此类推。

### 3.4 多节点启动顺序

运行时需要倒序启动。例如有三个节点 `node0`、`node1`、`node2`，需要先在 `node2` 上运行：

```bash
python multi_record.py
```

看到终端信息初始化成功后，再在 `node1` 上运行 `python multi_record.py`，最后在 `node0` 上运行 `python multi_record.py`。

正常情况下，在 `node0` 上运行 `python multi_record.py` 后，所有 node 的屏幕上都会出现预览窗口，此期间可用于调整相机位置等。

### 3.5 多节点录制与停止

- 录制：每台 node 节点电脑都要按 `r` 开始录制，不需要同步按。
- 停止：每台 node 节点电脑都要按 `s` 停止录制并退出，不需要同步按。

## 4. 数据对齐

### 4.1 配置与数据整理

先配置 `config/process_mkv.yaml`，建议只在 yaml 中根据实际情况维护以下长期不变或不频繁变化的参数：

```yaml
output_subdir: "processed"
fps: 30
all_serials:
  - "000033103112"
  - "000192203112"
  - "000541403212"
```

其中 `all_serials` 需要与本次参与处理的相机 serial 保持一致。

不建议直接修改 `config/process_mkv.yaml` 里的 `run_dir`。`run_dir` 通常每次录制都会变化，建议在运行脚本时通过命令行手动指定，避免把某次实验路径误写进配置文件后造成不必要的 bug。

如果是多节点录制，需要通过云盘或硬盘将所有 mkv + csv 数据拷贝到同一台 node 机器上。由于各个 node 节点开始录制的时刻可能存在细微差异，`run_dir` 下所有 mkv 和 csv 文件的时间戳前缀可能略有不同，需要手动重命名为统一的时间戳。

最终期望的 `run_dir` 格式如下所示，此时 `run_dir = "recordings/20260401_123456"`：

```text
recordings/
    20260401_123456/
        20260401_123456_<device_serial_1>.mkv
        20260401_123456_<device_serial_1>_system_timestamps.csv
        20260401_123456_<device_serial_2>.mkv
        20260401_123456_<device_serial_2>_system_timestamps.csv
        20260401_123456_<device_serial_n>.mkv
        20260401_123456_<device_serial_n>_system_timestamps.csv
```

### 4.2 运行对齐脚本

运行以下命令即可根据 device 时间戳进行精准帧对齐：

```bash
python tools/process_multi_mkv.py run_dir=recordings/20260401_123456
```

处理完成后，默认输出结构如下：

```text
recordings/
    20260401_123456/
        processed/
            <device_serial_1>/
                color_000000.jpg
                depth_000000.png
                ...
            <device_serial_n>/
                color_000000.jpg
                depth_000000.png
                ...
            timestep_info/
                <device_serial_1>_timestamps.csv
                <device_serial_n>_timestamps.csv
```

## 5. 提取内外参

### 5.1 配置标定参数

标定脚本配置文件为 `config/calib.yaml`。建议在 yaml 中提前根据实际情况配置好相机 serial、图片子目录、标定帧和标定板参数：

```yaml
sub_dir: "processed"
calibration_frame: 0
all_serials:
  - "000033103112"
  - "000192203112"
  - "000541403212"

checkerboard_size: [8, 11]
square_size: 0.03
```

参数说明：

- `sub_dir`：对齐脚本导出的图片目录，通常是 `processed`。
- `calibration_frame`：用于外参标定的帧号，例如 `0` 对应 `color_000000.jpg`。
- `all_serials`：需要参与标定的相机 serial，建议与处理后的图片目录保持一致。
- `checkerboard_size`：棋盘格内角点数量，格式为 `[列数, 行数]`。
- `square_size`：棋盘格每个格子的实际边长，单位为米。

与数据对齐脚本相同，不建议直接修改 `config/calib.yaml` 中的 `run_dir`。标定通常会针对不同录制目录反复运行，建议在命令行中手动指定 `run_dir`。

### 5.2 运行标定脚本

运行以下命令提取内参并估计外参：

```bash
python tools/calibration.py run_dir=recordings/20260401_123456
```

如果需要临时指定图片子目录或标定帧，可以使用：

```bash
python tools/calibration.py run_dir=recordings/20260401_123456 sub_dir=processed calibration_frame=0
```

内参通过 mkv 文件中的 Azure Kinect calibration 信息提取。外参通过 `calibration_frame` 指定的视频帧，检测多视角同时可见的棋盘格角点，并使用 `solvePnP` 估计每个相机相对于棋盘格世界坐标系的位姿。

标定结果默认保存到：

```text
recordings/
    20260401_123456/
        processed/
            calibration_info/
                intrinsics/
                    <device_serial_1>.yaml
                    <device_serial_n>.yaml
                extrinsics/
                    <device_serial_1>.yaml
                    <device_serial_n>.yaml
                test/
                    <device_serial_1>.jpg
                    <device_serial_n>.jpg
```

其中：

- `intrinsics/<serial>.yaml` 保存 color/depth 内参和 depth2color。
- `extrinsics/<serial>.yaml` 保存 `cam2world` 和 `world2cam`。
- `test/<serial>.jpg` 保存角点检测和 XYZ 坐标轴可视化结果，建议人工检查坐标轴投影是否合理。

## 6. 打包为 dataset

### 6.1 配置抽取参数

打包脚本配置文件为 `config/extract.yaml`。建议在 yaml 中提前维护相机 serial 和默认输出根目录：

```yaml
all_serials:
  - "000033103112"
  - "000192203112"
  - "000541403212"

output_dir: "datasets"
start_frame: 0
end_frame: -1
```

参数说明：

- `input_dir`：需要抽取的目录，通常是某次录制处理后的 `processed` 目录，例如 `recordings/20260401_123456/processed`。
- `all_serials`：需要打包进 dataset 的相机 serial，建议与 `processed` 目录中的相机子目录保持一致。
- `output_dir`：dataset 输出根目录，默认是 `datasets`。
- `output_name`：本次导出的 dataset 名称，例如 `20260401_123456_clip01`。
- `start_frame`：开始帧，默认 `0`。
- `end_frame`：结束帧，包含该帧；默认 `-1` 表示一直抽取到最后一帧。

与前面的脚本类似，`input_dir` 和 `output_name` 通常每次都会变化，建议在运行时通过命令行指定，不建议直接写死在 yaml 中。

### 6.2 运行打包脚本

例如，从 `processed` 中抽取第 `0` 到 `300` 帧并打包为一个 dataset：

```bash
python tools/extract_to_dataset.py input_dir=recordings/20260401_123456/processed output_name=20260401_123456_clip01 start_frame=0 end_frame=300
```

如果要从第 `0` 帧一直抽取到最后一帧，可以使用：

```bash
python tools/extract_to_dataset.py input_dir=recordings/20260401_123456/processed output_name=20260401_123456_full start_frame=0 end_frame=-1
```

默认输出到：

```text
datasets/
    20260401_123456_clip01/
        <device_serial_1>/
            color_000000.jpg
            depth_000000.png
            ...
        <device_serial_n>/
            color_000000.jpg
            depth_000000.png
            ...
        timestep_info/
            <device_serial_1>_timestamps.csv
            <device_serial_n>_timestamps.csv
        calibration_info/
            intrinsics/
            extrinsics/
            test/
```

脚本会复制指定帧范围内的 color/depth 图片，并过滤 `timestep_info` 中的时间戳 csv。如果输入目录下存在 `calibration_info`，也会一起复制到 dataset 中。

为避免误覆盖已有数据，如果目标目录 `datasets/<output_name>` 已经存在且不为空，脚本会直接抛出异常。需要重新导出时，请换一个 `output_name`，或者先手动删除旧目录。

