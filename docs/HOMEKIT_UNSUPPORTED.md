# HomeKit 不支持功能：可行性与大致方案

本文只讨论 **家庭 App 里看起来缺的能力**。Home Assistant 本地直播 / MP4 已经可用，见 [TEST_RESULTS.md](TEST_RESULTS.md)。

家庭 App 摄像头其实是两套协议叠在一起：

| 栈 | 谁实现 | 家庭 App 里能做什么 |
| --- | --- | --- |
| **HAP 实时预览**（RTP + 快照） | HA HomeKit Bridge 已做 | 点开看画面、听声音、Siri「显示摄像头」 |
| **HAP 运动/门铃事件** | Bridge 支持接线，本集成还没给出实体 | 锁屏/通知中心「检测到移动」；**不需要** iCloud、不需要 HKSV |
| **HKSV（Secure Video）** | Bridge **明确不做** | 时间轴、iCloud 片段、人脸/宠物/车辆、活动区域、包裹 |

本集成当前只保证第一层：云端 HEVC → 本地 H.264 HLS，给 Bridge 一条能 `copy` 的流。下面每一项都按「家庭 App 要的到底是哪一层」来拆。

结构化摘要：[artifacts/homekit-unsupported-feasibility.json](artifacts/homekit-unsupported-feasibility.json)。

---

## 0. 先选路，再谈功能

不支持的功能几乎都落在两条路上，不要混着做。

```text
路线 α（留在 HA HomeKit Bridge）
  本集成补实体 → Bridge 用 linked_* 接线
  能做：移动通知、（勉强）门铃事件、HA 里的云台/开关
  做不成：时间轴、人脸、活动区域、iCloud 回放

路线 β（旁路 Scrypted / go2rtc HomeKit，开 HKSV）
  本集成只负责稳定 H.264（最好再吐 RTSP）
  Scrypted 对家庭中枢做 HAP Recording
  能做：时间轴、人脸、活动区域、包裹；移动通知由 HKSV 自己出
  前提：家庭中枢 + iCloud+（50GB 起）+ 苹果设备在家用局域网
```

HKSV 在 HAP 里不是「多一个录像开关」，而是整套新服务：`CameraRecordingManagement`、`CameraOperatingMode`、`DataStreamTransportManagement`，用 HomeKit Data Stream 把加密 fMP4 片段推给中枢。HA 核心文档写明摄像头 **HomeKit Secure Video is not supported**。在本仓库里重做这套，等于再维护一个 Scrypted 子集。

**建议**：α 做运动实体（家庭通知）；要时间轴/人脸/区域走 β。不要在 `hass_hjq` 里实现 HAP Recording。

---

## 1. 动作感应 / 移动通知

### 家庭 App 实际要什么

只要摄像头配件上挂一个 HAP **Motion Sensor**。HA Bridge 的写法是 `linked_motion_sensor`，实体类型 `binary_sensor` 或 `event`。运动变 on 时，家庭 App 推「检测到移动」；点通知仍是进实时预览，**不会**因此出现活动历史。

这是路线 α，**不依赖 HKSV**。

### 现状

| 层 | 情况 |
| --- | --- |
| 摄像机 / 云端 | `functionId=9` 移动侦测 **已开**；列表字段 `unread_alarm_count`（实测为 0，说明计数存在） |
| 本集成 | 没有 `binary_sensor`，没有告警 API |
| HA HomeKit | 支持接线，没东西可接 |

### 可行性：高（本仓库下一步最值）

云端已经在侦测。缺的是把告警变成 HA 状态。不要在 HA 里对 2304×1296 HEVC 做本地运动检测：两路实时解码 CPU 高，和机内侦测重复，合家亲共享账号还不一定拿得到子码流。

### 方案

**1. 抓包找出警通道（必须先做）**

对手机 移动爱家做 HTTPS 解密（mitmproxy / Charles），触发一次移动侦测，按现有签名方式（`get_video_sign`：排序参数 + path + secret）过滤：

- 主机：`video.komect.com` 或设备 `baseUrl`（形如 `access*.region.video.komect.com`）
- 路径关键词：`alarm`、`message`、`notify`、`event`、`unread`
- 推送：WebSocket / MQTT / 长轮询（App 若秒级出告警，优先这条）

合家亲 `shareType=1` 要单独看：告警可能只给主人账号。若共享账号拉列表 403 / 空数组，运动实体只能在主人账号上做，或接受轮询不到。

**2. 集成内落地（中等工作量）**

1. `HJQApi` 增加告警查询；没有列表接口时，退化为比较 `queryList` 的 `unread_alarm_count`。
2. Coordinator 每 5–15s 拉一次（有推送则改为事件驱动）。
3. 每路 `binary_sensor.*_motion`，`device_class=motion`；on 后 30–60s 无新告警则 off（HomeKit 需要边沿，不是一直 on）。
4. 可选：`binary_sensor.*_sound` 对应 `functionId=36`（家庭 App 摄像头配件**没有**标准「声音侦测」特性，这条主要给 HA 自动化用）。
5. README 写明 Bridge YAML：同一配件里 `include` 运动实体，并设 `linked_motion_sensor`。

```yaml
homekit:
  - name: B33-2
    mode: accessory
    port: 21064
    filter:
      include_entities:
        - camera.shi_dong_she_xiang_tou_b33_2
        - binary_sensor.shi_dong_she_xiang_tou_b33_2_motion
    entity_config:
      camera.shi_dong_she_xiang_tou_b33_2:
        video_codec: copy
        support_audio: true
        linked_motion_sensor: binary_sensor.shi_dong_she_xiang_tou_b33_2_motion
```

**3. 和录像的关系**

有了运动实体之后，HA 自动化可以在 on 时打开 `switch.*_recording` 或调 `camera.record`。这是 **HA 本地 MP4**，不是家庭 App 时间轴。

### 风险

- 无官方文档；共享账号权限可能不足。
- 只靠 `unread_alarm_count` 分不清移动 / 声音 / 哭声，且已读后计数清零会丢边沿。
- 轮询 5s 对家庭通知够用，对 HKSV「事件前预录」不够（那是路线 β 的事）。

### 明确不做

本地 ffmpeg `select='gt(scene,0.02)'` 当主方案；也不要把运动做成 HKSV 的替代品。

---

## 2. 活动区域

家庭 App「活动区域」和 App 里「区域设置」不是同一件事。

| | 云端「区域设置」(`functionId=49`，已开) | 家庭 App 活动区域 |
| --- | --- | --- |
| 谁画框 | 移动爱家 App | 家庭 App |
| 谁判断 | 摄像机 / 厂家云 | **家庭中枢**（HKSV） |
| 本集成 | 未接读写 API | 接了也没用，Bridge 没有 HKSV |
| 家庭 App 能否看见 | 不能 | 仅 HKSV 配件 |

### 路线 A：只把云端区域接到 HA（中等，家庭 App 无感）

**目标**：HA 里看/改厂家区域，让云端按框报警，再配合 §1 的运动实体。

步骤：

1. 抓包区域 GET/SET（常见是多边形或格子掩码 + `macId`）。
2. HA：只读可用 `image` 叠框；可写可用 `switch` 按预设区域，或自定义 Lovelace 画框（工作量明显更大）。
3. 不导出到 HomeKit。家庭 App 没有「第三方区域」特性。

适合：HA 自动化要「只对门口报警」，且愿意继续在移动爱家 App 里画框。

### 路线 B：家庭 App 里画区域（依赖 HKSV）

做完路线 β 之后，区域是苹果 UI，中枢按框过滤人物/运动。**本集成不用实现画框**。输入仍是整帧 H.264；框不在摄像机上。

和路线 A 同时开可以，但两套框互不同步：App 框过滤云端告警，家庭 App 框过滤 HKSV 片段。

### 可行性

- A：中等，纯逆向 + HA 实体。
- B：可行，但工作在 Scrypted，不在本仓库。
- 在 Bridge 里自绘区域：**不可行**（没有对应 HAP）。

---

## 3. 人脸识别

家庭 App「认识的人」绑定 HKSV：分析在 HomePod / Apple TV / 作为中枢的 iPad，人物库在 iCloud，UI 只在家庭 App。HA Bridge 没有 Recording 服务，**接任何 `image_processing` 都不会变成苹果人脸**。

B33 的 `functionConfig` **没有**人脸 / 人物项，云端这条路目前也不存在。

| 做法 | 家庭 App 认人 | 工作量 | 建议 |
| --- | --- | --- | --- |
| 路线 β：Scrypted HKSV | 能 | 部署配置 | **要认人就走这条** |
| 本集成做人脸模型 | 不能 | 很大 | 不做 |
| Frigate / CodeProject.AI | 不能（只在 HA） | 大，且要双路 2304p HEVC | 仅当你要 HA 通知、不要苹果人物库 |
| 接厂家人脸 API | 不能 | 当前设备无此功能 | 不做 |

HKSV 人脸也不经过我们：中枢自己解码配件推上去的片段。本集成责任仍是稳定 H.264（β 还要事件触发，见 §4）。

**不要**在 `hass_hjq` 里做人脸 embedding、人物库或「HomeKit 人脸桥」。

---

## 4. 家庭 App 录制 / 时间轴（HKSV）

### 家庭 App 实际要什么

不是「配件自己写 MP4」，而是：

1. 用户有家庭中枢和 iCloud+。
2. 配件实现 Recording 相关 HAP 服务。
3. 运动/门铃触发后，配件通过 Data Stream 把**加密**的 fMP4（常见 HEVC + Opus，或实现里转的 H.264 变体）推给中枢。
4. 中枢分析、上传 iCloud；家庭 App 时间轴只读中枢结果。

HA 官方：摄像头配件 **不支持 HKSV**。本集成的录制开关 / `camera.record` 只写 `/config/www/hass_hjq/`，家庭 App 看不到。

### 路线 β（推荐）：Scrypted 吃本集成的 H.264

几乎不用改 HAP，要补的是 **Scrypted 怎么拿到流**。现在转码结果在容器内 `/tmp/hass_hjq/{mac}/index.m3u8`，Scrypted 默认够不到。

落地顺序：

1. 本集成保持 Baseline 720p / 1500k / GOP 30 / 无 B 帧（已经按 HomeKit 直播来）。
2. 增加一条**可关**的网络出口，三选一：
   - 把 HLS 拷到 `www` 或本地 HTTP（Scrypted HLS input，最简单、延迟略差）；
   - 用 go2rtc / mediamtx 再封装 `rtsp://ha:8554/<camera>`（HKSV 常用）；
   - 集成内可选 RTSP publish（多一个 ffmpeg，CPU 更高）。
3. Scrypted：该 RTSP/HLS → HomeKit 插件 → 独立配件模式 → 家庭 App 配对 → 打开「允许录制」。
4. **不要**同一路摄像头既走 HA Bridge 又走 Scrypted HomeKit，会两套 HAP、两个配对码。选一个出口：Bridge 只直播，或 Scrypted 直播+HKSV。
5. 运动：Scrypted 可自带检测；若 §1 已有 `binary_sensor`，也可当 Scrypted 的 motion sensor，减少双路解码。

工作量主要在部署和选「谁当 HomeKit 出口」，不是改云端 API。

### 路线 γ：本集成内实现 HAP Recording

要自建 HAP 服务器（或深改 HA `homekit`），实现 Recording + OperatingMode + DataStream，按中枢 SDP 出加密片段，处理密钥、一条流只能一路录、断开恢复。还要和家庭中枢真机联调。

工作量：很大。收益：少装一个 Scrypted。维护：HA 升级、苹果协议改动都要跟。**不建议进本仓库核心。**

### 给 HA 核心提 HKSV PR

社区多年没做。不确定、不挡直播。即便上游做了，本集成仍只需给 H.264 + motion。

### 和厂家「云存储 / 录像直存」的关系

`functionId=7` 云存储、`functionId=29` 录像直存是厂家通道，和 iCloud 时间轴无关。接到 HA 也只是「另一路 MP4/云回放」，家庭 App 不会出现活动历史。

---

## 5. 门铃 / 语音对讲

B33 是 `functionId=14` **语音对讲**，不是门铃按钮。家庭 App 门铃 = HAP Doorbell 服务上的按铃事件（HA：`linked_doorbell_sensor`）。对讲 = 另一条双向语音。

| 能力 | 家庭 App | 本集成 | 可行性 |
| --- | --- | --- | --- |
| 按铃通知 | `linked_doorbell_sensor` | 无门铃实体，设备也无门铃 | 低（硬件不是门铃） |
| 听摄像头声音 | Bridge `support_audio: true`，把 AAC 再转 AAC-ELD/Opus | 转码已带 AAC | **高**，属直播，家里配对后即可验 |
| 家庭 App 里对讲 | 配件要有 Microphone + Speaker，并且双向 RTP | HA Bridge 摄像头以单向听为主 | **低**：既要 Bridge 双向音频，又要把 RTP 接到厂家对讲通道 |
| HA 里对讲 | `button` 播放预置语音，或将来 media 流 | 未接对讲 API | 中，且 HomeKit 无感 |

厂家对讲通常是独立信令（WebSocket/RTP），不是直播 MPEG-TS 里的第二条音轨。要对讲，得抓包 `baseUrl` 上的 talk/voice 接口，再在 HA 做 `button`/`tts`。那是 HA 功能，不是家庭 App 对讲。

**不要**用运动实体假冒门铃（`linked_doorbell_sensor` 会出「有人按门铃」）。

---

## 6. 云台

`functionId=1` 云台控制已开，`ptz_support=true`。HAP **没有**消费级 IP 摄像头标准 PTZ 服务，家庭 App 摄像头页也没有方向键。

| 目标 | 可行性 | 方案 |
| --- | --- | --- |
| HA 里控制云台 | 中 | 抓包 PTZ（方向/停止/预置位）→ `button`（上下左右停）或 `number` 角度；可选 `camera` 服务 |
| 家庭 App 里摇杆 | 低 / 无标准 | 只能暴露一堆 Switch，体验差，不建议 |
| 移动追踪 `functionId=13` | 中（HA） | 多半是云端开关，做成 `switch`；HomeKit 无对应特性 |

云台应留在 HA（仪表盘、自动化）。HomeKit 出口继续只放 `camera` + 将来的 motion。

---

## 7. 声音侦测 / 哭声侦测

`functionId=36` 声音侦测已开；`functionId=4` 哭声侦测为 off。家庭 App 摄像头配件没有标准「声音侦测」特性。

方案与 §1 相同：告警类型字段若能区分，就多一个 `binary_sensor`（`device_class=sound`）。给 HA 自动化用；不要 `linked_motion_sensor` 混用（家庭 App 会显示成移动）。HKSV 的声音事件同样是路线 β 里中枢做的，不是这个传感器。

---

## 8. 本集成为路线 β 需要预留什么

即使 HKSV 不写在本仓库，下面几项能明显降低以后接 Scrypted 的成本：

1. **稳定 H.264**：已做（Baseline 3.1、无 B 帧、1s HLS）。
2. **网络可读的流地址**：尚未做。优先可选 RTSP 或把 HLS 放到可访问 HTTP。
3. **运动边沿**：§1。HKSV 用它当 event trigger，比 Scrypted 再解码一次更省。
4. **不要把同一 camera 实体同时给 Bridge 和 Scrypted HomeKit**。文档里写清二选一。

---

## 9. 建议落地顺序（含工作量）

| 顺序 | 事项 | 路线 | 工作量 | HomeKit 观感 |
| --- | --- | --- | --- | --- |
| 1 | 家里局域网配对，确认 720p 预览 + 音频 | 已有 | 配置 | 能看、能听 |
| 2 | 抓包告警 → `binary_sensor` 运动 → `linked_motion_sensor` | α | 中 | 移动通知 |
| 3 | （可选）告警类型 → 声音实体；PTZ `button` | α | 中 | HomeKit 无感，HA 有用 |
| 4 | 需要时间轴/人脸/区域：H.264 出 RTSP/HTTP → Scrypted HKSV | β | 部署为主 | 家庭 App 完整摄像头 |
| 5 | 云端区域 API | A | 中 | 仅 HA |
| — | 本仓库 HAP Recording、人脸模型、假门铃 | — | 很大 / 不值 | 不做 |

判定：只想家庭 App 推一下「客厅动了」→ 做 §1。想要和原装 HomeKit 摄像头一样的时间轴和认人 → 不要扩 Bridge，走 §4 路线 β。
