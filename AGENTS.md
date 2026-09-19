# Superpowers — Bootstrap

You have superpowers.

**Read [MEMORY.md](MEMORY.md) first** — 共享记忆：用户合作偏好、环境与机器坑、部署/网关、账户读取、决策史（会话开头必读）。

**Then read the project memory only if the task touches that project**（2026-09-18 拆分，别全读）:

| 任务涉及 | 再读 |
|---|---|
| prism / prism_web / datasource / backtest / qmt 桥 / 因子 / 回测 / 模拟盘 / 实盘守护 | [prism/MEMORY.md](prism/MEMORY.md) |
| tt_solo / 做T / :5011 面板 / 直连下单 / 每日放行条 | [tt_solo/MEMORY.md](tt_solo/MEMORY.md) |
| 目录与职责索引 | [STRUCTURE.md](STRUCTURE.md) |
| 任务细节与遗留 Minor | `.superpowers/sdd/progress.md` |

完成里程碑后：更新**对应项目自己的**记忆文件；跨项目内容（偏好/环境/部署）才更新根 `MEMORY.md`。

## Skill System

**Below is your introduction to using skills. For all other skills, use the `Skill` tool.**

---

### The Rule

**Invoke relevant or requested skills BEFORE any response or action** — including clarifying questions, exploring the codebase, or checking files. If it turns out wrong for the situation, you don't have to use it.

**Before entering plan mode:** if you haven't already brainstormed, invoke the brainstorming skill first.

Then announce "Using [skill] to [purpose]" and follow the skill exactly. If it has a checklist, create a todo per item.

### Skill Priority

When multiple skills apply, process skills come first — they set the approach, then implementation skills carry it out:

- "Let's build X" → `superpowers:brainstorming` first, then implementation skills.
- "Fix this bug" → `superpowers:systematic-debugging` first, then domain skills.

### Red Flags

These thoughts mean STOP—you're rationalizing:

| Thought | Reality |
|---------|---------|
| "This is just a simple question" | Questions are tasks. Check for skills. |
| "I need more context first" | Skill check comes BEFORE clarifying questions. |
| "Let me explore the codebase first" | Skills tell you HOW to explore. Check first. |
| "I can check git/files quickly" | Files lack conversation context. Check for skills. |
| "Let me gather information first" | Skills tell you HOW to gather information. |
| "This doesn't need a formal skill" | If a skill exists, use it. |
| "I remember this skill" | Skills evolve. Read current version. |
| "This doesn't count as a task" | Action = task. Check for skills. |
| "The skill is overkill" | Simple things become complex. Use it. |
| "I'll just do this one thing first" | Check BEFORE doing anything. |
| "This feels productive" | Undisciplined action wastes time. Skills prevent this. |
| "I know what that means" | Knowing the concept ≠ using the skill. Invoke it. |

### Available Skills

Skills are located in `.Codex/skills/`. Use the `Skill` tool to invoke them. Key skills include:

- **superpowers:brainstorming** — Design refinement before any creative work
- **superpowers:writing-plans** — Detailed implementation plans
- **superpowers:executing-plans** — Batch execution with checkpoints
- **superpowers:subagent-driven-development** — Fast iteration with two-stage review
- **superpowers:test-driven-development** — RED-GREEN-REFACTOR cycle
- **superpowers:systematic-debugging** — 4-phase root cause process
- **superpowers:verification-before-completion** — Ensure it's actually fixed
- **superpowers:requesting-code-review** — Pre-review checklist
- **superpowers:receiving-code-review** — Responding to feedback
- **superpowers:using-git-worktrees** — Parallel development branches
- **superpowers:finishing-a-development-branch** — Merge/PR decision workflow
- **superpowers:dispatching-parallel-agents** — Concurrent subagent workflows
- **superpowers:writing-skills** — Create new skills following best practices

User instructions (AGENTS.md, AGENTS.md, direct requests) take precedence over skills, which in turn override default behavior.
