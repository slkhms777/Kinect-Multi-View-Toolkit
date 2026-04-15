import pyk4a
from pyk4a import PyK4A, Config, connected_device_count
import cv2
import numpy as np
import time
from datetime import datetime
import os
master_id = 0
# master_serial = "000545703212"
master_serial = "000192203112"

device_cnt = connected_device_count()

def find_master_device():
    print("检测到设备数量:", device_cnt
          )
    find_master_id = -1
    for device_id in range(device_cnt):
        device = PyK4A(device_id=device_id)
        device.start()
        if device.serial == master_serial:
            find_master_id = device_id
            print(f"Master device ID: {find_master_id}, Serial: {device.serial}")
        device.stop()
    return find_master_id

def main(start_rank):
    device_idx = [i for i in range(device_cnt) if i != master_id]
    if master_id != -1:
        device_idx.append(master_id)
    cur_rank = start_rank

    device_info = [None] * device_cnt

    cur_time = datetime.now().strftime("%Y%m%d_%H%M%S")
    save_dir = os.path.join("recordings", cur_time)
    # 不在这里创建目录

    # 先初始化所有设备
    for id in device_idx:
        print(f"Initializing device ID: {id}")
        config = Config(
            color_resolution=pyk4a.ColorResolution.RES_720P,
            color_format=pyk4a.ImageFormat.COLOR_MJPG,
            depth_mode=pyk4a.DepthMode.WFOV_2X2BINNED,
            camera_fps=pyk4a.FPS.FPS_15,
            wired_sync_mode=pyk4a.WiredSyncMode.MASTER if id == master_id else pyk4a.WiredSyncMode.SUBORDINATE,
            subordinate_delay_off_master_usec=160 * cur_rank if id != master_id else 0,
            synchronized_images_only=True,
        )
        cur_rank += 1
        k4a = PyK4A(config=config, device_id=id)
        k4a.start()
        k4a.whitebalance_mode_auto = True
        # 先不创建record和目录
        device_info[id] = {
            'device': k4a,
            'config': config,
            'record': None,
            'is_recording': False,
            'first_frame_recorded': False  # 添加标志跟踪是否已记录第一帧
        }

    # 全部设备初始化成功后再创建目录
    os.makedirs(save_dir, exist_ok=True)

    # 再创建record对象
    for id in device_idx:
        k4a = device_info[id]['device']
        config = device_info[id]['config']
        record = pyk4a.PyK4ARecord(
            path=os.path.join(save_dir, f"{cur_time}_{k4a.serial}.mkv"),
            config=config,
            device=k4a
        )
        record.create()
        device_info[id]['record'] = record

    try:
        recording = False
        print("按 r 开始录制,按 s 停止录制,按 q 退出。")
        while True:
            for idx, info in enumerate(device_info):
                device = info['device']
                record = info['record']
                capture = device.get_capture()

                # 录制控制
                info['is_recording'] = recording

                # 录制
                if info['is_recording']:
                    # 如果是第一帧,记录时间戳
                    if not info['first_frame_recorded']:
                        timestamp_file = os.path.join(save_dir, f"{cur_time}_{device.serial}_first_frame_timestamp.txt")
                        with open(timestamp_file, 'w') as f:
                            # 记录设备时间戳(微秒)
                            if capture.color is not None:
                                color_timestamp = capture.color_timestamp_usec
                                f.write(f"Color timestamp (usec): {color_timestamp}\n")
                            if capture.depth is not None:
                                depth_timestamp = capture.depth_timestamp_usec
                                f.write(f"Depth timestamp (usec): {depth_timestamp}\n")
                            # 记录系统时间
                            system_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")
                            f.write(f"System time: {system_time}\n")
                        
                        info['first_frame_recorded'] = True
                        print(f"设备 {device.serial} 第一帧时间戳已保存到 {timestamp_file}")
                    
                    record.write_capture(capture)
                else:
                    # 重置第一帧标志
                    info['first_frame_recorded'] = False
                    
                    # 非录制时预览
                    if capture.color is not None and np.any(capture.color):
                        color_image = capture.color
                        if len(color_image.shape) == 1: # MJPG格式
                            color_image = cv2.imdecode(color_image, cv2.IMREAD_COLOR)
                        elif len(color_image.shape) == 3 and color_image.shape[2] == 4: # BGRA格式
                            color_image = color_image[:, :, :3]
                        cv2.imshow(f"Kinect_{idx}", cv2.resize(color_image, (640, 360)))

            key = cv2.waitKey(1) & 0xFF
            if key == ord('r'):
                recording = True
                print("开始录制")
            elif key == ord('s'):
                recording = False
                print("停止录制")
            elif key == ord('q'):
                print("退出程序。")
                break

    except KeyboardInterrupt:
        print("CTRL-C pressed. Exiting.")

    for info in device_info:
        record = info['record']
        record.flush()
        record.close()
    cv2.destroyAllWindows()

if __name__ == "__main__":
    master_id = find_master_device()
    main(start_rank=3)



