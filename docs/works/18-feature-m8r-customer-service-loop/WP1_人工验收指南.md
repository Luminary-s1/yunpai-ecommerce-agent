# M8-R WP1 人工验收指南

## 验收目的

测试人：谢良璇。

这次验收不是看代码，也不是只看 pytest，而是从本机 HTTP 接口真实体验 WP1：导入话术和
关键词、观察候选不可见、完成审核批准、检查范围和有效期、查看来源追溯并执行退役。

WP1 不调用模型、不连接真实店铺、不发送客服消息，也不执行退款、改订单或其他业务动作。
验收数据全部保存在 F 盘新建的隔离目录中。

## 开始前

确认项目目录为：

```text
F:\CodexProjects\yunpai-ecommerce-agent-m8r-dev
```

准备两个 PowerShell 窗口。窗口一只运行服务，窗口二只运行人工验收助手。不要在两个窗口
中混用命令。

## 窗口一：启动隔离服务

1. 打开新的 PowerShell。
2. 完整粘贴下面两行并回车：

```powershell
Set-Location "F:\CodexProjects\yunpai-ecommerce-agent-m8r-dev"
& ".\docs\works\18-feature-m8r-customer-service-loop\启动_WP1_人工验收环境.ps1"
```

3. 等到窗口显示 Uvicorn 已在 `127.0.0.1:8091` 运行。
4. 不要关闭这个窗口。
5. 在浏览器打开 `http://127.0.0.1:8091/health`，应看到 `status` 为 `ok`。
6. 还可以打开 `http://127.0.0.1:8091/docs` 查看本次实际使用的管理接口。

如果提示端口 8091 被占用，先确认是否有旧测试服务。不能确认时不要结束陌生进程，改用：

```powershell
& ".\docs\works\18-feature-m8r-customer-service-loop\启动_WP1_人工验收环境.ps1" -Port 8092
```

同时在窗口二启动助手时追加 `-BaseUrl "http://127.0.0.1:8092"`。

## 窗口二：逐步人工验收

1. 打开第二个 PowerShell。
2. 完整粘贴下面两行并回车：

```powershell
Set-Location "F:\CodexProjects\yunpai-ecommerce-agent-m8r-dev"
& ".\docs\works\18-feature-m8r-customer-service-loop\WP1_人工验收助手.ps1"
```

3. 助手会依次展示 8 个步骤。每一步都先展示真实接口返回，再显示自动契约检查。
4. 你必须亲自阅读返回结果；符合预期输入 `Y`，不符合输入 `N`。
5. 不要使用 `-AutoConfirm`。这个开关只用于开发者验证脚本能否跑通，不是人工验收。

## 每一步看什么

| 步骤 | 你要观察的业务结果 |
|---|---|
| 服务就绪 | health 为 ok；模型关闭，因为 WP1 不需要模型 |
| 受控导入 | 8 行中 6 行生成候选；隐藏必填列隔离 1 行；手机号拒绝 1 行 |
| 候选不可见 | 批准前 scripts 为空，不能快速直答 |
| 审核与批准 | 普通内容可激活；明天才生效的话术提前批准返回 409 |
| 范围与完全匹配 | SKU 优先专属话术；跨店、缺场景、相似问法不能快速直答 |
| 关键词提示 | 退款 signal 为 advisory_only，没有 route 或 mode |
| 过期与不可信文件 | 过期内容不出现；公式不执行；trace 可回查来源 |
| 退役 | SKU 退役后回落到店铺话术；两层都退役后 scripts 为空 |

任何一步出现 `N`，都不要把 WP1 标记为完成。保留窗口输出和结果文件，把不一致的实际返回
发给开发者定位。

## 验收证据位置

助手会自动保存两份文件：

```text
F:\CodexProjects\yunpai-ecommerce-agent-m8r-runtime\wp1-manual-evidence\谢良璇_WP1人工验收过程_时间.txt
F:\CodexProjects\yunpai-ecommerce-agent-m8r-runtime\wp1-manual-evidence\谢良璇_WP1人工验收结果_时间.json
```

只有结果中的 `confirmation_mode=human`、`human_observations_passed=true` 和
`final_status=human_accepted` 同时成立，才说明谢良璇完成了 WP1 开发侧人工验收。

这仍不等于 M8-R 里程碑完成：WP1～WP4 已组成完整 PR #25，但仍须由未参与开发的缪海南
针对 PR 页面最新固定 head 执行 WP5 未见样本独立复验；WP5 通过并经负责人审阅后才能合入。

## 结束环境

回到窗口一，按 `Ctrl+C` 停止服务。隔离数据和验收证据会保留在 F 盘，不影响其他项目。
