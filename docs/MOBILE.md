# iPhone 与 Mac 协作

手机是 Mac 群聊的触屏客户端。模型仍在 Mac 上运行，使用原来的登录、持续会话和项目权限；无须在手机再次登录模型账号。手机选择群聊不会切换 Mac 当前页面。

## 连接与安装

1. Mac 和 iPhone 安装 [Tailscale](https://tailscale.com/download)，登录同一账号并连接。无须处于同一 Wi-Fi。
2. 首次在 Mac 启用 [Tailscale HTTPS / Serve](https://tailscale.com/docs/features/tailscale-serve)。可在终端运行 `tailscale serve --bg --https=8443 http://127.0.0.1:8765`，按官方链接启用 HTTPS；不要启用 Funnel。macOS 独立版的 CLI 也位于 `/Applications/Tailscale.app/Contents/MacOS/Tailscale`。
3. Mac 群聊右上角选择「连接手机」→「开启连接」。客户端验证登录、证书域名、私有代理和端口配置后才会开启。
4. 用 iPhone Safari 打开窗口显示的完整 `https://…ts.net:8443/` 地址，输入 Mac 的 8 位配对码。码有效期 5 分钟，仅可使用一次。
5. Safari 中选择「分享 → 添加到主屏幕 → 添加」。若主屏幕入口再次要求配对，刷新配对码即可。

Mac 必须保持开机、唤醒并运行群聊。每次重开桌面客户端，都需要再点「开启连接」。已配对设备可保留 30 天；关闭连接不删除聊天记录。升级前的 HTTP 地址和配对均失效，请删除旧主屏幕入口，用新的 HTTPS 地址重新添加。旧 HTTP 网站存过草稿的话，请在 Safari 的网站数据设置中清除旧地址的数据；新 HTTPS 页面无法清除另一个源的数据。

HTTPS 证书的设备域名会进入公共证书透明度日志；使用不含个人信息的设备名。域名公开不代表服务或聊天公开。[Tailscale HTTPS 说明](https://tailscale.com/kb/1153/enabling-https)

## 手机可做什么

- 查看近期聊天及新回复、公开进展；前台约每 2 秒同步一次。
- 按项目选择或新建群聊；手机和 Mac 可以同时查看不同群聊。
- 向自动路由、指定成员或全体发送消息，切换普通/协同模式，停止指定成员。
- 发送图片，查看来自当前项目或当前群聊附件目录的 PNG、JPEG、GIF、WebP 图片。
- 查看当前模型、额度及忙闲状态。型号设置、改名、归档、失败请求重新递送和本地文件打开仍在 Mac 完成。

手机输入框回车换行，点击发送按钮发送；外接键盘也可用 ⌘Enter / Ctrl+Enter。每个群聊的草稿、接收者与未确认的发送编号仅保留在当前页面内存中，不写入 localStorage、sessionStorage 或离线缓存。刷新或关闭页面会丢失草稿。页面仍打开时，断线会保留草稿；恢复网络只同步记录，不会自动发送草稿。未确认的同一条消息可以再次点击发送，服务器会按请求编号去重。请求编号超过一天，或 Mac 重启前未确认入队的消息，需要先检查聊天记录，再决定是否作为新消息发送；这不是模型执行的 exactly-once 保证。

首次打开加载最近 300 条公开消息，随后持续追加。更早的完整记录可在 Mac 查看。手机不自动读取 Codex 桌面聊天；它读的是群聊自己的记录。项目文件由 Mac 上的模型按照现有权限访问。

## 连接范围

服务默认关闭，Python 后端只绑定 `127.0.0.1:8765`，普通局域网和公网设备无法直接连接。手机通过 Tailscale 私有网络和 HTTPS 8443 访问，Tailscale 使用 WireGuard 加密传输，包括经中继转发的流量。[加密说明](https://tailscale.com/docs/concepts/tailscale-encryption)

客户端启动时拒绝任何已开启的 Funnel 配置，不覆盖其他应用占用的 HTTPS 8443。所有网页和 API 请求都必须来自本机代理，带有 Serve 验证过的当前 Mac 所属账号身份、匹配的 Host，以及 HTTPS 转发标记；公网 Funnel 请求和其他账号均被拒绝。即使未来有人把代理切成 Funnel，其请求也无法通过身份检查。`Origin` 校验阻止跨站写入。Mac 本机进程属于已有的信任边界，不防御已被攻陷的 Mac 或恶意本机程序。[Serve 身份头说明](https://tailscale.com/docs/features/tailscale-serve)

配对后还需独立的 256 位随机令牌，通过 `__Host-` 前缀、Secure / HttpOnly / SameSite=Strict cookie 保存；Mac 只保存令牌哈希和所属账号，文件权限为 0600。配对码有尝试频率限制。旧 HTTP 令牌不迁移，切换 Tailscale 账号会撤销已有配对。桌面「撤销所有配对」使现有令牌失效；手机「断开本机配对」撤销该浏览器并清理页面内的聊天、草稿和项目列表。检测到授权失效时也清理页面；网络完全断开时，浏览器无法立即得知撤销，已显示内容会保留到再次连接或页面关闭。

配对设备能操作所有可用群聊，权限相当于使用这套桌面客户端。请仅配对自己的设备；退出后不会撤回已经送进模型队列的任务。此版本不提供公网共享、离线运行或后台通知，也不注册 service worker。手机授权和去重状态位于客户端状态目录的 `mobile-devices.json`、`mobile-receipts.json`，不应提交到仓库。代码不包含实际机器地址、账号或凭证。

## 排查

- 网页打不开：确认 Mac 没有睡眠，两端 Tailscale 都已连接且是同一账号，手机连接已开启，并使用窗口显示的 HTTPS 地址。不要给 Python 开放局域网入站、做端口转发或切回 HTTP。
- 提示 Funnel 或 8443 冲突：先检查自己的 Tailscale Serve 配置，关闭公网 Funnel 或将其他服务迁到别的端口。客户端不会覆盖其他服务。8765 被占用时，请检查是否运行了两份群聊。
- 返回配对页：令牌可能被撤销、过期，或 Safari 与主屏幕入口使用不同存储；刷新配对码后重新连接。
- 发送结果不确定：先看聊天是否已出现该消息，再决定重试；不要用不同内容或新请求编号盲目重复发送。

## 开发验证

在 `app2/` 下运行：

```sh
../.venv/bin/python -m unittest qa.test_coordinator qa.test_gemini_member qa.test_routing_session qa.test_macos qa.test_mobile qa.test_mobile_transport
node qa/test_display.cjs
node qa/test_paths.js
node qa/test_mobile_ui.cjs
```

手机测试使用临时状态目录、真实本地 HTTP 和模拟模型，不调用真实模型。运行环境必须允许监听本地测试端口。手机网页资源按请求读取；修改后刷新 Safari 即可。Python 服务或桌面界面修改后需要在模型空闲时退出并重开客户端。
