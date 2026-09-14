# DSH 网页端升级任务交接文档（给 Claude）

> 任务目标：把 DeepSeek Harness 网页端升级到仓库最新版（0.1.1-rc.2），
> 使其能使用今天（2026-08-21）上线的多模态模型 `deepseek-v4-flash-vision-exp`。
> 桌面端（DSH Desktop）**保留不删除**（用户已决定）。

---

## 一、已完成（无需重做）

| 步骤 | 状态 | 说明 |
|---|---|---|
| git pull | ✅ | 仓库 `D:\cc-joesph\deepseek-harness` 已在最新：master = `b150a551` = tag `dsh-v0.1.1-rc.2`，`git pull` 显示 Already up to date |
| 适配确认 | ✅ | 最新版 `packages/llm/llm-deepseek/src/index.ts` 内置目录含 `deepseek-v4-flash-vision-exp`（inputModalities: text+image），支持多模态 |
| pnpm install | ✅ | 935 包安装完成（走 Clash 代理才快，见"网络"节） |
| 构建产物 | ✅ | tsc/tsdown host+client、Vite 前端 `apps/web/dist` 全部构建成功 |

## 二、未完成（待你继续）

**`dsh web` 服务器无法启动** —— 这是当前唯一阻塞点。

- 现象：`pnpm dsh web` 启动后 node 子进程**被静默杀掉**（无任何报错输出，exit -1），
  端口 3080 不监听，只剩挂死的 cmd shim。
- 类似现象：`tsx <file>` 这种 CLI 形式在 pnpm 环境下也静默退出 -1；
  但 `node --import tsx/esm <file>` 形式**正常**（`pnpm dsh --version` 正常输出版本）。
- 疑似原因：当前会话的沙箱限制（长驻进程 / 子进程管道被 kill），
  以及在中断前执行过一次 `Stop-Process` 误杀进程，导致**当前会话的 pwsh 命令全部以 0xC0000409 崩溃**。

### 建议下一步
1. **不要依赖原会话的 shell**——pwsh 已崩溃（0xC0000409）。请在**独立的普通终端**操作。
2. 在仓库根目录直接运行（绕过 pnpm 包装，用 tsx hook 形式，已验证该形式可用）：
   ```sh
   cd /d D:\cc-joesph\deepseek-harness
   node --import tsx/esm apps/cli/src/bin.ts web --no-open
   ```
   - 若沙箱仍然杀进程，就在**非沙箱的普通终端**里运行 `pnpm dsh web`。
3. 启动后验证：
   - 端口 3080 监听：`netstat -ano | findstr 3080`
   - 浏览器打开 `http://127.0.0.1:3080`
   - 模型选择器应能看到 `deepseek-v4-flash-vision-exp`（`.dsh/settings.yaml` 已配好）
4. 注意：旧 GUI 在 65002 端口（由桌面端服务），新的是 3080，互不冲突。

## 三、关键环境事实

- 仓库：`D:\cc-joesph\deepseek-harness`（git，master @ 0.1.1-rc.2，工作树干净）
- Node v24.14.1，pnpm 11.7.0
- 依赖已装、产物已构建 —— **不需要重新 install/build**
- 用户配置：`C:\Users\28037\.dsh\settings.yaml` 中 `llm-deepseek.models` 已含
  `deepseek-v4-flash-vision-exp`（与 flash/pro 并列）
- 桌面端：`C:\Users\28037\DSH Desktop\`（捆绑旧 harness 0.1.0-rc.7，
  其 DeepSeek 适配器**拒绝图片内容**，所以多模态必须用仓库版；用户已决定保留桌面端）

## 四、网络（重要）

- GitHub / registry.npmjs.org **直连极慢或不通**；本机有 Clash Verge 代理 `127.0.0.1:7897`（系统代理已启用）。
- 仓库 git 已配置 repo-local 代理：`git config http.proxy http://127.0.0.1:7897`（在仓库目录内）。
- npm/pnpm 走代理命令示例：`pnpm install --config.proxy=http://127.0.0.1:7897 --config.https-proxy=http://127.0.0.1:7897 --config.no-proxy=127.0.0.1,localhost`

## 五、踩坑记录（避免重蹈）

1. **`Select-Object -First N` 会杀管道进程**（PowerShell 提前终止上游）——之前误报过一次构建失败，实际是输出截断杀掉了进程。查看长输出请重定向到文件再 tail。
2. `tsx <file>`（tsx CLI 形式）在 pnpm 环境下静默崩溃；改用 `node --import tsx/esm <file>`。
3. `scripts/build.ts` 需要 `npm_execpath` 环境变量（必须经 `pnpm run` 启动）；直接跑各步骤更稳：
   - `pnpm exec tsc -b tsconfig.host.json`
   - `pnpm exec tsdown --env.DSH_BUILD_FACE host`
   - `pnpm exec tsc -b tsconfig.client.json`
   - `pnpm exec tsdown --env.DSH_BUILD_FACE client`
   - `pnpm --filter @deepseek-ai/dsh-web-frontend run build`
4. npm 会警告 "Unknown env config disturl/runtime/target"——无害，忽略即可。
