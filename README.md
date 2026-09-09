# 移动爱家（原和家亲）Home Assistant 集成

在已归档的 [XiaoMiku01/hass-hjq](https://github.com/XiaoMiku01/hass-hjq) 基础上继续维护，目标是让云端摄像头能在 **Home Assistant 实时预览 / 本地录制**，并在 **HomeKit（家庭 App）里实时预览**。

本仓库摄像头没有本地 RTSP/ONVIF，只有云端直播地址（HLS / HTTP-FLV，常见为 H.265）。HomeKit 只接受 H.264，因此默认会用 ffmpeg 转成本地 H.264 HLS。

## 已支持

- [x] 手机号 + 密码登录（token 过期自动重新登录）
- [x] 账号下摄像头接入（含合家亲共享到本账号的设备，只要设备列表接口能返回）
- [x] Home Assistant 实时视频流
- [x] Home Assistant 本地分段录制（每路摄像头一个「录制」开关）
- [x] Home Assistant `camera.record` 短片录制
- [x] HomeKit 实时预览（需开启 H.264 转码，默认开启）
- [ ] HomeKit Secure Video（iCloud 录像）：Home Assistant 的 HomeKit Bridge **不支持** HKSV
- [ ] HomeKit 人脸识别 / 活动区域（依赖 HKSV）
- [ ] 智能插座等其它设备
- [ ] 摄像头事件 / 移动侦测上报（云端已开侦测，HA 尚未做成 `binary_sensor`）

## 安装

1. 把 `custom_components/hass_hjq/` 放到 Home Assistant 配置目录的 `custom_components/hass_hjq/`。
2. 重启 Home Assistant。
3. 设置 → 设备与服务 → 添加集成 → 搜索「移动爱家（原和家亲）」，用手机号和密码登录。
4. 账号下的摄像头会作为独立设备出现。每台设备包含：
   - `camera.*` 实时画面
   - `switch.*_recording` 本地录制开关

HACS 用户可将本仓库添加为自定义存储库（类别：Integration）。

## Home Assistant 实时预览与录制

打开摄像头实体即可看直播。云端地址会过期，集成会定时保活；若画面卡住，可调用服务 `hass_hjq.refresh_stream`。

### 持续录制

打开对应摄像头的 **录制** 开关。录像按选项里的分段时长写成 MP4，默认目录：

`/config/www/hass_hjq/`

可通过 `https://你的HA/local/hass_hjq/` 下载。建议同时在「媒体」中查看该目录。

### 按需短片

```yaml
service: camera.record
target:
  entity_id: camera.客厅
data:
  filename: /config/www/hass_hjq/clip.mp4
  duration: 30
```

## HomeKit 实时预览

1. 集成选项中保持 **「转码为 H.264」** 开启（默认已开）。两路 720p 转码在 x86 NAS / NUC 上一般可接受，树莓派可能吃力，可把分辨率设为 `1280:720`。
2. 设置 → 设备与服务 → 添加 **HomeKit Bridge**。
3. 选择配件模式（Accessory），每台摄像头单独一个配件，配对更稳定。
4. 包含对应的 `camera.*` 实体。
5. 若已开启本集成的 H.264 转码，在 HomeKit 选项里把这些摄像头勾选为 **原生 H.264**（`video_codec: copy`），避免二次转码。
6. 用家庭 App 扫描配对码。点开摄像头应能看到实时画面。

不要把未转码的 H.265 云端流勾成「原生 H.264」，否则家庭 App 会一直转圈。

### 关于 HomeKit「录制」

Apple 家庭 App 里的连续录像是 **HomeKit Secure Video**，依赖家庭中枢 + iCloud+。Home Assistant 官方 HomeKit Bridge **不能** 提供 HKSV。

本集成提供的录制是 Home Assistant 本地 MP4（上面的录制开关 / `camera.record`）。家庭 App 可以实时看，回放请在 Home Assistant 媒体目录里看。

如果一定要 HKSV（家庭 App 时间轴、人脸、活动区域），需要另外用 Scrypted 等方案，并把本集成转码后的 H.264 流作为输入。各不支持项的可行性、抓包步骤和推荐/不推荐做法见 [docs/HOMEKIT_UNSUPPORTED.md](docs/HOMEKIT_UNSUPPORTED.md)。

## 集成选项

设置 → 设备与服务 → 移动爱家 → 配置：

| 选项 | 说明 |
| --- | --- |
| 转码为 H.264 | HomeKit 实时预览需要。关闭后 HA 仍可能播放 H.265，但家庭 App 通常不行 |
| 转码分辨率 | `1280:720`（默认）、`1920:1080`、`source` |
| 转码码率 | `800k` / `1500k`（默认）/ `2500k` |
| 包含音频 | 转码时是否带 AAC 音频（16 kHz / 单声道 / 64k） |
| 录像分段时长 | 持续录制每个 MP4 的秒数，30–3600，默认 300 |

编码规格、可配项、HomeKit 能力对照与实测产物见 [docs/TEST_RESULTS.md](docs/TEST_RESULTS.md)。选项是账号级，两路摄像头共用。

## 实测设备

使用真实移动爱家账号在 Home Assistant 中验证过：

| 型号 | 接入方式 | 云端编码 | 说明 |
| --- | --- | --- | --- |
| 视洞 B33（`IPC_SD_B33_1`） | 合家亲共享 | HEVC 2304×1296 @ ~15fps + AAC，MPEG-TS | 无 HLS，只有带签名的 HTTP 直播地址；会话约 60 秒需保活。默认转码为 1280×720 H.264 |

同一账号下两路 B33 均可：HA 实时预览、`camera.record` 短片、录制开关分段 MP4。默认输出 **H.264 Constrained Baseline 1280×720 @ 1500k + AAC**。HomeKit Bridge 能在配件模式拉起摄像头配件并广播 `_hap._tcp`；家庭 App 配对必须在与 HA **同一局域网** 的苹果设备上完成。人脸识别、家庭 App 活动区域、HKSV 时间轴本集成不支持，分析见测试文档。

## 已知限制

1. 只有云端流，没有发现通用的本地 RTSP/ONVIF。
2. 原作者停更的原因仍然存在：部分厂家摄像头的设备列表 / 拉流接口不同，登录后可能没有实体。
3. App 密码登录可能触发风控；手机 App 与本集成抢同一会话时可能互踢，掉线后会自动重登。
4. H.264 转码会占用 CPU。两路 2304×1296 源请优先用默认 720p。
5. 预览图在转码进程刚重启的几秒内可能失败，随后会恢复。

## 免责声明（Disclaimer）

本项目的代码及相关文档是出于开源社区贡献的目的开发和发布，旨在为 [Home Assistant](https://github.com/home-assistant) 提供兼容性支持和功能扩展。作者不对任何个人或组织使用本代码所造成的任何直接或间接后果承担责任。该代码及其衍生产品仅限于合法用途，用户需自行确保其在使用本项目时遵守相关法律法规，包括但不限于知识产权和逆向工程相关的法律条款。

本项目代码基于 [Apache License 2.0](./LICENSE) 许可证发布，用户可以根据该许可证的条款自由使用、修改和分发本项目代码，但需保留原始的版权声明。

**免责声明要点**：

1. 本代码和项目仅出于学习、研究和开源社区贡献的目的，作者不保证代码的准确性、完整性和可用性。
2. 用户需自行承担使用本代码的法律责任，并自行确保其在使用该代码时未侵犯任何第三方的权利。
3. 本代码未与任何官方产品或服务相关联，亦不代表任何第三方利益。
4. 作者不对任何因使用本项目代码而导致的损害、数据丢失或其他任何损失承担责任。
5. **如果您认为本项目中的任何部分侵犯了您的合法权益，请立即通过原项目说明中的联系方式与作者联系。**

使用本代码即表示您同意以上免责声明内容。
