# Windows LLM 主机监控

系统采集器、Ollama 请求网关、SQLite 历史记录和无需外部 CDN 的中文网页面板。

## 已部署入口

- 远端监控页：`http://100.122.59.107:18080/`，需连接 Tailscale。
- 远端电脑本机：`http://127.0.0.1:18080/`。
- 原有聊天地址继续使用 `http://100.122.59.107:11434/api/chat`。
- OpenAI 兼容 Base URL 继续使用 `http://100.122.59.107:11434/v1`。
- Tailscale 的 11434 转发至网关 18080，网关固定调用远端 `127.0.0.1:11434` 的 Ollama。
- 本机程序也应调用 `127.0.0.1:18080` 才能记录逐请求数据。直接调用上游 Ollama 的请求无法获得逐请求记录。
- 服务只监听回环地址，Tailscale Serve 提供 tailnet 内访问。访问控制沿用现有 tailnet 策略，应用没有额外的账号登录。

## 本地开发

在本项目目录执行：

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe run_monitor.py
```

打开 `http://127.0.0.1:18080/`。也可以双击 `start_monitor.cmd`。

本地运行采集本地资源；远端运行采集远端资源。默认上游必须是本机 Ollama。服务仅使用一个 worker，以保持一个采集器和一致的运行状态。

## 功能与指标口径

- CPU 总利用率、各逻辑核心利用率、内存和多卡 NVIDIA GPU 指标，每秒采样。
- GPU 记录显存容量、使用率、温度、功耗。单个指标不支持时保存 `null` 和原因，不伪造零值。
- `/api/ps` 每 5 秒查询已加载模型、上下文长度和模型显存占用；与整机显存占用分开显示。
- 原生与兼容聊天接口均支持流式和非流式响应。网关逐段转发，不等待整段回答生成完毕。
- 每次生成记录请求 ID、模型、观测到的模型摘要、默认参数和本次参数覆盖、思考设置、输入/输出 token、缓存 token（上游提供时）、耗时、结束原因和状态。
- **生成 token/s** = `eval_count / (eval_duration / 1e9)`，采用原生接口最终统计。
- **端到端 token/s** = 输出 token / 网关接收请求至结束的总时间，包含加载、等待和网络处理。
- **首字等待**指流式请求出现第一个非空文字、思考或工具调用片段的等待时间；非流式接口无法直接观测，留空。
- OpenAI 流请求未指定时补充 `stream_options.include_usage=true`；明确指定 false 时尊重客户端设置。兼容接口无引擎计时，生成速率留空，端到端速率仍可计算。
- 中断/取消且没有最终统计的请求保留记录，token 数为未知，`usage_complete=0`。流式片段数从不作为 token 数。
- 模型对比的生成速率是总 token / 总生成时间，并非把每次速率简单取平均。
- 资源曲线属于整机，不能视为某个请求独占 CPU/GPU 的用量。
- 配置的 `known_options` 合并可查询的模型默认参数与请求覆盖；无法查询的引擎默认值不宣称已知。配置还保存当时观测到的模型加载状态。

默认不保存提问、回答、图片或系统提示词正文；系统提示词只保存 SHA-256。请求中断前的输出也不会保存在数据库中。上游错误内容原样返回客户端，数据库仅记录错误类别。

## 文件结构

```text
systemCollector.py     CPU / RAM / GPU 采集器，可独立运行
config.py              运行配置
database.py            SQLite、保留策略和分钟汇总
telemetry.py           NDJSON / SSE 解析和 token、时间统计
gateway.py             请求转发和模型配置快照
main.py                应用生命周期与监控接口
run_monitor.py         本地启动入口
service_runner.py      远端后台入口、日志轮转
static/                中文监控网页
tests/                 单元测试、真实接口验证和浏览器验证
deploy_windows.ps1     部署与普通权限后台启动
rollback_windows.ps1   恢复原 Tailscale API 转发
```

## 监控 API

| 路径 | 用途 |
|---|---|
| `GET /healthz` | 监控后端健康及上游、采集器错误 |
| `GET /monitor/api/system` | 最新系统采样 |
| `GET /monitor/api/history?seconds=3600` | 资源历史；最多 30 天 |
| `GET /monitor/api/models` | 已安装与已加载模型 |
| `GET /monitor/api/requests` | 请求列表，支持 model/state/hours/limit/offset |
| `GET /monitor/api/requests/{id}` | 请求详情及配置快照 |
| `GET /monitor/api/summary?hours=24` | 按模型聚合统计 |
| `GET /monitor/docs` | 交互式监控 API 文档 |

转发的生成接口：`POST /api/chat`、`POST /api/generate`、`POST /v1/chat/completions`。
转发的查询接口：`GET /api/tags`、`GET /api/ps`、`GET /api/version`、`GET /v1/models`、`POST /api/show`。
未开放模型下载、创建或删除等管理接口。

每次生成的响应头包含 `X-Request-ID`。客户端可传 `X-Monitor-Client` 作为记录标签；TCP 转发后的 socket 地址可能是回环地址，不将其伪称为原始客户端 IP。

## 存储与后台运行

- 本地默认数据库：`data/monitor.sqlite3`，使用 WAL。
- 远端安装目录：`C:\ProgramData\LLMMonitor`，其中 `app` 是代码，`venv` 是独立环境，`data` 是数据库，`logs` 是日志。
- 计划任务 `LLM-Monitor` 使用当前账户的 S4U 登录和 **Limited** 权限，开机启动、失败重试，不使用 SYSTEM 或最高权限。
- 每分钟汇总已完成分钟的资源指标；原始采样保留 7 天，分钟汇总和请求保留 30 天。
- 监控重启时将遗留的进行中请求标记为已中断，保留已完成的历史记录。
- 日志每文件最多 5MiB，保留三个备份。复制数据库备份时应先停止服务，或使用 SQLite backup API，不能仅复制正在写入的主文件。
- 首次改变 Tailscale 转发前保存 `tailscale-serve-before.json`。回滚脚本恢复 11434 原转发、关闭新建的 18080 转发并停止任务，保留数据库。

远端 PowerShell 管理：

```powershell
Get-ScheduledTask -TaskName LLM-Monitor
Start-ScheduledTask -TaskName LLM-Monitor
Stop-ScheduledTask -TaskName LLM-Monitor
Get-Content C:\ProgramData\LLMMonitor\logs\monitor.log -Tail 30
& 'C:\Program Files\Tailscale\tailscale.exe' serve status
```

运行配置：`LLM_MONITOR_DB` 设置数据库路径，`LLM_MONITOR_INTERVAL` 设置 0.2–60 秒采样间隔，`LLM_MONITOR_GPU=0` 关闭 GPU 采集；`LLM_MONITOR_UPSTREAM` 仅允许本机 11434。

## 验证

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

以下命令会实际调用远端模型，验证正常生成、兼容接口、取消和错误请求：

```powershell
.\.venv\Scripts\python.exe tests/live_verify.py --base-url http://100.122.59.107:11434
```

浏览器检查是可选开发依赖，使用本机已安装的 Chrome：

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\.venv\Scripts\python.exe tests/browser_verify.py
```

实际验证报告和页面截图位于 `verification/`。部署后的真实请求从接入网关时开始统计，不会补造此前的历史。
