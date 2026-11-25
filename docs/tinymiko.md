# tinymiko 使用说明

tinymiko 管理一个 SSH 交互 shell 的生命周期、提示符、分页及命令归属。需要 Python 3.11+，通过 uv 安装项目依赖：

```powershell
uv sync
```

本项目独立打包 `tinymiko` 核心，运行依赖为 AsyncSSH，不包含原 Excel 采集器。尚未发布至 PyPI。

## 建立会话与识别

### 校验设备身份

known_hosts 用于校验远端设备的 SSH 主机公钥，确认连接的是预期设备；文件保存可信主机公钥，不保存登录密码。正式环境建议使用此方式。

```python
from tinymiko import HUAWEI, Session

options = {
    "host": "192.0.2.10",
    "username": "admin",
    "known_hosts": "known_hosts",
}

async with Session.connect(options, profile=HUAWEI) as session:
    print(session.identity.vendor, session.identity.platform)
    print(session.prompt, session.permission, session.configuration_mode)
```

options 接受参数字典或 AsyncSSH 的 SSHClientConnectionOptions。认证、主机密钥和加密参数直接交给 AsyncSSH；不自动关闭主机密钥校验或启用历史弱算法。

示例中的 `"known_hosts": "known_hosts"` 指定当前工作目录下名为 known_hosts 的文件，使用时应替换为实际路径，并事先通过可信渠道核对并配置设备主机公钥。主机公钥未知或不匹配时校验失败，不自动接受新密钥。也可以省略 known_hosts 字段，使用默认的用户目录 `.ssh/known_hosts` 文件。

### 跳过设备身份校验

测试环境中如需跳过主机密钥校验，可显式设置 known_hosts=None：

```python
from tinymiko import HUAWEI, Session

options = {
    "host": "192.0.2.10",
    "username": "admin",
    "password": "...",
    "known_hosts": None,
}

async with Session.connect(options, profile=HUAWEI) as session:
    print(session.prompt)
```

此方式仍使用 SSH 加密通信和用户认证，但不校验对端主机公钥，无法可靠确认设备身份，存在中间人攻击风险。省略 known_hosts 字段与显式设置为 None 的含义不同：前者保留默认校验，后者关闭校验。两种方式均可按需在 options 中添加 password 或 client_keys 等认证参数。

### 设备识别与交互配置

默认先用受限启动规则读取登录信息与初始 prompt，再识别设备并加载默认配置。内置 Huawei/Cisco profile，ZTE/Nokia 仅提供保守的厂商证据识别，需要显式配置交互规则。登录输出没有明确厂商证据时不会仅凭尖括号或井号猜测厂商；显式提供 profile 仍可建立身份未知的会话。

Identity 只包含 vendor 与 platform：

| 目标 | vendor | platform |
| --- | --- | --- |
| 网络设备 | 厂商，例如 huawei | 型号或已确认的产品系列，例如 S9300 |
| Linux | 发行版，例如 debian | 发行版版本，例如 12 |

例如 Identity(vendor="huawei", platform="S9300") 或 Identity(vendor="debian", platform="12")。没有可靠信息时字段为 None；网络设备软件版本不作为型号，Linux 内核版本不作为发行版版本。Arch 没有 VERSION_ID 时 platform=None，不填入推测的版本。Linux 发行版识别不代表内置了相应 shell 的默认 profile，连接时仍需由调用方提供匹配其 prompt 的 Profile。

此版将旧的 Identity.version 改为 Identity.platform，并改变其含义。自定义识别函数、显式 identity 参数及读取身份的调用代码均应使用新字段。

profile 是完整配置覆盖，可通过 dataclasses.replace 修改内置值。它包含 encoding、newline、echo、prompt_pattern、page_pattern、continue_key、quit_key、term_type、term_size。自定义 prompt 正则可使用 device、permission、mode 命名捕获组；权限和配置模式根据每次 prompt 独立提取，不维护配置上下文栈。设备名称默认保持不变；若操作需要改名，调用方应在命令前显式更新 session.prompt_info 中的设备匹配信息。

可用原生 Python 函数替换识别逻辑：

```python
from tinymiko import HUAWEI, Session, identify_login


async def identify(session):
    result = await session.execute_paged("display version")
    # 显式复用文本识别函数提取厂商和型号/系列，不把软件版本作为 platform。
    return identify_login(result.output, session.prompt)


async with Session.connect(options, profile=HUAWEI, identify=identify) as session:
    print(session.identity)
```

identify 可以是同步函数或异步函数，接收已取得启动边界的 session，返回 Identity。探测沿用同一会话，不递归启动识别。identity 参数可补充显式 vendor/platform，与识别结果矛盾时抛出 IdentityConflict。login_timeout（默认 30 秒）覆盖连接、认证、初始 prompt 和识别全过程。

Linux 自定义识别函数也可显式执行 cat /etc/os-release，再将结果交给 identify_login；该函数读取 ID 与 VERSION_ID，并支持 Debian、Ubuntu、Fedora、Arch 的有限登录横幅样式。不会主动执行上述命令，也不根据 prompt 猜测发行版或版本。

## 三类执行方式

### 简单执行

```python
await session.execute("screen-length 0 temporary")
result = await session.execute("display current-configuration", timeout=60)
```

execute 只等待终止 prompt，不处理分页，不自动回复询问。若远端仍停在分页或询问，最终超时并关闭会话。初始化命令由调用方明确执行。

### 自动与单步分页

```python
result = await session.execute_paged("display current-configuration", timeout=60)

async with session.command("display current-configuration", timeout=60) as command:
    event = await command.read_pages(expect=r"目标内容")
    if not event.done:
        # 此时命令仍在进行；可以继续 read_pages / next_page，或退出分页。
        result = await command.quit_paging()
    else:
        result = command.result

async with session.command("display current-configuration", timeout=60) as command:
    while True:
        event = await command.next_page()
        print(event.text, end="")
        if event.done:
            break
    result = command.result
```

自动分页仅在明确匹配分页提示后发送继续键（默认空格），不因静默盲发。read_pages 不带 expect 时读取至 prompt；带字符串或编译正则时读取至目标或 prompt，先到者返回。next_page 首次读取到分页点，之后每次续一页。quit_paging 必要时先读到分页点再发退出键，并等待终止 prompt。

命中目标或分页点不释放会话执行权。主动退出分页后 result.status 可以是 completed，但 output_complete 为假；这表示交互已结束，剩余页面未采集。

### 多轮交互

```python
async with session.command("save", timeout=180) as command:
    while True:
        event = await command.expect({
            "confirm": r"Continue\?\s*\[y/n\]:",
            "filename": r"File name:",
        })
        if event.done:
            break
        if event.kind == "page":
            await command.reply(" ", newline=False)
        elif event.name == "confirm":
            await command.reply("y", newline=True)
        elif event.name == "filename":
            await command.reply("backup.cfg", newline=True)
    result = command.result
```

正则只是示例，不代表所有设备的保存对话。Event 含 kind（match/page/prompt）、text、name、groups、fields 和 done。expect 不自动回答分页或询问；reply 必须显式指定 newline，敏感回复使用 sensitive=True。普通正文正则可以直接使用 re.compile，TextFSM 等解析器在调用方处理 event.text 或 command.output，不属于核心依赖。

## 输出、失败与并发

output 是连续正文，剔除已识别的发送回显、分页提示、对应擦除序列和终止 prompt，保留换行及正文缩进。stderr 独立保存在结果中，不参与 stdout 的提示符判定，不声称两个流具有远端精确全序。原始登录信息可从 login_output 查看；首版不提供命令原始终端记录的持久化接口。

命令内通过 command.output 查看已读取正文，正常完成后取得 command.result。CommandResult 含 command_id、command、output、stderr、prompt_before/after、elapsed_seconds、status、output_complete。completed 只代表取得结束 prompt，业务成功需由上层判断。

CommandTimeout、CommandError、OutputLimitExceeded 携带 result 部分结果。取消保持 asyncio.CancelledError；调用方解析异常保持原异常，可以在保存的 command 对象上访问 partial_result。超时、取消、断线或未完成上下文退出会关闭会话，不自动重试或重放。

同会话并发调用自动串行；嵌套同会话命令明确报错，包括从当前命令派生的子任务。命令 IO 只允许在持有上下文的任务内调用。总期限覆盖发送、所有读取/回复、分页及调用方异步处理，不在每次 expect 后重置；排队等待不计入命令期限，可用外层 asyncio.timeout 限制排队时间。

默认总输出及接收队列上限均为 8 MiB，每命令最多自动继续 10000 次，可在 connect 中通过 max_output、max_pages 调整。有界连接清理期限 cleanup_timeout 默认为 3 秒。关闭连接不能证明远端命令已停止。

## logging 与正文回调

connect 接受 logging.Logger 或 LoggerAdapter；默认使用 tinymiko 命名 logger，仅安装 NullHandler。库不配置 root logger，也不创建日志文件。应用可通过标准 handler 将记录转发到 Loguru 等系统。

INFO 记录会话与命令生命周期，DEBUG 记录交互事件；正文日志需显式启用 log_output=True，并将 logger 设置为 DEBUG。extra 包含 session_id、command_id、event_type，正文事件的 event_type 为 output。专用 handler 可按命令关联并原样追加正文消息，不自行插入换行，即可得到连续正文日志。

on_output 接受快速同步函数，在确认事件边界时收到新增正文；首版不保证每个网络 chunk 立即触发回调。日志及回调在发布前剔除已标记敏感回复的匹配回显，并对后续相同明文和跨事件前缀脱敏；不保证识别设备任意变形、编码或掩码后的秘密内容。日志与回调不自动包含 stderr 或原始终端字节。

## 验证范围

测试使用本地 AsyncSSH 服务及真实 PTY 通道，覆盖逐字节分包、多字节字符、ANSI/退格擦除、三类执行、分页退出、交互、敏感回显、总期限、取消、并发及失步关闭。当前终端整理支持行式 CLI 常见控制序列，不支持全屏编辑器、任意光标定位或终端图形应用。

已在一台 Huawei VRP 交换机上验证，版本为 VRP (R) software, Version 5.170 (S9300 V200R022C00SPC500)：

- display interface brief main 自动续页，匹配 GigabitEthernet1/0/10 后退出，以及单步翻页。
- screen-length 0 temp 后，以简单执行方式采集 display current-configuration interface，并重复读取确认一致。
- 接口配置自动分页与关闭分页后的正文逐行一致（保留缩进，仅比较时忽略设备原始输出中不同的行尾空格）。
- system-view / quit 的配置视图与用户视图识别，以及显式 display version 探测。该设备登录输出不足以识别厂商或型号，因此使用显式 Huawei profile；默认识别仍返回未知。新版文本识别规则可从该次探测输出提取 vendor="huawei"、platform="S9300"（系列），不再把 VRP 软件版本放入身份字段；此字段变更使用已观测文本验证，未再次连接设备。

实机发现的宽列分页擦除序列（ESC[42D、42 个空格、ESC[42D）已加入回归测试，分页标记的前导空白也归入内部分页事件，不进入正文。以上仅代表该设备和软件版本的测试结果，不承诺覆盖其他设备或所有版本。

可通过 scripts/test_tinymiko_live.py 对已授权的 Huawei 设备复测；主机和用户名从命令行提供，密码从 TINYMIKO_PASSWORD 环境变量读取。脚本只执行只读查询、当前会话的临时分页设置和不修改配置的视图切换，报告仅保留结果、长度、哈希及控制字符诊断，不保存凭证或配置正文。首次取得的主机公钥在本次运行内固定，属于 TOFU，不等同于通过独立可信渠道验证设备身份。
