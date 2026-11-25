# tinymiko：Windows 原生异步终端小核心设计

本文记录执行核心的设计约定，首版实现位于 src/tinymiko，具体接口与已验证范围见 [使用说明](tinymiko.md)。执行核心收缩为单个 SSH 交互会话的基础 IO、提示符提取、登录信息识别、分页处理、交互匹配和命令结果归属。设备身份解析、跨会话调度、业务命令及结果解析由上层混合执行引擎负责。

## 边界

AsyncSSH 负责 SSH 连接、认证、加密算法、通道和网络流控，连接参数直接使用或薄封装其选项。核心负责一个交互终端内可靠地发送、接收和划分命令边界；不实现 SSH 协议或另一套加密算法配置系统。

| 小核心提供 | 上层负责 |
| --- | --- |
| 一个连接及交互 shell 的生命周期 | 设备地址映射、跨批次队列、设备并发 |
| 提取完整 prompt 和设备标识 | 资产信息、业务节点及厂商能力目录 |
| 从登录输出与 prompt 提取 vendor/platform 身份 | 必要的主动探测及业务适配选择 |
| 流式输出、增量匹配及分页续读 | 命令生成、重试、回退、业务成功判断 |
| 正则等待、交互回复、命令结果记录 | Python 分支逻辑、TextFSM 模板及解析 |

核心不依赖工作流 ID、设备队列、TextFSM、插件框架或数据库。上层自行关联核心返回的 command_id 与任务/节点。Linux exec 可作为后续独立便利接口，不作为本轮交互终端核心的前置条件。

总体架构、运行时及插件文档中的设备队列、主动识别和更完整设备资料仍属于上层职责，不能据此扩大本核心的依赖或接口。

## 设备信息：vendor＋platform

识别结果只保留两个字段：`vendor`、`platform`，含义按目标体系区分：

| 目标 | vendor | platform |
| --- | --- | --- |
| 网络设备 | 厂商，如 huawei、cisco | 硬件型号或已确认的产品系列，如 S9300、C9300-48P |
| Linux | 发行版，如 debian、fedora、arch | 发行版版本，如 12、40；没有版本证据时为 None |

网络设备的软件版本不能代替硬件型号；只有系列信息时保留系列名称，不猜测具体型号。Linux 优先采用 os-release 文本中的 ID 与 VERSION_ID，不用 ID_LIKE 代替发行版，也不用内核版本或 BUILD_ID 代替发行版版本。两个字段均为可选字符串，不做跨体系版本比较，不增加独立的设备类别、型号或系统版本字段。

两个字段均允许未知：例如登录信息只有 Huawei VRP 软件版本而没有型号时，platform=None；Arch 没有发行版版本信息时也保留 None。默认识别函数只分析本次登录成功后的输出和 prompt，不自动执行 display/show 或 cat /etc/os-release 等探测命令。调用方可以用原生 Python 函数替换识别逻辑，包括在同一会话中执行明确的探测命令；识别所需命令和解析由该函数负责，核心不内置主动探测流程。

保留原始登录输出，便于使用者检查识别依据，但不再把证据、置信度等变成额外的设备分类字段。使用者可显式提供 vendor/platform 及交互配置；显式值与观测结果矛盾时报告冲突，不静默覆盖。

设备识别采用轻量规则函数，输入登录文本及 prompt，输出上述两个字段。Huawei、ZTE、Cisco、Nokia 等规则与通用收发实现分开，覆盖范围以真实样本为准。不能把任意一组尖括号或井号当作充分的厂商证据。

## Prompt 与交互配置

会话保存当前完整 prompt 以及从中提取的设备标识。设备标识用于便于阅读和辅助匹配，不作为跨地址唯一设备身份。

通过默认正则规则提取当前 prompt 所表示的权限类别和配置模式，例如用户模式、特权模式、config、config-if。每次根据当前 prompt 独立识别，不维护配置上下文栈，不推演此前进入过哪些层级。prompt 无法证明的数字权限等级返回未知；核心不自动提权或切换配置模式。

Prompt 规则需要支持同一会话内模式变化。匹配结合行边界、缓冲区末端、已知设备标识及允许的模式，避免仅见到 > 或 # 就结束。设备改名后如何更新由明确规则或使用者指定，不能无限泛化匹配。

轻量交互配置仅包含编码、行结束符、回显规则、prompt 提取与结束匹配、权限及配置模式提取、分页标记及继续/退出按键；提供已适配设备的默认配置，并允许调用方覆盖。分页继续键默认为空格，退出键由设备配置明确指定。配置本身不执行初始化命令，不包含路由、保存配置等业务能力，也不要求为每个硬件型号建设驱动类。

会话启动顺序为：

1. 建立 SSH shell，使用调用方指定或受限候选的启动规则，收集登录输出并取得初始 prompt。
2. 调用默认或自定义识别函数，取得 vendor 与 platform。
3. 加载已适配设备的默认交互配置，再应用调用方显式覆盖，校验当前 prompt。
4. 进入可执行状态，开始接收业务命令。

初始 prompt 也是需要匹配的终端文本，不是 SSH 协议直接提供的字段。自定义识别函数若执行命令，必须先具备足够的启动交互配置，并使用同一会话的底层执行机制；不能递归触发连接或设备识别。没有足够识别信息时允许部分未知，并可使用调用方提供的完整配置；若终端边界无法确认或缺少必要交互配置，则明确失败。

## 最小 API

采用一个 Session、一个命令上下文、一个结果对象。API 按使用方式分为三类，底层共用同一读取、文本整理、事件匹配、完成判定及结果记录实现，不各建一套状态机。

| 调用方式 | 分页行为 | 返回条件 |
| --- | --- | --- |
| 简单执行 `execute` | 不自动回复分页 | 取得终止 prompt |
| 自动/单步滚屏 `execute_paged`、`read_pages`、`next_page` | 匹配分页提示后发送继续键，可循环或单步 | 指定 expect、单步分页边界或终止 prompt |
| 交互执行 `command`、`expect`、`reply` | 由调用方控制，可显式调用滚屏方法 | 明确的匹配事件或终止 prompt |

### 简单执行与自动滚屏

以下是拟议接口，并非可运行代码：

```python
async with Session.connect(options, profile=profile) as session:
    print(session.identity.vendor, session.identity.platform)
    print(session.prompt)

    # 简单执行不处理分页，适合调用方已明确关闭分页的采集流程。
    await session.execute("screen-length 0 temporary", timeout=30)
    result = await session.execute("display version", timeout=30)

    # 即使设备仍启用分页，也可自动逐页读取至终止 prompt。
    result = await session.execute_paged("display current-configuration", timeout=60)
```

`execute` 只发送命令并等待终止 prompt，不自动关闭分页，也不自动发送空格。若实际停在分页提示，且调用方没有关闭分页，则最终超时，不能把已收到的部分输出当作完整成功。

`execute_paged` 等价于在 command 上下文中调用不带 expect 的 read_pages，循环处理分页直到取得终止 prompt；不自动回答 y/n 或字段输入等询问。

### 可控制滚屏

```python
async with session.command("display current-configuration", timeout=60) as command:
    # 循环处理分页，直到匹配目标；若 prompt 先出现，也会返回。
    event = await command.read_pages(expect=r"目标内容")
    if not event.done:
        print(command.output)  # 截至当前已读取的连续正文快照
        # 可根据 Python 逻辑继续自动读取、单步翻页或退出。
        result = await command.quit_paging()
    else:
        result = command.result

async with session.command("display current-configuration", timeout=60) as command:
    while True:
        # 首次读取至第一个边界，之后每次继续一页；最后一段正文也需展示。
        event = await command.next_page()
        print(event.text)
        if event.done:
            break
    result = command.result
```

- `read_pages(expect=None)`：没有 expect 时，循环处理分页直至终止 prompt；提供正则时，在匹配目标后暂停，返回目标事件。prompt 先出现则返回 done，表示命令已结束而目标未匹配，不再发送空格。
- `next_page()`：若当前已停在分页提示，发送一次继续键，再读至下一个分页提示或终止 prompt；若尚未停在分页提示，则先读取至该边界，不提前发送空格。
- `quit_paging()`：若当前已停在分页提示，发送配置的退出键；若因 expect 命中而停在页内，则先读至分页提示或终止 prompt，再决定是否发送退出键。退出键只在已确认的分页点发送，随后等待终止 prompt 并返回结果。
- `command.output`：累计已收到并整理过的正文，不消费事件，也不主动读取。命中 expect 时，网络上已同时到达的后续内容可以已经包含在快照内；不能承诺恰好停止在目标字符处。

命中 expect 或到达单步分页边界只结束本次读取，不代表顶层命令完成。上下文继续持有会话执行权，后续滚屏、退出和回复均归属于同一 command_id。调用方提前退出上下文且未取得终止 prompt 时，按未完成命令清理会话。

### 交互执行

```python
async with Session.connect(options, profile=profile) as session:
    async with session.command("save", timeout=180) as command:
        while True:
            event = await command.expect({
                "confirm": r"(?i)continue\?\s*\[y/n\]\s*:?\s*$",
                "filename": r"(?i)file name\s*:\s*$",
            })
            if event.done:
                break
            if event.kind == "page":
                # 本示例使用空格继续分页，然后恢复询问匹配。
                await command.reply(" ", newline=False)
                continue
            if event.name == "confirm":
                await command.reply("y", newline=True)
            elif event.name == "filename":
                await command.reply("backup.cfg", newline=True)

        result = command.result
```

示例正则仅说明机制，不能当作各厂商通用的 save 对话。使用者可根据返回文本、正则捕获字段或解析结果自由使用 Python if/else、循环及函数，不要求把分支转换成声明式交互图。

- `command`：整个上下文持有会话执行权，发起一个顶层命令；回复仍归属于这个命令。
- `expect`：在尚未消费的命令输出中等待一组具名正则、分页提示或终止 prompt，不自动回复。正则直接接受 Python 字符串或已编译正则对象；不设计额外的表达式语言。终止 prompt 和分页提示由核心统一检测，不要求每次重复传入。
- `reply`：写入交互回复，是否追加配置的行结束符显式指定；支持敏感回复标记。回复不会自动启动下一条顶层命令。
- `result`：命令确定结束后取得完整结果；超时/取消时异常携带部分结果。

读取事件包含 kind（match / page / prompt）、匹配名称、捕获字段、本段 text 和 done；done 仅在取得终止 prompt 时为真。read_pages 的目标事件使用 match，next_page 的分页事件使用 page。交互调用方收到 page 时，可调用 next_page 或 read_pages 处理；未识别的询问不会被自动回答。

同一会话一次只允许一个命令上下文。并发调用在该会话内串行，嵌套执行应明确报错，不能挂起等待自己。使用者的 Python 分支和解析位于命令上下文内，期间其他命令不能插入。

expect、read_pages 和 next_page 共用消费位置，保留匹配之后到达的数据，防止重复命中旧问题或丢失紧随问题后的输出。匹配事件按最早位置处理，多个用户模式同位置按声明顺序；识别到与结束 prompt 重叠的用户规则视为冲突，不静默猜测。分页提示由内部识别，重叠的用户规则不能再次回复同一个分页事件；自动滚屏也不能越过更早的目标事件发送继续键。一次分页提示只处理一次，分页键回显和擦除不能使旧分页点再次触发。

同一次 expect 可读取多批数据，直到有匹配、命令结束或总期限到达。禁止将一次 read 或一段静默当作完整事件。使用者误将普通正文匹配为询问时可能发送错误回复，因此模式应尽可能限定提示上下文，核心不声称能够理解任意自然语言询问。

如需实时展示，可提供同步且快速的正文追加回调，或由上层包装为有界事件流；公开回调使用与 output 相同的整理规则，不能先输出分页提示再要求观察者自行删除。观察者不能读取通道或绕开 command 的回复/分页方法写入。首版不必为了展示增加后台 reader 和另一套控制接口。

## 正则与 TextFSM 配合

正则负责确定交互边界；TextFSM 是使用者可选的结构化解析工具。两者都通过原生 Python 对象、函数和控制流程使用，不转换为声明式交互图。核心返回文本，不强制 TextFSM 依赖，也不内置一套业务模板系统。

使用者可以把 expect 已确认的一段文本交给普通 Python 解析函数，再根据字典、列表等结果决定回复：

```python
event = await command.expect({"question": question_pattern})
if event.kind == "match":
    rows = parse_with_textfsm(event.text, template)
    if rows and rows[0]["ACTION"] == "confirm":
        await command.reply("y", newline=True)
    else:
        await command.reply("n", newline=True)
```

parse_with_textfsm 是上层示例函数，不属于核心 API。若决策需要此前更多输出，可解析截至当前事件的累计快照；选段还是累计必须由使用者明确，避免把之前已消费的记录重复处理。

TextFSM 官方接口接受模板与文本并返回记录，按行推进且具有 EOF 行为。因此不能把每个任意网络 chunk 当作完整输入反复解析；这会涉及断行、跨行状态、重复记录及提前 EOF。首版推荐对已确认的交互片段或完整命令结果解析，每次独立解析使用新的解析器状态。若确需增量 TextFSM，应在上层单独实现并验证行缓冲、状态保留和最终结束处理。

未知询问可以通过调用方配置的更宽规则显式捕获，再交由 Python 判断；未匹配到时继续等待至期限，不擅自回答。即使使用 TextFSM，也仍需要明确何时已有足够文本可以解析，结构化解析不能替代终端边界判定。

## 基础 IO 与分页

AsyncSSH 建立持久 shell，按交互配置请求远端 PTY；Windows 不需要本地 PTY。所有输出只有一个读取所有者，由当前串行操作驱动读取。stderr 如独立返回必须消费；不能未经证明将 stdout/stderr 伪装成具有精确全序的单流。

读取采用块而不是假定 readline 能收到 prompt，处理跨块正则、分页标记、CR/LF、ANSI 及退格擦除。编码由配置明确；必要时用字节流与增量解码器，不能每个 chunk 单独 decode。保留原始内容与归一化匹配视图的区分，匹配位置要能准确映射到消费边界。

分页处理只在滚屏方法中自动进行，简单执行和 expect 不自动续页。自动滚屏默认采用“读取 → 匹配分页提示 → 发送空格 → 继续读取”的循环；只有已经匹配到分页提示才能发送继续键，不因无输出、尚未匹配 expect 或等待超时而盲发空格。支持覆盖分页标记、继续键和退出键，所有按键均不默认追加行结束符。

无 expect 时循环直到终止 prompt；有 expect 时循环直到目标或终止 prompt，先到者结束本次读取。单步模式一次只推进到下一个分页边界。非分页询问需要调用方提供匹配并显式回复，不能用空格替代答案。分页继续和退出均为当前命令的 IO 事件，不产生新命令记录。分页次数、总输出量和总时间均有上限，终端尺寸不能替代分页处理。

公开的 event.text、command.output、CommandResult.output 及正文日志使用一致的文本整理规则：移除已识别分页提示、对应的擦除控制序列、按回显规则识别的发送回显和终止 prompt，保留连续正文及其逻辑换行。分页边界不额外插入 ---- More ----、页码或分隔行，不丢失边界两侧正文，也不重复追加上一页。不能全局替换同名字符串而误删正文。

为处理跨块分页提示及擦除序列，尚未确认的终端尾部可以暂缓向公开回调发布。原始终端内容与公开正文分开保留，诊断记录不得冒充连续正文日志。敏感回复标记不能只隐藏发送事件，还需处理接收侧回显；未经相应处理的原始内容不得进入普通日志或公开回调。

匹配缓冲与完整结果分开。首版在总输出上限内保留未消费文本供原生正则匹配，已消费正文不重复扫描；不能用固定窗口静默淘汰可能参与跨块匹配的前缀。输出达到上限时明确报错并保留已读内容，不静默截断后返回完整成功。

## 长等待与命令边界

命令发送后，核心只在匹配到明确的交互模式时向调用者报告该事件。无输出只代表暂时没有输出；save 等操作可以长时间执行，不据此推断等待输入。

新命令与交互回复由 API 和当前会话状态区分，不根据“非 prompt 阻塞”推断。取得终止 prompt 才允许发起下一条顶层命令；分页提示及明确询问属于当前命令，未匹配到任何边界时继续等待至总期限。

命令总期限从开始发送起计算，贯穿所有 expect、分页、回复和异步用户处理，不能在每轮 expect 后重置。调用者可为 save 显式设置更长时间。上下文期限需实际取消仍在等待的异步操作；同步阻塞的 Python 处理无法靠协作式 asyncio 及时中止，使用者应避免在事件循环中运行长时间阻塞逻辑。

命令完成以可信终止 prompt 为依据；表示“终端交互已完成”，不代表业务成功。设备输出的 Error、保存失败等内容由上层判断。正文与 prompt 完全相同、异步告警混入输出等情况存在协议歧义，需通过实际样本完善规则，不能承诺任意设备绝对准确。

正常完成后保留已确认的可执行状态，不要求下一条命令前凭空再等待一个新 prompt。已有空闲输出需按配置处理，不能直接归入新命令；若它使终端状态无法确认，则明确失败。空闲输出可以保留为会话事件；命令进行中混入的异步通知若无可靠规则，保留在该命令输出中，不伪造归属，也不承诺对在途通知进行精确划分。

## 结果与故障语义

CommandResult 至少包含 command_id、command、output、prompt_before、prompt_after、耗时和完成状态。交互与分页事件关联同一个 command_id；普通命令与回复明确区分，敏感回复不写明文记录。vendor/platform 在会话中保存，结果可引用而不重复复制。

结果另含 output_complete：正常读取至终止 prompt 且未主动放弃剩余分页时为真；主动退出分页或命令未完成时为假。completed 表示终端交互结束，不能用来证明已经获取所有分页。中途命中 expect 只有读取事件和累计快照，此时不能取得最终 result。

完成状态保持简单：`completed / interrupted / unknown`。在任何命令内容写出前失败可标记 interrupted；一旦可能已提交命令，超时或断线就可能是 unknown。completed 仅表示已取得可信终端结束边界，不代替设备命令的业务成功状态。

遇到超时、取消、EOF 或无法恢复的 IO 错误，保留部分结果并关闭会话，不能继续复用可能失步的流。command 上下文在完成前被 break、异常退出或未处理交互时也执行有界清理。调用方解析异常不应被伪装成 SSH 错误。

核心不自动重试、不重放、不恢复设备业务状态，也不管理设备隔离。关闭连接不能证明远端命令已停止；上层根据 unknown 决定隔离、核查或重新连接。CancelledError 向上传播，清理不将其吞掉。

## 日志接口

核心统一使用 Python 标准库 logging，Session.connect 接受可选的 logger 参数，类型为 logging.Logger 或 logging.LoggerAdapter；未提供时使用包内命名 logger。核心不依赖 Loguru 等第三方日志库，也不直接接受接口语义不同的第三方 logger 对象。

调用方负责日志级别、handler、formatter、过滤、输出目的地、轮转及保留策略。核心不调用 basicConfig、不修改 root logger、不自动创建文件或控制台 handler；包内默认 logger 仅设置 NullHandler，并保留标准传播行为，便于应用统一配置。

以下是拟议接入方式：

```python
import logging

logger = logging.getLogger("myapp.execution")
async with Session.connect(options, profile=profile, logger=logger) as session:
    result = await session.execute_paged("display current-configuration")
```

使用 Loguru 或其他日志系统时，由调用方在注入的 logging logger 或其祖先 logger 上配置转发 handler，将 LogRecord 转交给目标系统。转发应保留级别、异常信息和关联字段，并避免重复传播或双向桥接造成递归；核心不额外实现一套日志适配框架。

日志内容约定如下：

- INFO：会话建立/关闭、命令开始/结束及耗时、完成状态和 output_complete，不默认附带设备正文。
- DEBUG：交互与分页事件、匹配类型等诊断信息。正文日志需由调用方显式启用，不能仅因开启 DEBUG 就输出全部设备内容；敏感信息保护不随日志级别降低。
- WARNING / ERROR：可继续处理的异常情况 / 导致操作失败的错误；异常向上传播时避免在多层重复记录同一堆栈。
- 使用 LogRecord.extra 携带 session_id、command_id（适用时）和 event_type，供 handler 过滤、关联或转发；敏感回复、凭证及未经处理的原始终端内容不写入消息或 extra。

正文日志采用前述连续正文规则，按已确认的新文本增量记录，不在每轮 expect 或翻页时重复记录累计 output；单独以 event_type 区分正文与生命周期/诊断事件。logging 的记录边界不代表设备换行：需要导出连续纯文本时，由调用方的专用 handler 按 session_id、command_id 原样追加正文消息，不添加时间戳、记录分隔换行或分页标记。默认日志格式不承诺可直接拼接为命令正文，完整正文仍以 CommandResult.output 为准。

同步 handler 应快速返回；文件、网络等慢速日志输出可由调用方通过 QueueHandler / QueueListener 等标准机制处理。日志不是可靠审计存储，过滤或关闭日志不能影响命令结果和执行状态。

## 实现与验证顺序

首版可按 session、channel、profiles 三部分组织，无需独立发布多个包或建立复杂继承体系。

1. AsyncSSH 连接与 shell、启动 prompt、默认/自定义识别、设备默认配置加载及权限/配置模式提取。
2. 单会话串行、简单执行、命令输出边界及结果对象。
3. 自动/单步滚屏、匹配目标后暂停、退出分页及连续正文整理。
4. command/expect/reply、共享事件消费位置与总期限。
5. TextFSM 上层示例与真实设备脱敏对话样本。
6. 接入上层设备队列及一个 VRP 业务能力，再逐步增加厂商规则。

验证重点包括：提示符/分页/多字节字符任意位置断包、无换行询问、多轮 y/n/a/c 对话、长时间静默后正常结束、问题后立即到达后续数据、命令正文含提示符、超时迟到输出、未完成上下文退出、同会话并发、敏感回显以及命令与回复归属。

分页专项覆盖：无 expect 自动读完、有 expect 中途暂停、目标未出现而 prompt 先到、逐页读取、页内命中后退出、退出后等到 prompt、简单执行遇到分页不发送空格、重复读取不重复回复旧分页点；连续正文在不同断包方式下保持一致，分页提示/退格/ANSI 不泄露至正文回调，不误删正文中的同名字符串，主动退出时 output_complete 为假。启动专项覆盖自定义命令识别、未知设备使用显式配置、默认配置被覆盖，以及同一设备不同 prompt 模式的无状态提取。

日志专项覆盖：默认不安装输出 handler、不改变应用日志配置；注入 Logger / LoggerAdapter 生效；转发到其他日志系统后关联字段保留且无重复记录；正文增量拼接与最终 output 一致；敏感回复及跨块回显不进入日志；关闭日志不影响执行结果。

首版采用 AsyncSSH 本地测试服务器模拟可控终端，并已在一台 Huawei VRP 交换机上验证分页、条件停止、接口配置采集及视图切换；版本和限制见使用说明。FakeConnection 只能证明上层调用顺序，无法证明终端协议正确。

## 参考资料

- [AsyncSSH 官方文档](https://asyncssh.readthedocs.io/en/latest/)
- [AsyncSSH API：shell、PTY 与流接口](https://asyncssh.readthedocs.io/en/latest/api.html)
- [TextFSM 官方说明：文本输入、状态及 EOF 行为](https://github.com/google/textfsm/wiki/TextFSM)
