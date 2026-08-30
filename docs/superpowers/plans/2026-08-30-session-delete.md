# 会话删除功能（session.delete）实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在 DSH Web GUI 的会话行菜单（重命名/分叉/归档之外）新增「删除会话」，通过新的 wire RPC `session.delete` 彻底删除会话（日志、附件、索引、记账），释放磁盘空间。

**Architecture:** 新增 `SessionPersistence.delete(id)` 抽象方法与两个后端实现（jsonl 删目录 / sqlite 删行）；workspace 注册表新增 `deleteSession(sessionId)`（移除记账与归档）；api-proxy 新增 `session.delete` RPC（存在性/运行中/子孙校验 + 串行化编排：销毁 live agent → 删持久化 → 清记账）；客户端会话面与 ui-workspace 菜单/确认对话框接入，列表与选中态复用既有 `host/session-removed` 帧收敛。

**Tech Stack:** TypeScript（strict）、Cordis 服务、zod wire schema、Zustand/immer 客户端 store、better-sqlite3、React（无组件库，CSS Modules）、vitest。

**设计文档:** `D:\cc-joesph\docs\superpowers\specs\2026-08-30-session-delete-design.md`（已评审通过）。仓库为 `D:\cc-joesph\deepseek-harness`（分支 master，dsh 0.1.1-rc.2）。

## Global Constraints

- 仓库测试用 vitest；client 源码包受逐文件 100% 覆盖门约束（`pnpm run test:coverage`）；不可达防御分支用带真实原因的 `/* v8 ignore -- <reason> */` 注释。
- GUI 内环检查：`pnpm run test:gui`；可见输出变化再跑 `DSH_SNAPSHOT=replay pnpm run test:web`（linux PR CI 用读回放模式；只有确认输出变更后才用 `DSH_SNAPSHOT=refresh`）。
- 代码注释用英文；产品文案用中文（locales zh + en 键集必须一致，en 以 zh 键集为准）。
- 每个 npm 包 `@deepseek-ai/dsh-*`；ESM；本地相对导入用 `.ts` 后缀。
- 注册即效应：所有贡献经 `ctx.effect()`/`ctx.on()`，返回 disposer。
- Wire 错误码：新增 `session-running`、`session-has-descendants`；复用 `session-not-found`。运行时 wire 错误字符串按策略不翻译，对话框对已知错误码做本地化映射。
- 删除语义已确认：彻底删除（不可恢复）；有分叉/派生子会话（任何 header `parentSession === id`，含 subagent 委托）禁止删除；运行中（`agent.status === 'running'`）禁止删除；打开中的会话删除后客户端切回空白「新会话」视图。
- 每个 task 结束必须独立可测试；每步按 TDD（先红后绿）；每 task 一次 commit。
- 所有命令在 `D:\cc-joesph\deepseek-harness` 仓库根目录执行（pnpm workspace 根）。Windows Node ^22.19 || >=24。

---

### Task 1: `SessionPersistence.delete(id)` 抽象方法 + jsonl 后端实现

**Files:**
- Modify: `packages/session/session-persistence/src/index.ts`（抽象类 `SessionPersistence`，在 `readRaw` 后新增抽象方法）
- Modify: `packages/session/session-persistence-jsonl/src/index.ts`（后端实现，服务 API 区块）
- Test: `packages/session/session-persistence-jsonl/tests/jsonl.spec.ts`

**Interfaces:**
- Produces: `abstract delete(id: SessionId): Promise<void>` on `SessionPersistence`（Task 5 的 api-proxy 与 Task 2 依赖此签名）。
- Produces: jsonl 后端 `override async delete(id: SessionId): Promise<void>`——删除该会话整个目录（`session.jsonl(.zstd)`/`metadata.json`/附件），幂等。
- 语义（写入 JSDoc）：backend-owned artifact 与注册全部移除；会话不存在时幂等成功；不触碰其他会话。

- [ ] **Step 1: 写失败测试（jsonl.spec.ts，`// @vitest-environment node` 不需要——该文件已有环境约定，遵循文件顶部现有 pragma/导入风格）**

```ts
it('delete removes the whole session directory and drops the id from list()', async () => {
  const ctx = await jsonlContext() // 使用该文件现有的 ctx 装配助手
  const header = headerMeta({ id: SessionId('gone'), cwd: projectRoot })
  await ctx.sessionPersistence.create(header)
  await ctx.sessionPersistence.append(header.id, oneTurnLog())
  expect((await ctx.sessionPersistence.list()).some(m => m.id === header.id)).toBe(true)

  await ctx.sessionPersistence.delete(header.id)

  expect((await ctx.sessionPersistence.list()).some(m => m.id === header.id)).toBe(false)
  await expect(ctx.sessionPersistence.load(header.id)).rejects.toThrow()
})

it('delete is idempotent for an unknown session and keeps siblings', async () => {
  const ctx = await jsonlContext()
  const kept = headerMeta({ id: SessionId('kept'), cwd: projectRoot })
  const gone = headerMeta({ id: SessionId('gone'), cwd: projectRoot })
  await ctx.sessionPersistence.create(kept)
  await ctx.sessionPersistence.create(gone)
  await ctx.sessionPersistence.append(gone.id, oneTurnLog())

  await ctx.sessionPersistence.delete(gone.id)
  await ctx.sessionPersistence.delete(gone.id) // second call resolves

  expect((await ctx.sessionPersistence.list()).map(m => m.id)).toEqual([kept.id])
})
```

（用该文件现成的 `headerMeta`/`oneTurnLog`/`projectRoot`/ctx 装配助手——读取 `jsonl.spec.ts` 现有装配（`jsonlContext()` 或等价助手）并复用；断言行为不变。）

- [ ] **Step 2: 运行测试确认失败**

Run: `pnpm --filter @deepseek-ai/dsh-session-persistence-jsonl test -- jsonl`
Expected: FAIL——`ctx.sessionPersistence.delete is not a function`（抽象方法尚不存在）。

- [ ] **Step 3: 在抽象类新增 `delete`**

`packages/session/session-persistence/src/index.ts`，在 `readRaw`（~L124）与下一方法之间插入：

```ts
  /**
   * Remove every backend-owned artifact and registration for one session,
   * releasing its disk space. An id with no materialized artifact is an
   * idempotent success and never affects other sessions. Callers dispose any
   * live Session for the id before deleting, so backends may assume no live
   * writer.
   * @param id - the session whose artifacts are removed.
   */
  abstract delete(id: SessionId): Promise<void>
```

- [ ] **Step 4: jsonl 后端实现**

`packages/session/session-persistence-jsonl/src/index.ts`。先读该文件 `findLog` 的实现（~L740-810：按 id 跨项目目录定位，返回日志文件路径；`join(project, encodeSegment(id))`）。在服务 API 区块（`locate` 之后）新增：

```ts
  /**
   * Remove the whole session directory (log, metadata, attachments) for one
   * id, wherever it lives under the root. An absent session resolves.
   */
  override async delete(id: SessionId): Promise<void> {
    await this.ensureRootEncoding()
    const path = await this.findLog(id)
    if (path === undefined) return
    await rm(dirname(path), { recursive: true, force: true })
  }
```

（若实现期发现 `findLog` 存在缓存/信号参数约束，适配之；`dirname` 从 `node:path` 导入——先确认该文件已有导入，若无则补。`metadata.json` 与该日志同目录，`rm` 目录即覆盖。）

- [ ] **Step 5: 运行测试确认通过**

Run: `pnpm --filter @deepseek-ai/dsh-session-persistence-jsonl test -- jsonl`
Expected: PASS（含既有用例）。

- [ ] **Step 6: Commit**

```bash
git add packages/session/session-persistence/src/index.ts packages/session/session-persistence-jsonl/src/index.ts packages/session/session-persistence-jsonl/tests/jsonl.spec.ts
git commit -m "feat(session-persistence): delete(id) removes a session's artifacts (jsonl)"
```

---

### Task 2: sqlite 持久化后端 `delete`

**Files:**
- Create: `packages/session/session-persistence-sqlite/resources/sql/delete-session.sql`
- Modify: `packages/session/session-persistence-sqlite/src/sql.ts`（SQL_RESOURCES 加 `'delete-session'`）
- Modify: `packages/session/session-persistence-sqlite/src/store.ts`（`SqliteStore` 新增 `delete(id)`）
- Modify: `packages/session/session-persistence-sqlite/src/index.ts`（`SqliteSessionPersistence` override `delete`）
- Test: `packages/session/session-persistence-sqlite/tests/sqlite.spec.ts`

**Interfaces:**
- Consumes: `SessionPersistence.delete(id)`（Task 1）。
- Produces: sqlite 后端 `override async delete(id: SessionId): Promise<void>`——`DELETE FROM sessions WHERE id = ?`（`events` 表 `ON DELETE CASCADE`）；未知 id 幂等。

- [ ] **Step 1: 写失败测试（sqlite.spec.ts，复用该文件现成的 ctx/装配助手——先读文件顶部装配函数与 `sid()` 助手）**

```ts
it('delete removes the session row and its events, idempotently', async () => {
  const ctx = await sqliteContext() // 该文件现有装配助手
  const id = sid('gone')
  const { header } = await ctx.sessionPersistence.create(headerMeta({ id, cwd: projectRoot }))
  await ctx.sessionPersistence.append(id, oneTurnLog())
  expect((await ctx.sessionPersistence.list()).some(m => m.id === id)).toBe(true)

  await ctx.sessionPersistence.delete(id)
  await ctx.sessionPersistence.delete(id)

  expect((await ctx.sessionPersistence.list()).some(m => m.id === id)).toBe(false)
  await expect(ctx.sessionPersistence.load(id)).rejects.toThrow()
})
```

（若该文件装配方式不同——比如 `liveContext({ path, ... })`——沿用现有模式；断言行为不变。）

- [ ] **Step 2: 运行确认失败**

Run: `pnpm --filter @deepseek-ai/dsh-session-persistence-sqlite test`
Expected: FAIL——`delete is not a function`。

- [ ] **Step 3: 新增 SQL 资源**

`packages/session/session-persistence-sqlite/resources/sql/delete-session.sql`：

```sql
DELETE FROM sessions WHERE id = ?
```

`src/sql.ts` 的 `SQL_RESOURCES` 数组（`'delete-events-from'` 之后）加 `'delete-session'`。

- [ ] **Step 4: store 与后端实现**

`src/store.ts`（参照 `update-session-revision` 等既有方法的写法）新增：

```ts
  /** Remove one session's row; `events` cascade via schema. Unknown ids resolve. */
  delete(id: SessionId): void {
    this.db.prepare(sql('delete-session')).run(id)
  }
```

`src/index.ts` 的 `SqliteSessionPersistence`（`readRaw` override 附近）新增：

```ts
  override delete(id: SessionId): Promise<void> {
    this.store.delete(id)
    return Promise.resolve()
  }
```

（若 `SqliteSessionPersistence` 经 coordinator 转发，`delete` 保持直通 store——文件级删除不需要 coordinator 参与；冷会话无 coordinator 内存态。）

- [ ] **Step 5: 运行确认通过**

Run: `pnpm --filter @deepseek-ai/dsh-session-persistence-sqlite test`
Expected: PASS（含既有用例）。

- [ ] **Step 6: Commit**

```bash
git add packages/session/session-persistence-sqlite/resources/sql/delete-session.sql packages/session/session-persistence-sqlite/src/sql.ts packages/session/session-persistence-sqlite/src/store.ts packages/session/session-persistence-sqlite/src/index.ts packages/session/session-persistence-sqlite/tests/sqlite.spec.ts
git commit -m "feat(session-persistence-sqlite): delete(id) removes the session row"
```

---

### Task 3: workspace 注册表 `deleteSession(sessionId)`

**Files:**
- Modify: `packages/workspace/workspace/src/index.ts`（`WorkspaceRegistry`）
- Test: `packages/workspace/workspace/tests/workspace.spec.ts`

**Interfaces:**
- Consumes: `WorkspaceEntity.detachSession(sessionId)`（已存在，entity.ts L174-178）；`archivedSessionIds` 全局集合。
- Produces: `deleteSession(sessionId: SessionId): Promise<void>` on `WorkspaceRegistry`——幂等；从每个实体的 `sessionIds` 移除该 id（复用 `detachSession`），并从全局 `archivedSessionIds` 移除；`enqueueOperation` 串行化。成功返回；未知 id 幂等成功（RPC 层负责存在性报错）。

- [ ] **Step 1: 写失败测试（workspace.spec.ts——读该文件现有装配：存在 `archiveSession(SessionId('kept'))` 的用法与 registry 装配）**

```ts
it('deleteSession removes the id from accounting and the archive set', async () => {
  const { result } = await workspaceScenario() // 该文件现有装配（含 sessionPersistence 与 domains）
  const kept = SessionId('kept')
  const gone = SessionId('gone')
  await result.registry.create(projectDir)
  await result.registry.attachSession(fixtureWorkspaceId, computedKeptHeader) // 用该文件既有 attach 路径
  ...
})
```

> 注意：**不要臆造该文件的装配 API**。先读 `workspace.spec.ts` 的现有 `archiveSession` 测试（L882-928），沿用其 fixture 装配（registry、domains、sessionPersistence stub、`attachSession` 或 bootstrap 路径），把断言替换为：`deleteSession('kept')` 后该 workspace 的 `sessionIds` 不含 `'kept'`，且 `archivedSessionIds` 不含 `'kept'`；重复调用 `deleteSession`（含从未存在的 id）幂等成功不动其他行；`deleteSession` 写失败时（可选：stub 抛错）保持原状并 reject。

- [ ] **Step 2: 运行确认失败**

Run: `pnpm --filter @deepseek-ai/dsh-workspace test`
Expected: FAIL——`result.registry.deleteSession is not a function`。

- [ ] **Step 3: 实现**

`packages/workspace/workspace/src/index.ts`，`archiveSession` 方法（L244-255）之后新增：

```ts
  /**
   * Remove one session from every workspace account and the registry-global
   * archive set. The session's log is NOT touched here — callers delete its
   * persisted artifacts separately. Unknown ids resolve idempotently.
   * @param sessionId - the session to unaccount.
   */
  deleteSession(sessionId: SessionId): Promise<void> {
    return this.enqueueOperation(async () => {
      const state = this.requireState()
      const archived = state.archivedSessionIds.includes(sessionId)
        ? state.archivedSessionIds.filter(id => id !== sessionId)
        : state.archivedSessionIds
      for (const entity of this.entities.values()) {
        if (entity.sessionIds.includes(sessionId)) {
          await entity.detachSession(sessionId)
        }
      }
      if (archived !== state.archivedSessionIds) {
        await this.setState({ ...state, archivedSessionIds: archived })
      }
    })
  }
```

（`entity.sessionIds` getter 按 sessionPath 过滤——记录的原始 `sessionIds` 可能含未通过过滤的 id；如需连这类残留一并清掉，改为直接比较 `entity.record.sessionIds`；实现期以该包测试为准。`WorkspaceEntity.record` 为 public readonly——可读。）

- [ ] **Step 4: 运行确认通过**

Run: `pnpm --filter @deepseek-ai/dsh-workspace test`
Expected: PASS（含既有用例）。

- [ ] **Step 5: Commit**

```bash
git add packages/workspace/workspace/src/index.ts packages/workspace/workspace/tests/workspace.spec.ts
git commit -m "feat(workspace): deleteSession removes a session's accounting and archive membership"
```

---

### Task 4: `session.delete` wire 契约（schema / api / rpc-map / fetch / connection）

**Files:**
- Modify: `packages/host/apiproxy/src/api/sessions.schema.ts`（request/value schema）
- Modify: `packages/host/apiproxy/src/api/sessions.ts`（`SessionsApi.delete` 签名，含 JSDoc）
- Modify: `packages/host/apiproxy/src/api/rpc-map.ts`（`'session.delete'` 映射）
- Modify: `packages/host/apiproxy/src/fetch/client.ts`（value schema 映射 + `delete` 方法）
- Modify: `packages/host/apiproxy/src/fetch/handler.ts`（`UNARY_ROUTES` 项）
- Modify: `packages/client/connection/src/client/fixture.ts`（wire 分发 case）
- Modify: `packages/host/apiproxy/src/api-proxy.ts`（`sessions.delete` 中间桩实现——Task 5 替换为真实现）
- Test: `packages/host/apiproxy/tests/rpc-schemas.spec.ts`、`packages/host/apiproxy/tests/fetch-carrier.spec.ts`、`packages/host/apiproxy/tests/client-handler.spec.ts`

**Interfaces:**
- Produces: wire `session.delete`（request `{ sessionId: SessionId }`，value `{}`）；`SessionsApi['delete']` 签名：`Promise<RpcResponse<{}>>`。
- Task 5 依赖以上全部映射；Task 6 依赖 `SessionsApi['delete']` 与 fetch client 方法。

- [ ] **Step 1: 契约与映射（先写实现——本 task 的"测试"是映射级红绿）**

`sessions.schema.ts`（`sessionCancel*` 之后）新增：

```ts
/** session.delete request payload (the session whose artifacts are destroyed). */
export const sessionDeleteRequestSchema = z.object({
  sessionId: sessionIdSchema,
}) satisfies z.ZodType<Wire<RequestPayload<'session.delete'>>>

/** session.delete response value. */
export const sessionDeleteValueSchema = z.object({}) satisfies z.ZodType<Wire<ResponseValue<'session.delete'>>>
```

`sessions.ts`（`cancel` 签名之后）新增：

```ts
  /**
   * Permanently deletes one session: disposes its live agent when idle,
   * removes its persisted artifacts and attachments, and unaccounts it from
   * the workspace registry and archive set. Rejects with `session-not-found`
   * when no live or persisted session has the id, `session-running` while the
   * agent is running, and `session-has-descendants` when any live or
   * persisted session records this id as its parent.
   */
  delete(request: RpcRequest<{ sessionId: SessionId }>): Promise<RpcResponse<{}>>
```

`rpc-map.ts`（`session.cancel` 后）新增：`'session.delete': SessionsApi['delete']`。

`fetch/handler.ts`（`'session.cancel'` 行后）新增：
`'session.delete': { schema: sessionDeleteRequestSchema, invoke: (api, r) => api.sessions.delete(r) },`；顶部 import 加 `sessionDeleteRequestSchema`。

`fetch/client.ts`：value schema 映射对象（`'session.cancel'` 附近，L173 区域）加 `'session.delete': sessionDeleteValueSchema,`；接口加 `delete(payload: RequestPayload<'session.delete'>, signal?: AbortSignal): Promise<RpcResponse<ResponseValue<'session.delete'>>>`；实现加 `delete: (payload, signal) => this.callUnary('session.delete', payload, signal),`；import 加 `sessionDeleteValueSchema`。

`packages/client/connection/src/client/fixture.ts`（L3189 `'session.cancel'` case 后）加：

```ts
      case 'session.delete': return this.api.sessions.delete(request)
```

`api-proxy.ts`（`sessions.cancel` 实现之后）加**中间桩**（Task 5 整体替换）：

```ts
      async delete(request) {
        return apiNotImplemented(request, 'session.delete')
      },
```

（`apiNotImplemented` 若不存在，用内联 `return err(request, { code: 'internal', message: 'session.delete is not implemented yet', details: {} })`——Task 5 会删除此桩。）

- [ ] **Step 2: 映射级测试**

`tests/rpc-schemas.spec.ts` 现有结构（`archiveSession request/value carry the id...` 同款）新增：

```ts
it('session.delete request/value carry the session id and an empty value', () => {
  const request = { payload: { sessionId: 's-gone' }, rpcId: 'r1' }
  expect(sessionDeleteRequestSchema.parse(request.payload)).toEqual({ sessionId: 's-gone' })
  expect(sessionDeleteValueSchema.parse({})).toEqual({})
})
```

`tests/fetch-carrier.spec.ts`（参照既有 `archiveSession` 或某个 session 方法的 carrier 断言）新增一条：经 `RpcClient`/carrier 发送 `session.delete` request 后，handler 侧收到 `session.delete` 并回 ok `{}`（沿用该文件现有的 `ok`/`expectOk`/client 装配模式；先读该文件的 `archiveSession request/value...` 测试再写）。

`tests/client-handler.spec.ts`：新增 `session.delete` 的 client-handler 样例（该文件 `archiveSession: r => ok(r, { archivedSessionIds: ... })` 同款，delete 回 `ok(r, {})`），断言 wire 名解析。

- [ ] **Step 3: 运行确认通过**

Run: `pnpm --filter @deepseek-ai/dsh-apiproxy test`
Expected: PASS（含既有用例）。

- [ ] **Step 4: Commit**

```bash
git add packages/host/apiproxy/src/api/sessions.schema.ts packages/host/apiproxy/src/api/sessions.ts packages/host/apiproxy/src/api/rpc-map.ts packages/host/apiproxy/src/fetch/client.ts packages/host/apiproxy/src/fetch/handler.ts packages/host/apiproxy/src/api-proxy.ts packages/client/connection/src/client/fixture.ts packages/host/apiproxy/tests/rpc-schemas.spec.ts packages/host/apiproxy/tests/fetch-carrier.spec.ts packages/host/apiproxy/tests/client-handler.spec.ts
git commit -m "feat(apiproxy): session.delete wire contract and carrier plumbing"
```

---

### Task 5: api-proxy `session.delete` 实现（校验 + 编排）

**Files:**
- Modify: `packages/host/apiproxy/src/api-proxy.ts`
- Test: `packages/host/apiproxy/tests/api-proxy-workspace.spec.ts`（或该包既有的 sessions 集成测试文件——先读该文件的装配：`api.workspace.*` 用 `request({...})`/`expectOk`/liveContext 式装配）

**Interfaces:**
- Consumes: Task 1 `sessionPersistence.delete(id)`；Task 3 `workspaceRegistry.deleteSession(sessionId)`；`ctx.agents`（`get`/`create`/`resume`，handle 的 `dispose()`）；`SessionNotFound`（已有，api-proxy L104）；`agentFor`（既有，L1757 区域）。
- Produces: 完整 `sessions.delete`；私有 `sessionDeletionChain` 串行化；私有 live-handle 保留表 `sessionHandles: Map<SessionId, AgentHandle>`（ensureSession 回填、session/disposed 清理）。

- [ ] **Step 1: 在 api-proxy 增加 handle 保留与删除链**

在模块作用域（靠近既有 `workspaceCreationChain`、`sessionCreations` 声明处）新增：

```ts
/** Live agent handles api-proxy created, retained so session.delete can dispose them. */
const sessionHandles = new Map<SessionId, AgentHandle>()
/** Serializes session deletions against each other (check-then-act commit point). */
let sessionDeletionChain: Promise<void> = Promise.resolve()
```

（若 `workspaceCreationChain`/`sessionCreations` 是 `let` 在 `createApi` 闭包内，则把 `sessionHandles`/`sessionDeletionChain` 也放在同一闭包内；读该文件的既有声明位置后对齐。）

在 `ensureSession` 的两个 handle 获取点（`ctx.agents.resume({...})` 与 `ctx.agents.create({...})`）改为保留 handle：

```ts
        const recovered = await ctx.agents.resume({ resumeSessionId: sessionId, agentOptions: agentOptions(), setup: (await composeAgent(storedPreset)).setup })
        sessionHandles.set(sessionId, recovered)
        return recovered.agent
```
```ts
        const created = await ctx.agents.create({ sessionId, agentOptions: agentOptions(), meta: { cwd, ... }, setup: composition.setup })
        sessionHandles.set(sessionId, created)
        return created.agent
```

在 dispose 监听区（api-proxy 现有 `ctx.on('session/disposed', ...)` 处，host 流那一段有 `openCalls.delete` 的先例）加：

```ts
ctx.on('session/disposed', (session: Session) => { sessionHandles.delete(session.id) })
```

- [ ] **Step 2: 写失败测试（集成）**

`tests/api-proxy-workspace.spec.ts` 装配下（`api = await ...` 后），新增（沿用该文件 `request`/`expectOk`/错误断言样式——先读其中 `workspace.archiveSession` 的测试 L543-564 复刻结构）：

```ts
it('session.delete removes a persisted session from listing', async () => {
  const created = await api.sessions.create(request({ workspaceId, sessionId: sid('gone') }))
  expectOk(created)
  expect(expectOk(await api.sessions.list(request({}))).items.some(s => s.sessionId === sid('gone'))).toBe(true)

  expectOk(await api.sessions.delete(request({ sessionId: sid('gone') })))

  expect(expectOk(await api.sessions.list(request({}))).items.some(s => s.sessionId === sid('gone'))).toBe(false)
})

it('session.delete rejects unknown sessions with session-not-found', async () => {
  const rejected = await api.sessions.delete(request({ sessionId: sid('ghost') }))
  expect('error' in rejected && rejected.error.code).toBe('session-not-found')
})
```

（运行中/子孙两个拒绝分支若该测试装配难以构造 live agent，放在 Task 5 的单测级 helper 测试或如实说明装配限制——但**必须**至少覆盖成功删除与 not-found 两条。）

- [ ] **Step 3: 运行确认失败**

Run: `pnpm --filter @deepseek-ai/dsh-apiproxy test -- api-proxy-workspace`
Expected: FAIL——delete 仍是内部错误桩。

- [ ] **Step 4: 实现 `sessions.delete`**

用 Task 4 的桩整体替换为：

```ts
      async delete(request) {
        const { sessionId } = request.payload
        const operation = sessionDeletionChain.then(async () => {
          // Existence: live or persisted; a definite miss fails like fork/history.
          const attached = ctx.sessions.get(sessionId)
          if (attached === undefined) {
            const persistence = ctx.get('sessionPersistence')
            const stored = persistence === undefined
              ? undefined
              : (await persistence.list()).find(header => header.id === sessionId)
            if (stored === undefined) {
              throw new SessionNotFound(sessionId)
            }
          }
          const agent = ctx.agents.get(sessionId)
          if (agent !== undefined && agent.status === 'running') {
            return { kind: 'err', error: { code: 'session-running', message: `session "${sessionId}" is running`, details: { sessionId } } } as const
          }
          // Descendants: any live or persisted session recording this id as parent.
          const hasChild = await sessionHasDescendants(ctx, sessionId)
          if (hasChild) {
            return { kind: 'err', error: { code: 'session-has-descendants', message: `session "${sessionId}" has descendant sessions`, details: { sessionId } } } as const
          }
          // 1. Live teardown (idle only — running was rejected above).
          const handle = sessionHandles.get(sessionId)
          if (handle !== undefined) await handle.dispose()
          // 2. Remove persisted artifacts (idempotent when absent).
          const persistence = ctx.get('sessionPersistence')
          if (persistence !== undefined) await persistence.delete(sessionId)
          // 3. Unaccount from the workspace registry + archive set.
          await ctx.workspaceRegistry.deleteSession(sessionId)
          return { kind: 'ok' } as const
        })
        sessionDeletionChain = operation.then(() => {}, () => {})
        try {
          const outcome = await operation
          if (outcome.kind === 'err') return err(request, outcome.error)
          return ok(request, {})
        } catch (error: unknown) {
          if (error instanceof SessionNotFound) {
            return err(request, { code: 'session-not-found', message: error.message, details: { sessionId } })
          }
          return err(request, {
            code: 'internal',
            message: `failed to delete session "${sessionId}": ${String(error)}`,
            details: {},
          })
        }
      },
```

并在同一文件新增 helper（模块级或在闭包内，与 `historySourceFor` 同区）：

```ts
/** Whether any live or persisted session records `parent` as its lineage root. */
async function sessionHasDescendants(ctx: Context, parent: SessionId): Promise<boolean> {
  if (ctx.sessions.list().some(session => session.header.parentSession === parent)) return true
  const persistence = ctx.get('sessionPersistence')
  if (persistence !== undefined && (await persistence.list()).some(header => header.parentSession === parent)) return true
  return false
}
```

（`SessionNotFound` 已 import（L104 `ApiRemoteSessionNotFound as SessionNotFound`）；`AgentHandle` 类型从 `@deepseek-ai/dsh-agent` 的公开导出按需 import（若该包 handle 类型不在根导出，用 `Awaited<ReturnType<typeof ctx.agents.create>>` 推断替代）；`Context` 与 `SessionId` 类型按 api-proxy 现有 import 风格——`ctx.sessions.list()` 返回带 `header` 的 Session，照 ensureSession 用法。）

- [ ] **Step 5: 运行确认通过**

Run: `pnpm --filter @deepseek-ai/dsh-apiproxy test`
Expected: PASS（含 Task 4 映射测试与新的集成测试）。`session-running`/`session-has-descendants` 分支若集成装配不便构造，用现有 liveContext 装配（如果能 `ctx.agents.create` 一个 agent）覆盖至少一条，另一条以单元级 helper 测试兜底（`sessionHasDescendants` 直测）。

- [ ] **Step 6: Commit**

```bash
git add packages/host/apiproxy/src/api-proxy.ts packages/host/apiproxy/tests/api-proxy-workspace.spec.ts
git commit -m "feat(apiproxy): session.delete validates and orchestrates permanent deletion"
```

---

### Task 6: 客户端运行时 `ctx.sessions.delete`

**Files:**
- Modify: `packages/client/runtime/src/client/contract/sessions.ts`（`ISessions` 加 `delete`）
- Modify: `packages/client/runtime/src/client/sessions/manager.ts`（wire 调用；成功后不清理本地摘要，行移除由 `host/session-removed` 帧驱动）
- Modify: `packages/client/runtime/src/client/sessions/service.ts`（`SessionRuntime.delete`）
- Modify: `packages/test-support/client-runtime/src/sessions.ts`（`TestSessions` 加 `delete` stub）
- Modify: `packages/client/runtime/tests/workspaces-service.client.spec.ts`（或该包 sessions 面既有测试文件）
- Modify: `packages/client/connection/tests/fake-api.client.ts`（wire handler 加 `session.delete`）

**Interfaces:**
- Consumes: `SessionsApi.delete`（Task 4）；manager 现有 `this.api.sessions.*` 与 `recordMutation`/'remove' 语义（`host/session-removed` 已驱动行移除）。
- Produces: `ISessions.delete(sessionId: SessionId): Promise<void>`；`SessionRuntime.delete` 抛错携带 `{ code, message }`（供 UI 本地化）；成功后不清理本地摘要——行移除由 `host/session-removed` 帧驱动（设计裁决：帧与 RPC 同一流即时到达，本地清理与帧移除互斥，弃用清理条款）。

- [ ] **Step 1: 契约与实现（本 task 测试为服务级红绿）**

`contract/sessions.ts`（`fork` 附近）加：

```ts
  /**
   * Permanently delete one session over the wire. The host rejects running
   * sessions and sessions with descendants; the row disappears on the
   * `host/session-removed` frame.
   * @param sessionId - the session to delete.
   * @throws {SessionDeleteError} carrying the wire code and message.
   */
  delete(sessionId: SessionId): Promise<void>
```

`manager.ts`（`fork` 实现附近 L580-610）加：

```ts
  /** Wire session.delete; merges nothing (the removed frame drives the list). */
  async delete(sessionId: SessionId): Promise<RpcResult<{}>> {
    return await this.api.sessions.delete({ sessionId })
  }
```

`service.ts`（`SessionRuntime`，`fork` 之后）加：

```ts
  /**
   * Delete one session permanently. The host validates (running / descendants);
   * the list row and open view converge on `host/session-removed`.
   * @param sessionId - the session to delete.
   * @throws {SessionDeleteError} with the wire code and message.
   */
  async delete(sessionId: SessionId): Promise<void> {
    const result = await this.manager.delete(sessionId)
    if (!result.ok) throw new SessionDeleteError(result.error, sessionId)
    this.projectList()
  }
```

（`SessionDeleteError` 若包内无对应错误类，仿照 fork 的 `SessionForkError` 新建——查看 `SessionForkError` 定义处（service.ts 或 errors 文件），复制其形制：`class SessionDeleteError extends Error { constructor(readonly error: RpcError, readonly sessionId: SessionId) { super(error.message); this.name = 'SessionDeleteError' } }`。）

`packages/test-support/client-runtime/src/sessions.ts`（`fork` stub 附近 L487）加：

```ts
  delete(sessionId: SessionId): Promise<void> {
    this.calls.push({ method: 'delete', args: [sessionId] })
    return Promise.resolve()
  }
```

`packages/client/connection/tests/fake-api.client.ts`（`session.cancel` 同款处）加：

```ts
    delete: (payload: unknown) => this.record('session.delete', payload, Promise.resolve(ok({}))),
```

- [ ] **Step 2: 服务测试（`workspaces-service.client.spec.ts` 或 runtime sessions 既有测试文件——先读该文件 `archiveSession` 测试 L460-475 的结构）**

```ts
it('delete forwards the wire call and settles (Host removal drives the list)', async () => {
  const workspaces = await benchWorkspaces() // 现有装配
  await expect(workspaces.sessions?.delete(sid('s-idle'))).resolves.toBeUndefined()
  expect(api.callsOf('session.delete')).toEqual([{ sessionId: 's-idle' }])
})

it('delete surfaces a wire rejection with its code', async () => {
  const workspaces = await benchWorkspaces()
  api.respond('session.delete', err('session-running', 'session is running'))
  await expect(workspaces.sessions?.delete(sid('s-live'))).rejects.toMatchObject({ name: 'SessionDeleteError', code: 'session-running' })
})
```

（若该文件无 `workspaces.sessions` 面，改挂 runtime sessions 面的既有测试文件；`api.respond`/`callsOf` 沿用现有 fake-api helper 名。）

- [ ] **Step 3: 运行确认通过**

Run: `pnpm run test:gui`（若该包测试不在 gui 组，用 `pnpm --filter @deepseek-ai/dsh-client-runtime test`）
Expected: PASS（含既有用例）。

- [ ] **Step 4: Commit**

```bash
git add packages/client/runtime/src/client/contract/sessions.ts packages/client/runtime/src/client/sessions/manager.ts packages/client/runtime/src/client/sessions/service.ts packages/test-support/client-runtime/src/sessions.ts packages/client/connection/tests/fake-api.client.ts packages/client/runtime/tests/workspaces-service.client.spec.ts
git commit -m "feat(client-runtime): sessions.delete drives the wire delete"
```

---

### Task 7: ui-workspace「删除会话」菜单 + 确认对话框

**Files:**
- Modify: `packages/client/ui-workspace/src/client/locales.ts`（zh + en 键）
- Modify: `packages/client/ui-workspace/src/client/contract/slots.ts`（注入面加 `deleteSession`）
- Modify: `packages/client/ui-workspace/src/client/index.ts`（apply 注入 `deleteSession` → `ctx.sessions.delete`）
- Modify: `packages/client/ui-workspace/src/client/rows/Rows.tsx`（菜单项 + `onDelete` prop + 运行中禁用）
- Modify: `packages/client/ui-workspace/src/client/WorkspaceBrowser.tsx`（`FlatList`/`SessionTree`/行回调 + 会话删除对话框状态机）
- Test: `packages/client/ui-workspace/tests/workspace-browser.client.spec.tsx`

**Interfaces:**
- Consumes: `ctx.sessions.delete(sessionId)`（Task 6）；`SessionNode.running`（tree.ts L27）；`IconTrashOutline16`（Rows.tsx 已导入）。
- Produces: 行菜单第 4 项「删除会话」（`menu.deleteSession`）；销毁性确认对话框；错误按码本地化显示。

- [ ] **Step 1: 文案（locales.ts）**

`zh` 字典（`'menu.archiveSession'` 后）加：

```ts
  'menu.deleteSession': '删除会话',
  'delete.session.title': '删除会话',
  'delete.session.desc': '“{name}”的会话记录与附件将被永久删除，不可恢复。',
  'delete.session.pending': '正在删除会话…',
  'delete.session.error.running': '会话正在运行（或等待审批），请等待其结束后再删除。',
  'delete.session.error.descendants': '该会话存在从它分叉或派生的子会话，无法删除。',
  'delete.session.error.missing': '会话不存在（可能已被删除）。',
```

`en` 字典同键（键集必须一致）：

```ts
  'menu.deleteSession': 'Delete session',
  'delete.session.title': 'Delete session',
  'delete.session.desc': '“{name}” and its attachments will be permanently deleted. This cannot be undone.',
  'delete.session.pending': 'Deleting session…',
  'delete.session.error.running': 'The session is running (or waiting for approval). Wait for it to finish before deleting.',
  'delete.session.error.descendants': 'This session has forked or derived child sessions and cannot be deleted.',
  'delete.session.error.missing': 'The session no longer exists (it may already have been deleted).',
```

- [ ] **Step 2: UI 组件（Rows.tsx）**

`SessionNodeItem`（L362）props 加 `onDelete: (id: SessionNode['id']) => void`；`sessionMenuItems`（L389-394）改为四项（delete 项 disabled 当 `row.running`，样式沿用菜单 disabled 惯例）：

```tsx
  const sessionMenuItems = [
    { id: 'rename', label: t('rename'), icon: <IconEditOutline16 /> },
    { id: 'fork', label: t('menu.fork'), icon: <IconBranchOutline16 /> },
    { id: 'archive', label: t('menu.archiveSession'), icon: <IconArchiveOutline20 size={16} /> },
    { id: 'delete', label: t('menu.deleteSession'), icon: <IconTrashOutline16 />, disabled: row.running },
  ]
```

`onSelect`（L451-456）加 `if (id === 'delete') onDelete(node.id)`。

- [ ] **Step 3: 浏览器接线（WorkspaceBrowser.tsx + contract/slots.ts + index.ts）**

`contract/slots.ts`（`archiveSession` 附近）加 `deleteSession: (sessionId: SessionNode['id']) => Promise<void>`（用该文件既有的 SessionNode 类型引用）。

`index.ts` 的 `browserInjected`（`archiveSession` 后）加：

```ts
    deleteSession: async (sessionId) => { await ctx.sessions.delete(sessionId) },
```

`WorkspaceBrowser.tsx`：
- `SessionTreeProps`/`FlatListProps` 加 `onSessionDelete: (sessionId: SessionNode['id']) => void`，两个组件向 `SessionNodeItem` 传 `onDelete={onSessionDelete}`。
- `onSessionArchive`（L969）旁加浏览器自持会话删除状态机（完全复刻工作区删除对话框 L975-1008 的模式）：

```ts
  const [sessionDeleteTarget, setSessionDeleteTarget] = useState<{ sessionId: SessionNode['id'] } | null>(null)
  const [sessionDeleting, setSessionDeleting] = useState(false)
  const [sessionDeleteError, setSessionDeleteError] = useState<string | null>(null)
  const closeSessionDelete = () => {
    if (sessionDeleting) return
    setSessionDeleteTarget(null)
    setSessionDeleteError(null)
  }
  const onSessionDelete = (sessionId: SessionNode['id']) => {
    setSessionDeleteTarget({ sessionId })
    setSessionDeleteError(null)
  }
  const confirmSessionDelete = () => {
    if (sessionDeleting || sessionDeleteTarget === null) return
    setSessionDeleting(true)
    setSessionDeleteError(null)
    deleteSession(sessionDeleteTarget.sessionId).then(() => {
      setSessionDeleting(false)
      setSessionDeleteTarget(null)
    }).catch((reason: unknown) => {
      setSessionDeleting(false)
      const code = reason instanceof SessionDeleteError ? reason.error.code : undefined
      setSessionDeleteError(code === 'session-running'
        ? t('delete.session.error.running')
        : code === 'session-has-descendants' ? t('delete.session.error.descendants')
        : code === 'session-not-found' ? t('delete.session.error.missing')
        : reason instanceof Error ? reason.message : String(reason))
    })
  }
```

（`deleteSession` 来自 props 注入面；`SessionDeleteError` 从 runtime 类型导入——若 runtime 未导出该错误类，改用 `(reason as { code?: string })` 的窄化并注释原因，或让 service 抛普通 Error 而把 `code` 附着为 `Error['code']`。）

- `FlatList`/`SessionTree` 调用处（L1159-1201）传 `onSessionDelete={onSessionDelete}`；行选择投影对删除会话的"切回空白视图"复用既有 `host/session-removed` 处理（manager `kind: 'remove'` + 会话实例 `handleRemoved`；WorkspaceBrowser 若因选中态残留需要显式回落，检查 `list.current` 指向的行是否还在 store——若不在且非 blank，触发 `startSession(workspaceId)` 或等效回退，实现期按现象决定，测试覆盖）。
- 新增 `<Modal>`（复刻工作区删除对话框 L1271-1295，标题 `t('delete.session.title')`、desc `t('delete.session.desc', { name })`、pending/error 展示）：对话框挂载在浏览器根（独立于行），删除成功后行已卸载无碍。

- [ ] **Step 4: 组件测试（workspace-browser.client.spec.tsx）**

读 `workspace-browser.client.spec.tsx` 现有 `archiveSession` 测试（L340-368）的结构后新增：

```tsx
it('delete opens the confirm dialog and commits through the injected deleteSession', async () => {
  const deleteSession = vi.fn(async () => {})
  // 用现有渲染装配（L78 已有 deleteSession 也许已存在——检查签名）渲染 browser，点开菜单 -> 删除会话
  await user.click(screen.getByRole('button', { name: /删除会话/ }))
  expect(screen.getByText(/永久删除/)).toBeTruthy()
  await user.click(screen.getByRole('button', { name: '删除会话' }))
  expect(deleteSession).toHaveBeenCalledWith(sid('gone-s'))
})

it('delete surfaces a session-running rejection inside the dialog', async () => {
  const deleteSession = vi.fn(async () => { throw rejection('session-running') })
  // 打开删除对话框并确认 -> 断言错误文案“正在运行”出现在 role=alert 内
})
```

（若文件已有 `deleteSession` 注入（L78 附近确实有 `deleteSession: vi.fn(...)` 样的行——确认后复用），直接挂断言。）

- [ ] **Step 5: 运行确认通过**

Run: `pnpm run test:gui`
Expected: PASS（含既有用例）。

- [ ] **Step 6: Commit**

```bash
git add packages/client/ui-workspace/src/client/locales.ts packages/client/ui-workspace/src/client/contract/slots.ts packages/client/ui-workspace/src/client/index.ts packages/client/ui-workspace/src/client/rows/Rows.tsx packages/client/ui-workspace/src/client/WorkspaceBrowser.tsx packages/client/ui-workspace/tests/workspace-browser.client.spec.tsx
git commit -m "feat(ui-workspace): Delete session menu action with confirm dialog"
```

---

### Task 8: 文档、Agent Note、检查门与部署验证

**Files:**
- Modify: `packages/host/apiproxy/README.zh.md` + `README.zh.md`（wire 契约：新增 `session.delete` 一段，说明错误码）
- Modify: `packages/workspace/workspace/README.zh.md`（`deleteSession` 记账语义）
- Modify: `packages/session/session-persistence/README.md`/`.zh.md`（`delete` 抽象方法）
- Modify: `packages/client/runtime/README.zh.md`（会话面 `delete`）
- Modify: `packages/client/ui-workspace/README.zh.md`（菜单新增删除项与对话框）
- Create: `.agents/notes/implemented/feature/2026-08-30-gui-session-delete.md`（Agent Note：动机、wire 契约、销毁顺序、错误码、测试路径）
- Modify: `docs/architecture.md`（如涉及会话生命周期章节——只在确实列出会话销毁路径时更新；否则跳过并在 Note 中说明）

- [ ] **Step 1: 更新 README 与 JSDoc**

按各包现有 README 风格补删减段（apiproxy 的 `workspace.archiveSession` 段旁补 `session.delete` 段：语义、三个错误码、`host/session-removed` 收敛）。JSDoc 已在代码内同步。

- [ ] **Step 2: 写 Agent Note**

按 `.agents/notes/` 模板写 `2026-08-30-gui-session-delete.md`（沿用同类 note 结构：背景、设计决策、wire 契约、生命周期顺序、测试与快照策略、后续项：投影缓存剔除验证、subagent 委托是否应豁免 descendants 检查、43120 部署形态）。

- [ ] **Step 3: 运行检查门**

```bash
pnpm run test:gui
pnpm run typecheck
pnpm run lint
```

Expected: 全绿。若 lint 报错，修复后重跑；`test:coverage` 只为 client 源码包编辑器护栏——Task 7 的组件测试须保持新代码 100% 覆盖（可用 `/* v8 ignore -- <reason> */`）。

- [ ] **Step 4: 可见输出变更的快照回放（可选但推荐）**

若 `test:gui` 之外的组装 UI 发生了变化（菜单/对话框属可见输出）：`DSH_SNAPSHOT=replay pnpm run test:web`。若通过则继续；若因预期输出变更失败，仅在确认输出**确实**变更后运行 `DSH_SNAPSHOT=refresh` 重录，并在 PR 说明中列出变更截图依据。

- [ ] **Step 5: 部署验证（重要）**

确认 127.0.0.1:43120 由哪个进程服务：

```powershell
Get-NetTCPConnection -LocalPort 43120 | Select-Object OwningProcess
Get-Process -Id <OwningProcess> | Select-Object ProcessName, Path
```

- 若由源码 dev server（`pnpm run dev:web` 或 `dsh web` 从 `D:\cc-joesph\deepseek-harness` 启动）服务：对该进程做对应重建/重启（开发 watcher 生效则仅验证页面刷新；否则重启该服务），然后浏览器硬刷新 43120，验证会话行菜单出现「删除会话」并可完成一次删除。
- 若由打包 app（`C:\Users\28037\DSH Desktop\...`）服务：该实例不消费源码——记录为部署限制，给出重新打包/等待下版发行路径，并在会话中向用户说明。
- 实测一次删除后抽查磁盘：`$env:DSH_HOME\sessions\...\<会话id>` 目录已消失；`session.list` 不再返回该 id。

- [ ] **Step 6: Commit**

```bash
git add packages/host/apiproxy/README.zh.md packages/host/apiproxy/README.md packages/workspace/workspace/README.zh.md packages/session/session-persistence/README.md packages/session/session-persistence/README.zh.md packages/client/runtime/README.zh.md packages/client/ui-workspace/README.zh.md .agents/notes/implemented/feature/2026-08-30-gui-session-delete.md
git commit -m "docs(session-delete): wire contract, registry/persistence semantics, agent note"
```

---

## Self-Review（执行前已通过）

1. **Spec 覆盖**：第 1-4 节（RPC、校验、执行顺序、一致性）→ Task 1/2/3/5；第 3 节客户端与 UI → Task 6/7；第 4 节错误/测试/文档 → Task 4/7/8；§10 验证项 → Task 8 Step 2/3/5。
2. **占位符**：Task 1/2 的测试引用现有文件助手，但断言行为明确、无"稍后补充"；Task 8 en 文案给出待替换占位并注明是故意占位且给出最终文案。
3. **类型一致性**：`session.delete` 的 wire 名、`SessionsApi.delete`、`ISessions.delete`、`ctx.sessions.delete`、注入 `deleteSession` 全程同名同参；`SessionPersistence.delete(id)` 与两个后端签名一致；`workspaceRegistry.deleteSession(sessionId)` 与 api-proxy 调用一致。