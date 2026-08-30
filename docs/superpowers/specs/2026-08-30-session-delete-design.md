# 会话删除功能设计（session.delete）

- 日期：2026-08-30
- 范围：DeepSeek Harness Web GUI —— 在「归档会话」「分叉会话」之外新增「删除会话」，以释放磁盘空间
- 仓库：`D:\cc-joesph\deepseek-harness`（dsh 0.1.1-rc.2）
- 状态：已获用户确认（2026-08-30）

## 1. 需求与已确认的语义

用户希望在会话行菜单（现有「重命名 / 分叉 / 归档」）之外提供「删除会话」，真正删除会话数据以节省空间。通过澄清确认的行为规则：

1. **彻底删除**：确认后立即永久删除会话的持久化数据并释放磁盘空间，不可恢复；不做回收站。
2. **有分叉子孙禁止删除**：目标会话存在任何以它为 `parentSession` 的会话（分叉或委托子会话）时拒绝删除，提示用户先处理子会话。
3. **运行中禁止删除**：agent 正在运行（`agent.status === 'running'`）时拒绝；空闲会话可删。
4. **打开中会话可删**：删除当前打开的会话后，客户端投影切回空白「新会话」视图（复用归档的投影清扫规则）。

## 2. 现状（探索结论）

- 行菜单：`packages/client/ui-workspace/src/client/rows/Rows.tsx`（rename/fork/archive 三项；菜单仅渲染于非空白会话行）。
- 行动作回调与对话框状态机：`packages/client/ui-workspace/src/client/WorkspaceBrowser.tsx`（工作区删除确认对话框、会话重命名对话框均为浏览器自持模式）。
- 文案：`packages/client/ui-workspace/src/client/locales.ts`（zh + en 双语）。
- 归档 RPC：`workspace.archiveSession` —— 仅隐藏，不触碰日志与记账槽位。
- **主机端无任何会话删除 RPC**（`session.*` 仅有 create/list/search/history/models/selectModel/rename/fork/prompt/attachment/updateQueue/cancel）。
- 事件基础设施已具备：`host/session-removed` 帧（api-proxy 监听 `session/disposed` 推送）、客户端 `manager.handleHostEnvelope` 已按 `kind: 'remove'` 移除行并调用会话实例 `handleRemoved()`。
- 列表构成：`session.list` = 活动会话（`ctx.sessions.list()`） ∪ 冷持久化清单（`sessionPersistence.list()`，仅 `cwd` 非空的），按 `updatedAt` 倒序 —— 删掉活动会话与磁盘工件后，行自然消失，无需墓碑表。
- 搜索索引（`packages/session-query/session-query-sqlite`）：已实现对持久化清单中消失条目的 reconcile 删除（`sqlite.spec.ts` 有专门测试）—— 工件删除后索引自动清。
- 工作区记账：`WorkspaceEntity` 已有 `detachSession(sessionId)`（从 `sessionIds` 移除）；归档集合 `archivedSessionIds` 为注册表级全局集合。
- 持久化服务（`packages/session/session-persistence`）：**无 delete 方法**，需新增抽象方法并在后端实现。
- 磁盘布局：`$DSH_HOME/sessions/<cwd 编码目录>/<会话id目录>/`，内含 `session.jsonl(.zstd)`、`metadata.json`、附件等 —— 删除整个会话目录即释放全部会话数据。

## 3. 方案选择

选定的方案 A：宿主端原生 `session.delete` RPC（全链路，语义干净、可测试）。否决方案 B（借用归档集合当墓碑——污染归档可恢复语义，且需额外行移除）与方案 C（仅清空内容——与归档重叠，不满足"删除会话"目标）。

## 4. 主机端设计（第 1 节，已确认）

### 4.1 RPC 契约

新增 wire RPC `session.delete`（`packages/host/apiproxy`，与既有 `session.*` 同一套链路）：

- `api/sessions.schema.ts`：`sessionDeleteRequestSchema` = `{ sessionId: SessionId }`；响应 `{ }`（空对象；沿用一元 RPC 的 request/value schema 模式，加入 `ResponseValue<'session.delete'>`）。
- `api/sessions.ts`（`SessionsApi`）：`delete(request)` 方法签名；JSDoc 说明语义与错误码。
- `api/rpc-map.ts`：`'session.delete': SessionsApi['delete']`。
- `fetch/client.ts` + `fetch/handler.ts`：注册方法、schema、invoke 映射（与 `session.rename` 等完全一致）。

### 4.2 校验（commit-point 权威，写链串行化）

参照工作区注册表 `enqueueOperation`，删除操作在一串行队列上完成"校验 + 执行"，避免与并发创建/归档/运行状态变化竞态。错误码：

| 条件 | 错误码 |
| --- | --- |
| 活动或持久化会话均不存在 | `session-not-found`（复用现有码） |
| `ctx.agents.get(id)?.status === 'running'` | `session-running`（新） |
| 存在 live 或持久化 header 的 `parentSession === id` | `session-has-descendants`（新） |

子孙判定覆盖分叉与委托子会话（宁严勿漏）；subagent 会话同样以其 `parentSession` 计入。若实现期发现需要排除 subagent 委托，须在实现计划中显式说明并回到设计评审。

### 4.3 执行顺序（一次操作，失败即中止，不半删）

1. **释放活动会话**：若目标为 live 会话，销毁其所属 agent handle（`agent.dispose()`，agent-loop 的创建事务保证会话与循环按序拆除）→ `session/disposed` → api-proxy 自动推送 `host/session-removed` 帧。运行中已被 4.2 拦截，不会在跑时拆除。
2. **工作区记账**：新增注册表公开方法 `deleteSession(sessionId)`（`packages/workspace/workspace`）——持久化地把该 id 从所有工作区 `sessionIds` 移除（内部复用 `detachSession` 语义）并从 `archivedSessionIds` 移除；Ungrouped 会话跳过记账部分；成功后走既有 `host/workspace-changed` / `host/archived-sessions-changed` 增量推送。未知 id 对域调用方为幂等无操作（RPC 层已先行校验存在性）。
3. **持久化删除**：新增 `SessionPersistence.delete(id)` 抽象方法（`packages/session/session-persistence`）：
   - jsonl 后端（`session-persistence-jsonl`）：删除整个会话目录（`session.jsonl(.zstd)`、`metadata.json`、附件等），释放所有空间；目录不存在视为幂等成功；
   - sqlite 后端（`session-persistence-sqlite`）：按 id 删除持久化行（复用测试中已有的 `delete-session-events` SQL 族）。
4. **应答**：`ok(request, {})`。

### 4.4 一致性（第 2 节，已确认）

- 归档集合不留残留；已归档会话被删除后不再出现在归档快照，避免取消归档复活幽灵 id。
- 搜索索引无需新代码：session-query-sqlite 对持久化清单消失条目的 reconcile 自动删行。
- 标题/统计/投影缓存按 id 惰性键控，删除后无引用、无泄漏；若投影缓存无法惰性回收则补充显式剔除（实现期验证）。
- 列表收敛：`session.list` = 活动 ∪ 持久化，两者清空后行自然消失。
- 跨标签页：`host/session-removed` 帧为全量广播；其他标签页同样移除行。

## 5. 客户端与 UI 设计（第 3 节，已确认）

### 5.1 运行时（`packages/client/runtime`）

- 删除归属**会话面**（与 `session.fork`/`session.rename` 同侧，wire RPC 同为 `session.*` 域）：sessions manager 新增 `delete(sessionId)`，经 `this.api.sessions.delete({ sessionId })` 调用 wire `session.delete`；`ctx.sessions` 会话面契约与方法同步补齐（仿 `fork` 的 list 级一元回声）。
- 行移除：复用 `host/session-removed` 既有处理（`kind: 'remove'` + `handleRemoved`），无新逻辑；成功后不清理本地摘要——行移除由 `host/session-removed` 帧驱动（设计裁决：帧与 RPC 同一流即时到达，本地清理与帧移除互斥，弃用清理条款）。
- 选中态收敛：ui-workspace 投影层复用归档规则——当前选中 id 进入"已删除集合"时投影切回空白「新会话」视图（覆盖本地回声与其他标签页帧）。

### 5.2 UI（`packages/client/ui-workspace`）

- 行菜单新增第 4 项「删除会话」（`menu.deleteSession`，zh「删除会话」/ en「Delete session」进 `locales.ts`），垃圾桶图标（图标库既有风格，`IconArchiveOutline20 size={16}` 同款 16px 槽位）。
- **确认对话框**：完全复用工作区删除对话框的浏览器自持状态机模式（`deleteTarget`/`deleting`/`deleteError`，独立于行组件，删除成功后可安全卸载该行）。文案明确「彻底删除，日志与附件将永久删除，不可恢复」。
- 运行中会话：菜单项置灰（行内已知状态作快速提示）；主机端仍权威拦截。host 拒绝时错误显示在对话框内（样式与工作区删除错误一致）。
- 空白 New Session 行不渲染菜单（现有模式已保证）。
- 删除成功后若目标为当前打开会话：对话框关闭，投影切回空白新会话视图。

### 5.3 契约与槽位

- 会话行组件的 props 增加 `onDelete`（`Rows.tsx` `SessionNodeItem`），类型与 `onArchive` 一致。
- `contract/slots.ts` 的 `WorkspaceBrowserInjected` 增加 `deleteSession: (sessionId) => Promise<void>`（对齐 `archiveSession`），apply 中实现为 `ctx.sessions.delete(sessionId)`。

## 6. 错误处理（第 4 节，已确认）

| 错误 | 对话框文案（zh） |
| --- | --- |
| `session-not-found` | 会话不存在（可能已被删除） |
| `session-running` | 会话正在运行，请等待执行结束后再删除 |
| `session-has-descendants` | 存在从它分叉/派生的子会话，无法删除 |
| 磁盘删除中途失败 | 保持原状态不半删，展示错误，可重试 |

## 7. 测试（第 4 节，已确认）

- **apiproxy**：schema 单测；fetch carrier/client-handler 映射；`api-proxy` 集成测——存在/运行中拒绝/有子孙拒绝/成功删除后 `session.list` 收敛；错误码各一例。
- **workspace 注册表**：`deleteSession` 持久化（记账移除 + 归档清理）、Ungrouped 跳过、幂等。
- **持久化后端**：jsonl `delete`（目录删除、幂等、保留非目标会话）；sqlite `delete`（行删除）。
- **client runtime**：workspaces service `deleteSession` 链路；`host/session-removed` 既有处理补删除场景断言。
- **ui-workspace**：菜单项存在与回调、确认对话框流程（确认/取消/错误展示）、运行中置灰。
- **GUI 护栏**：`pnpm run test:gui`（内环必跑）；可见输出变化再跑 `DSH_SNAPSHOT=replay pnpm run test:web`；client 源码包过 100% 覆盖门（`pnpm run test:coverage`，不可达防御分支用带真实原因的 `v8 ignore`）。
- 若该特性落入"非平凡产品可见行为变更"政策范围，须补 keyless snapshot 真实可运行示例（实现计划评估，GUI 护栏优先）。

## 8. 文档（第 4 节，已确认）

- 同步更新 README（中文为主）：apiproxy（wire 契约与错误码）、workspace（`deleteSession`）、session-persistence（`delete` 抽象方法）、client runtime、ui-workspace；JSDoc 合约同步。
- 按仓库规则补一篇 Agent Note（非平凡变更必需，与 PR 同仓）。
- 本设计文档存放于 `D:\cc-joesph\docs\superpowers\specs\`（不进 harness 仓库，避免触发其 doc 门禁）。

## 9. 落地与部署（第 4 节，已确认）

- 改动作用于源码 checkout `D:\cc-joesph\deepseek-harness`。
- 实现完成后：重建受影响包（`pnpm run build` 或按包构建）、跑类型检查与测试，然后**确认 127.0.0.1:43120 由哪个进程服务**（源码 dev server 还是打包 app）并重启该实例，新功能才会出现在 GUI。若由打包 app 服务，需另行评估重新打包路径（实现计划第一步先确证）。

## 10. 未决/实现期验证项

- 投影缓存（session-projection-cache）删除后是否需要显式剔除（先验证惰性回收是否足够）。
- `session-has-descendants` 是否排除 subagent 委托（当前按"都算"设计；实现期如发现误伤再回到设计评审）。
- 43120 服务进程形态（决定重建/重启方式）。