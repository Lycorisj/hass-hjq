# 实测规格与 HomeKit 能力说明

测试时间：2026-09-09。账号、直播 URL、内网 IP/MAC 已脱敏。

测试对象：同一移动爱家账号下两路 **合家亲共享** 视洞 B33（`IPC_SD_B33_1`）。

**结论（先看这里）**

- **Home Assistant 直播**：支持。默认转码为 H.264 Constrained Baseline 1280×720 @ 1500k + AAC；分辨率、码率、音频、是否转码可配。
- **Home Assistant 录制**：支持。录制开关分段 MP4 + `camera.record` 短片；分段时长可配（30–3600s），编码跟随直播转码选项。
- **HomeKit 直播**：服务侧支持（配件已拉起、本地 HLS 可 copy）。家庭 App 需在家用局域网配对。分辨率/码率跟随本集成选项。
- **HomeKit 录制 / 人脸 / 活动区域**：不支持（依赖 HKSV；HA Bridge 不做 HKSV）。
- **动作感应器**：云端移动侦测已开，HA/HomeKit **尚未接线**；做成 `binary_sensor` 再 `linked_motion_sensor` 可行。

### 产物

| 文件 | 内容 |
| --- | --- |
| [artifacts/stream-probe.redacted.json](artifacts/stream-probe.redacted.json) | 云端 HEVC 探针、HA 默认转码/录像、HomeKit HAP 端口 |
| [artifacts/ha-options.json](artifacts/ha-options.json) | HA 可配项与写死参数 |
| [artifacts/entity-attributes.example.json](artifacts/entity-attributes.example.json) | 摄像头实体上的规格属性 |
| [artifacts/function-config.redacted.json](artifacts/function-config.redacted.json) | App `functionConfig`（无人脸项） |
| [artifacts/homekit-hap.redacted.json](artifacts/homekit-hap.redacted.json) | HomeKit Bridge 实测与能力边界 |
| [artifacts/homekit-unsupported-feasibility.json](artifacts/homekit-unsupported-feasibility.json) | 不支持项的可行性评级与推荐路线 |
| [HOMEKIT_UNSUPPORTED.md](HOMEKIT_UNSUPPORTED.md) | 不支持项的详细方案（运动/区域/人脸/HKSV/对讲/云台） |
| [artifacts/ha-device-page.png](artifacts/ha-device-page.png) | HA 设备页：Streaming + Recording 开关（无实况画面） |

---

## 1. 云端源流（不可由本集成改编码）

厂家只给云端直播，没有 RTSP/ONVIF。`getLiveAddress` 返回的是带签名的 HTTP MPEG-TS（路径里带 `/flv/`，`ffprobe` 识别为 `mpegts`）。

| 项目 | B33-2 | B33-1 | 可配？ |
| --- | --- | --- | --- |
| 容器 | MPEG-TS | MPEG-TS | 否，云端决定 |
| 视频 | HEVC Main 2304×1296 @ 16fps yuv420p | HEVC Main 2304×1296 @ 15fps | 否。App 里有「清晰度」字段，当前接口未暴露可切换子码流 |
| 音频 | AAC LC | AAC LC | 否 |
| 会话保活 | `timeout=60s`，签名 URL 约 1 小时 | 同左 | 保活间隔固定 20s（小于 60s） |
| 接入类型 | `shareType=1` 合家亲共享 | 同左 | — |

App 侧 `functionConfig`（完整表见 [artifacts/function-config.redacted.json](artifacts/function-config.redacted.json)，**不等于** HA/HomeKit 已接入）：

| functionId | 名称 | App 开关 | HA | HomeKit |
| --- | --- | --- | --- | --- |
| 1 | 云台控制 | on | 未接入 | HAP 无标准摄像头云台 |
| 9 | 移动侦测 | on | 未接入 | 需 `linked_motion_sensor` |
| 36 | 声音侦测 | on | 未接入 | 无标准服务 |
| 13 | 移动追踪 | on | 未接入 | 无 |
| 49 | 区域设置 | on | 未接入 | 家庭 App 区域 = HKSV |
| 14 | 语音对讲 | on | 未接入 | 不是门铃按钮 |
| 7 | 云存储 | on（需购买） | 未接入 | 与 HKSV 不是同一套 |
| 29 | 录像直存 | on | 用 HA 本地 MP4 代替 | 无 |
| — | **人脸识别** | **列表无此项** | 无 | 仅 HKSV + 家庭中枢 |

---

## 2. Home Assistant 直播

默认开启 H.264 转码。HA 前端、`camera_proxy` 预览图、`camera.record` 都走转码后的本地 HLS。

### 实测默认输出

| 项目 | 实测结果 | 可配？ | 怎么配 |
| --- | --- | --- | --- |
| 容器 | HLS（1s 分片，窗口 5 片） | 否 | 写死为低延迟预览 |
| 视频编码 | H.264 Constrained Baseline 3.1，无 B 帧，GOP 30 | 否（为兼容 HomeKit） | 写死 `libx264 ultrafast + zerolatency + baseline` |
| 分辨率 | **1280×720**（源 2304×1296 缩小） | **是** | 集成选项「转码分辨率」：`1280:720` / `1920:1080` / `source` |
| 视频码率 | **1500k**（VBV maxrate=码率，bufsize=2×） | **是** | 集成选项「转码码率」：`800k` / `1500k` / `2500k` |
| 帧率 | 跟随源，约 15–16fps，不升帧 | 否 | 源就是 ~15fps；HomeKit 侧可用 `max_fps` 再限 |
| 音频 | AAC 16kHz / 单声道 / 64k | 开/关可配，参数写死 | 集成选项「包含音频」 |
| 预览图 | JPEG 1280×720；`width=80&height=80` 缩略图可用 | 跟随分辨率 | HA `camera_proxy` 的 width/height |
| 协议 | HA Stream → HLS；实体 `supported_features=STREAM` | — | 关闭转码则直接喂云端 HEVC，Safari/部分卡片能播，HomeKit 不行 |

摄像头实体属性会带上当前输出规格（`video_codec`、`video_resolution`、`video_bitrate`、`audio_*`、`record_segment_seconds` 等），改选项后重载集成生效。示例见 [artifacts/entity-attributes.example.json](artifacts/entity-attributes.example.json)。选项是 **账号级**，两路摄像头共用；暂无单路覆盖。可配项清单见 [artifacts/ha-options.json](artifacts/ha-options.json)。

关闭「转码为 H.264」时：HA 直接播云端 HEVC 2304×1296，CPU 低，家庭 App 实时预览通常失败。

---

## 3. Home Assistant 录制

两条路径，编码都落在转码后的 H.264 上（转码开启时用 `copy` 封装，不再二次编码）。

| 路径 | 实测 | 可配？ |
| --- | --- | --- |
| **录制开关** `switch.*_recording` | 持续分段 MP4。B33-2 实测约 **50.5s**、H.264 1280×720 + AAC，文件在 `/config/www/hass_hjq/` | 分段时长 **30–3600 秒**（默认 300）；分辨率/码率/音频跟随直播转码选项；目录暂不可配 |
| **`camera.record`** | B33-1 请求 8s，得到 H.264 1280×720 + AAC 的 MP4 | **时长、文件名**由服务参数决定；编码跟随当前直播源 |

HA 没有内置 NVR 时间轴。回放就是目录里的 MP4（`/local/hass_hjq/` 或媒体浏览器）。运动触发自动录像需要先有运动实体，目前还没有。

---

## 4. HomeKit 功能对照

测试环境是云 VM：HomeKit **配件服务已拉起**（mDNS `_hap._tcp`、HAP 端口 21064/21065、类别 Camera=`ci=17`、本地 HLS 可 `copy` 成 Constrained Baseline 1280×720），但 **家庭 App 无法配对**（没有与 iPhone 同一局域网）。下表区分「本集成 / HA HomeKit Bridge 能力」和「苹果家庭 App 里看起来怎样」。

| 功能 | 现状 | 规格 / 可配性 | 说明 |
| --- | --- | --- | --- |
| **实时预览** | **支持**（服务侧已验证；家庭 App 需在家用局域网配对） | 视频 H.264 Baseline 1280×720 @ ~15fps 1500k；音频需再转成 HomeKit 的 AAC-ELD/Opus。分辨率、码率、音频跟随本集成选项。HomeKit 还可配 `video_codec`（建议 `copy`）、`max_width`/`max_height`（默认 1920×1080）、`max_fps`（默认 30）、`support_audio` | 必须配件模式；转码打开后勾选「原生 H.264」避免二次编码 |
| **录制（家庭 App 时间轴 / iCloud）** | **不支持** | — | 这是 **HomeKit Secure Video (HKSV)**。HA 官方文档写明 HomeKit Bridge **不支持 HKSV**。回放请用 HA 本地 MP4 |
| **人脸识别** | **不支持** | — | 苹果人脸/人物分类跑在家庭中枢上，且绑定 HKSV。云端 `functionConfig` 也没有人脸项 |
| **动作感应 / 移动通知** | **HomeKit 通知：未接线**；**云端侦测：App 已开，HA 未接入** | HA HomeKit 支持 `linked_motion_sensor`，只要有一个 `binary_sensor`/`event` 即可在家庭 App 弹「检测到移动」。本集成目前 **没有** 运动实体 | 见 §5.1 |
| **活动区域** | **不支持** | — | 家庭 App 活动区域是 HKSV 功能。云端 App 有「区域设置」，本集成未接该 API | 见 §5.2 |
| **门铃 / 对讲** | **不支持** | HomeKit 可 `linked_doorbell_sensor`；B33 有对讲但不是门铃按钮 | 对讲是双向语音，和 HomeKit 门铃不是同一套协议 |
| **云台** | **不支持** | App 有云台 | 可后续做成 HA `button`/`number`，HomeKit 无标准摄像头云台服务 |

### HomeKit 直播建议配置（家里 HA）

```yaml
homekit:
  - name: B33-2
    port: 21064
    mode: accessory
    filter:
      include_entities:
        - camera.shi_dong_she_xiang_tou_b33_2
        # 将来有运动实体后再加上：
        # - binary_sensor.shi_dong_she_xiang_tou_b33_2_motion
    entity_config:
      camera.shi_dong_she_xiang_tou_b33_2:
        video_codec: copy
        support_audio: true
        max_width: 1280
        max_height: 720
        max_fps: 15
        # linked_motion_sensor: binary_sensor.shi_dong_she_xiang_tou_b33_2_motion
```

勾选原生 H.264 后，家庭 App 实时预览应直接 copy 本集成的 720p Baseline 流。

---

## 5. 不支持项：开发可行性

详细方案、接口抓包步骤、YAML 和明确不做的事项见 [HOMEKIT_UNSUPPORTED.md](HOMEKIT_UNSUPPORTED.md)。这里只保留对照。

家庭 App 摄像头是三层，不要混在一块做：

| 层 | HA HomeKit Bridge | 本集成现状 |
| --- | --- | --- |
| HAP 实时预览 | 支持 | 已提供 H.264 HLS |
| HAP 运动/门铃事件 | 支持 `linked_motion_sensor` / `linked_doorbell_sensor` | **还没有**对应实体 |
| HKSV（时间轴、人脸、活动区域、iCloud） | **官方不支持** | 不要在本仓库实现 HAP Recording |

| 功能 | 可行性 | 推荐做法 | 不建议 |
| --- | --- | --- | --- |
| **移动通知** | **高**，中等工作量，**不需要 HKSV** | 抓包告警 → `binary_sensor.*_motion` → `linked_motion_sensor` | 对 2304p HEVC 做本地运动检测 |
| **活动区域** | 家庭 App：**只能走 HKSV**；HA 自己用云端框：中等 | 要在家庭 App 画框 → Scrypted HKSV。只要过滤告警 → 接云端「区域设置」API，家庭 App 看不见 | 以为接到 HA 后家庭 App 会出区域 UI |
| **人脸 / 认识的人** | 仅 HKSV + 家庭中枢 + iCloud+ | Scrypted 开录制后由苹果中枢做人脸 | 本集成做人脸模型；Frigate 填不满家庭 App 人物库。B33 `functionConfig` 也无人脸项 |
| **录制 / 活动历史** | 旁路 **高**；写进本仓库 **很大** | 把 H.264 再以 RTSP/HTTP 给 Scrypted；同一路不要既 Bridge 又 Scrypted HomeKit。当前 HLS 在容器 `/tmp`，Scrypted 默认够不到 | 自研 `CameraRecordingManagement` + Data Stream |
| **门铃 / 对讲** | 听声音：**高**（`support_audio`）；对讲：**低**；门铃：硬件不是门铃 | 直播带音频即可。对讲若做也只做 HA `button`，需另抓厂家 talk 通道 | 用运动实体冒充 `linked_doorbell_sensor` |
| **云台** | HA：**中**；家庭 App：**无标准服务** | 抓包 PTZ → HA `button`/`number` | 在家庭 App 里堆方向开关 |

两条总路：

- **α 留在 Bridge**：补运动实体，家庭 App 能推「检测到移动」，仍然没有时间轴。
- **β 旁路 Scrypted HKSV**：本集成只保证 H.264（最好加网络可读 RTSP），时间轴/人脸/区域全部交给中枢。

---

## 6. 建议落地顺序

1. 家里 HA 配对家庭 App，确认 720p 实时预览（本 PR 已覆盖服务侧）。
2. 逆向告警接口 → `binary_sensor` 运动 → HomeKit 通知（α，见 HOMEKIT_UNSUPPORTED.md §1）。
3. 若需要家庭 App 时间轴/人脸/活动区域：H.264 出 RTSP/HTTP，进 Scrypted 开 HKSV（β）。同一摄像头不要两套 HomeKit 出口。
4. 云端活动区域 API、PTZ、对讲只作为 HA 能力，按需再做。