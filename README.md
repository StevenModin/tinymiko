# tinymiko

轻量、异步、可编程的网络设备 SSH 交互核心，直接使用 AsyncSSH，支持简单执行、自动/单步分页及原生 Python 交互。实现位于 `src/tinymiko`，运行时仅依赖标准库和 AsyncSSH。

```python
import asyncio

from tinymiko import Session


async def main():
    options = {"host": "192.0.2.10", "username": "admin"}
    async with Session.connect(options) as session:
        result = await session.execute_paged("display current-configuration")
        print(result.output)


asyncio.run(main())
```

连接参数透传 AsyncSSH，示例沿用其认证和主机密钥校验设置。身份包含 `vendor/platform`：网络设备表示厂商/型号或系列，Linux 表示发行版/版本。默认从登录输出识别，无法选择默认配置时需显式提供 `Profile`。当前内置 Huawei/Cisco 交互规则，已验证本地模拟终端及一台 Huawei VRP 交换机；具体版本和覆盖范围见使用说明。

使用方式及限制见 [tinymiko 使用说明](docs/tinymiko.md)，设计约定见 [执行核心设计](docs/execution-core.md)。


## 安装与开发

需要 Python 3.11+，当前使用 Python 3.14 验证。使用 uv 管理依赖：

```powershell
uv sync
uv run pytest
ruff check src tests scripts
ty check src scripts
uv build --wheel
```

Ruff 和 ty 使用通过 uv tool 安装的命令。实机测试脚本从环境变量读取密码，使用方法见使用说明；output 中保留脱敏的历史实机测试报告，不包含凭证或配置正文。项目尚未发布至 PyPI。
