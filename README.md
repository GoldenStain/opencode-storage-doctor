# OpenCode Storage Doctor

[English](#english) | [中文](#中文)

> Formerly `opencode-storage-audit`. Old GitHub links redirect here.

---

## English

A self-contained [OpenCode](https://opencode.ai) skill that audits and safely cleans OpenCode's local storage: the SQLite database, its WAL, the persistent event log, `session_diff` files, and oversized `tool-output`. It also repairs a common V1 to V2 upgrade problem: sessions from non-git directories that `opencode session list` no longer shows.

The audit is **read-only** and never prints conversation content. Cleanup is destructive by design, requires explicit flags, takes a verified backup when asked to (`--backup`), and refuses to run while OpenCode is alive.

### Requirements

- Python 3.8+ (standard library only — no third-party packages)
- Linux: process detection reads `/proc`.
- Windows: process detection runs `tasklist`, and the database file is probed with an exclusive open, which fails while any process holds it. Paths stored by OpenCode as `C:/Users/...` are compared and hashed in their native form.
- macOS and other platforms: no process detector exists. `cleanup` and `session adopt-global` refuse to run there unless you pass `--no-process-check` after stopping OpenCode yourself.

### Install

Copy the directory into your OpenCode skills folder:

```bash
git clone https://github.com/GoldenStain/opencode-storage-doctor.git
cp -r opencode-storage-doctor ~/.config/opencode/skills/
```

Or symlink it:

```bash
ln -s "$PWD/opencode-storage-doctor" ~/.config/opencode/skills/opencode-storage-doctor
```

### Usage

```bash
python3 scripts/opencode_storage.py audit   [--top N] [--oversized-mib N]
python3 scripts/opencode_storage.py cleanup [--clear-events] [--delete-orphan-diffs] [--purge-v1] [--dry-run]
python3 scripts/opencode_storage.py session delete ID [--format auto|v1|v2] [--dry-run] [--backup] [--yes]
python3 scripts/opencode_storage.py session adopt-global [--dir DIR]... [--dry-run] [--backup] [--yes]
python3 scripts/opencode_storage.py session tree ID
```

Shared options: `--data-dir PATH`, `--json`, `--no-color`, and `--no-process-check` on the two destructive commands.

Data directory resolution: `--data-dir` → `$OPENCODE_DATA_DIR` → `$XDG_DATA_HOME/opencode` → `~/.local/share/opencode`. The last rule is the same on Windows, where it means `%USERPROFILE%\.local\share\opencode`; OpenCode has no platform branch. The database file is `opencode.db`, or the file named by `$OPENCODE_DB`, or a single `opencode-<channel>.db` in the data directory.

#### audit (read-only)

Reports:

- Total OpenCode data size, with per-entry sizes under the data directory.
- Physical SQLite size, WAL size, freelist bytes, clearable event bytes, and the combined reclaimable percentage.
- Largest database tables and indexes.
- Largest sessions ranked by message, part, event, and `session_diff` bytes.
- Oversized individual messages, including session IDs and titles.
- Orphan `session_diff` files.
- Event-log size, including events whose session no longer exists.
- V2 databases: V1 residue, meaning V1 rows and `session_diff` files of sessions V2 has already migrated.
- Largest `tool-output` files.

On V2 databases, sessions and messages come from `session_v2` and `session_message`.

#### session tree (read-only)

Draws the hierarchy around one session: the ancestor chain above it, the session itself, and every session below it. It accepts a full id or a unique prefix and works while OpenCode runs.

```bash
python3 scripts/opencode_storage.py session tree ses_1d95e5a37ffe
```

```
▍Session tree (6)
  ses_root  Root session  100 B
  └─ ses_a  Child A  0 B
     └─ ses_b  Target session  0 B  ← this session
        ├─ ses_b2  Child two  0 B
        │  └─ ses_b2x  Grandchild  0 B
        └─ ses_b1  Child one  300 B
```

- Each row shows the session id, its title, and its message bytes. `← this session` marks the session you asked about.
- Ancestors are drawn root first. Only the path to the queried session is shown, not its siblings.
- A session with no parent starts the tree at itself; a session with no children ends there.
- When `parent_id` points at a session the database no longer holds, that row prints `(parent not in the database)` and the walk stops.
- A prefix that matches several sessions lists the candidates and exits non-zero.
- Nothing is modified and no confirmation is asked.
- `--json` returns `sessions`, `ancestors`, `descendants`, `parent_id`, `parent`, `parent_missing`, and a nested `tree` of `{id, current, detail, children}` nodes.

#### cleanup (destructive)

- Refuses to run while any `opencode` process is alive (VACUUM requires exclusive database access) and names the offending PIDs. On Windows it also treats `OpenChamber.exe` and `opencode-desktop.exe` as OpenCode processes.
- Refuses to run when any process holds `opencode.db` open, even one whose name is not on the list. Windows reports this by an exclusive open; Linux reads `/proc/<pid>/fd`. This is what catches a renamed binary, a `bun`/`node` dev launch, or another tool holding the database.
- Refuses to run when it cannot check at all (macOS, a failed `tasklist`, or an unreadable handle table), because "could not check" is not "stopped". Pass `--no-process-check` after stopping OpenCode by hand to bypass only that refusal. It never bypasses a process or an open file that was actually found.
- With `--backup`: creates and integrity-checks a timestamped backup (`opencode.db.pre-cleanup-<timestamp>.bak`) before touching anything, and zips removed diff files. Without it, no backup is written.
- `--clear-events`: delete `event` rows, plus `event_sequence` on V1 databases only (V2 numbers new session messages from `event_sequence`, so it is kept there).
- `--delete-orphan-diffs`: archive, then remove `session_diff` files with no corresponding live session.
- `--purge-v1` (V2 databases only): delete the V1 `session`, `message`, `part`, `todo`, and `session_share` rows, and the `session_diff` files, of sessions that V2 has already migrated. The tables are kept empty. V1 sessions missing from V2 are kept. Refuses when V2's V1->V2 migration is not completed. This removes the only lossless copy of V1 history; pass `--backup` if you may need it again.
- Then: pre-VACUUM `quick_check`, WAL checkpoint, `VACUUM`, post-`integrity_check`.

Always dry-run first (read-only, no flags needed beyond the ones you plan to use), then exit every OpenCode process and re-run without `--dry-run`.

```bash
# 1. see exactly what would be removed and the estimated reclaim
python3 scripts/opencode_storage.py cleanup --clear-events --delete-orphan-diffs --dry-run

# 2. after exiting all OpenCode processes
python3 scripts/opencode_storage.py cleanup --clear-events --delete-orphan-diffs
```

#### session delete (destructive)

Deletes one session and its child sessions, chosen by full id or unique prefix. Asks for confirmation unless `--yes` is given; `--dry-run` only prints the plan. Run `session tree ID` first to see which sessions go with it.

- `--format v2`: delete the V2 session through `opencode session delete` (needs the OpenCode service, default data directory only).
- `--format v1`: delete the session's V1 rows and `storage/session_diff` file. Works while OpenCode runs.
- `--format auto` (default): run both steps that apply, V2 first.
- `--backup`: write a verified database backup and zip the removed diff file first.
- Refuses to delete the session that runs it, and warns when a session exists only as V1 data.

#### session adopt-global (repair)

After upgrading from V1, sessions you started outside a git repository vanish from `opencode session list`. V1 stored them all in one project named `global`; V2 gives each directory its own project but leaves the old sessions in `global`. They are still in the database.

This command moves them, one directory at a time, into the project V2 uses for that directory, the same way V2 already handles git repositories. Session order and contents do not change.

```bash
# 1. see which directories and sessions would move (works while OpenCode runs)
python3 scripts/opencode_storage.py session adopt-global --dry-run

# 2. stop OpenCode (`opencode service stop`, and any app running `opencode serve`), then
python3 scripts/opencode_storage.py session adopt-global --backup
```

- Skips directories that no longer exist, symlinked paths, and directories inside a git or hg tree (OpenCode adopts those itself).
- The project id is the SHA-1 of the native path, so `C:/Users/you/proj` is hashed as `C:\Users\you\proj`, the form OpenCode wrote. Both separator forms are accepted when matching existing projects.
- `--dir DIR` (repeatable) limits the move to some directories.
- Refuses while OpenCode runs, on non-V2 databases, and when the database's own directory projects do not match the known id rule (an unsupported OpenCode version). If no directory project exists yet, open OpenCode once in any non-git directory and retry.
- The audit reports how many sessions are affected.

### Privacy

The audit never reads or prints message, part, event, diff, or tool-output contents — only metadata and byte lengths.

### Notes

- Logical deletion is not physical shrink: SQLite `DELETE` only returns pages to the freelist; only `VACUUM` gives that space back to the filesystem. Reclaimable space = `freelist + event rows + V1 residue rows`.
- Windows opens a database file exclusively enough that a running OpenCode blocks `VACUUM`. Stop it first; the preflight checks do that for you. If a diff file cannot be deleted because another process holds it, the command names the file, keeps the archive, and reports how many files were removed.
- The checks run just before `VACUUM`, not during it. A process that starts in that gap makes SQLite refuse with `database is locked`; it does not corrupt the file. Re-run after stopping that process.
- Keep the backup for a day or two; delete it once `integrity_check` is `ok`, OpenCode starts normally, retained sessions open, and you no longer need the deleted history.

### License

MIT — see [LICENSE](LICENSE).

---

## 中文

一个自包含的 [OpenCode](https://opencode.ai) skill：审计并安全清理 OpenCode 的本地存储——SQLite 数据库、WAL、持久化事件日志、`session_diff` 文件，以及体积异常的 `tool-output`。它还能修复 V1 升级到 V2 后的一个常见问题：在非 git 目录中创建的会话，在 `opencode session list` 中不再显示。

审计**只读**，且绝不打印对话内容。清理按设计是破坏性操作：必须显式指定参数、按需（`--backup`）做带校验的备份、并在 OpenCode 进程存活时拒绝运行。

### 环境要求

- Python 3.8+（仅标准库，无第三方依赖）
- Linux：进程检测读取 `/proc`。
- Windows：进程检测调用 `tasklist`，另外对数据库文件做独占打开探测：只要任何进程持有该文件，探测就会失败。OpenCode 存成 `C:/Users/...` 的路径，按原生形式比较和哈希。
- macOS 及其他平台：没有进程检测后端。`cleanup` 与 `session adopt-global` 会拒绝运行，除非你先手动退出 OpenCode 再加 `--no-process-check`。

### 安装

把目录复制进 OpenCode 的 skill 目录：

```bash
git clone https://github.com/GoldenStain/opencode-storage-doctor.git
cp -r opencode-storage-doctor ~/.config/opencode/skills/
```

或建立软链接：

```bash
ln -s "$PWD/opencode-storage-doctor" ~/.config/opencode/skills/opencode-storage-doctor
```

### 用法

```bash
python3 scripts/opencode_storage.py audit   [--top N] [--oversized-mib N]
python3 scripts/opencode_storage.py cleanup [--clear-events] [--delete-orphan-diffs] [--purge-v1] [--dry-run]
python3 scripts/opencode_storage.py session delete ID [--format auto|v1|v2] [--dry-run] [--backup] [--yes]
python3 scripts/opencode_storage.py session adopt-global [--dir DIR]... [--dry-run] [--backup] [--yes]
python3 scripts/opencode_storage.py session tree ID
```

通用选项：`--data-dir PATH`、`--json`、`--no-color`；两个破坏性命令另有 `--no-process-check`。

数据目录解析顺序：`--data-dir` → `$OPENCODE_DATA_DIR` → `$XDG_DATA_HOME/opencode` → `~/.local/share/opencode`。最后一条在 Windows 上同样是 `%USERPROFILE%\.local\share\opencode`，OpenCode 没有平台分支。数据库文件是 `opencode.db`，或 `$OPENCODE_DB` 指定的文件，或数据目录下唯一的 `opencode-<channel>.db`。

#### audit（只读）

输出内容：

- OpenCode 数据总量，以及数据目录下各条目的体积。
- SQLite 物理大小、WAL 大小、freelist 字节数、可清理的事件字节数，以及综合可回收百分比。
- 最大的数据库表与索引。
- 按 message / part / event / `session_diff` 字节数排序的最大会话。
- 体积异常的单条消息（含会话 ID 与标题）。
- 孤儿 `session_diff` 文件。
- 事件日志大小，包含所属会话已不存在的事件。
- V2 数据库：V1 残留，即 V2 已迁移会话的 V1 行与 `session_diff` 文件。
- 最大的 `tool-output` 文件。

在 V2 数据库上，会话与消息统计读取 `session_v2` 与 `session_message`。

#### session tree（只读）

以树状图显示一个会话周围的关系：它上方的祖先链、它自己，以及它下方的所有会话。接受完整 id 或唯一前缀，OpenCode 运行时也可执行。

```bash
python3 scripts/opencode_storage.py session tree ses_1d95e5a37ffe
```

```
▍Session tree (6)
  ses_root  Root session  100 B
  └─ ses_a  Child A  0 B
     └─ ses_b  Target session  0 B  ← this session
        ├─ ses_b2  Child two  0 B
        │  └─ ses_b2x  Grandchild  0 B
        └─ ses_b1  Child one  300 B
```

- 每行显示会话 id、标题和消息字节数。`← this session` 标记你查询的会话。
- 祖先从根开始绘制。只显示通往被查询会话的那条路径，不显示它的兄弟会话。
- 没有父会话的会话从它自己开始；没有子会话的会话到此结束。
- 如果 `parent_id` 指向的会话已不在数据库中，该行显示 `(parent not in the database)`，遍历就此停止。
- 前缀匹配到多个会话时，列出候选并返回非零退出码。
- 不修改任何数据，也不会要求确认。
- `--json` 返回 `sessions`、`ancestors`、`descendants`、`parent_id`、`parent`、`parent_missing`，以及嵌套的 `tree`（每个节点为 `{id, current, detail, children}`）。

#### cleanup（破坏性）

- OpenCode 进程存活时**拒绝运行**（VACUUM 需要独占数据库访问），并列出占用进程的 PID。在 Windows 上，`OpenChamber.exe` 与 `opencode-desktop.exe` 同样算作 OpenCode 进程。
- 任何进程持有 `opencode.db` 时也会**拒绝运行**，即使它的名字不在名单里。Windows 用独占打开探测，Linux 读 `/proc/<pid>/fd`。这条能抓住改名后的二进制、`bun`/`node` 启动的开发版本，以及其他占用数据库的工具。
- 完全无法检查时（macOS、`tasklist` 失败、句柄表不可读）也拒绝运行，因为“没法检查”不等于“已经停止”。手动退出 OpenCode 后可加 `--no-process-check` 跳过这道拒绝；它**不会**绕过真实检测到的进程或已被占用的文件。
- 加 `--backup` 时：改动前先创建带时间戳的备份（`opencode.db.pre-cleanup-<时间戳>.bak`）并做完整性校验，被删的 diff 文件打包成 zip。不加则不写任何备份。
- `--clear-events`：删除 `event` 行；仅在 V1 数据库上同时删除 `event_sequence`（V2 用 `event_sequence` 给新消息编号，所以保留）。
- `--delete-orphan-diffs`：先归档，再删除没有对应存活会话的 `session_diff` 文件。
- `--purge-v1`（仅限 V2 数据库）：删除 V2 已迁移会话在 V1 `session`、`message`、`part`、`todo`、`session_share` 表中的行，以及它们的 `session_diff` 文件。表本身保留为空表。V2 中没有的 V1 会话会保留。V2 的 V1→V2 迁移未完成时拒绝执行。这会删掉 V1 历史唯一的无损副本；如果以后可能还需要，请加 `--backup`。
- 随后依次执行：VACUUM 前 `quick_check`、WAL checkpoint、`VACUUM`、VACUUM 后 `integrity_check`。

务必先 dry-run（只读，不会修改任何东西），确认后再退出所有 OpenCode 进程、去掉 `--dry-run` 正式执行。

```bash
# 1. 先看会删掉什么、预计回收多少空间
python3 scripts/opencode_storage.py cleanup --clear-events --delete-orphan-diffs --dry-run

# 2. 退出所有 OpenCode 进程后
python3 scripts/opencode_storage.py cleanup --clear-events --delete-orphan-diffs
```

#### session delete（破坏性）

按完整 id 或唯一前缀删除一个会话及其子会话。不加 `--yes` 时会要求确认；`--dry-run` 只打印计划。可先用 `session tree ID` 查看会一并删除哪些会话。

- `--format v2`：通过 `opencode session delete` 删除 V2 会话（需要 OpenCode 服务在运行，且只支持默认数据目录）。
- `--format v1`：删除该会话的 V1 行与 `storage/session_diff` 文件。OpenCode 运行时也可执行。
- `--format auto`（默认）：执行所有适用的步骤，先 V2 后 V1。
- `--backup`：先写一份带校验的数据库备份，并把被删的 diff 文件打包成 zip。
- 拒绝删除正在运行本命令的会话；会话只存在于 V1 数据中时会给出警告。

#### session adopt-global（修复）

从 V1 升级后，在 git 仓库之外创建的会话会从 `opencode session list` 中消失。原因是 V1 把这些会话都放在一个名为 `global` 的项目里；V2 给每个目录单独建项目，但旧会话仍留在 `global` 中。数据本身仍在数据库里。

这个命令按目录把它们移到 V2 为该目录使用的项目中，做法与 V2 处理 git 仓库时相同。会话的顺序和内容都不变。

```bash
# 1. 查看哪些目录和会话会被移动（OpenCode 运行时也可执行）
python3 scripts/opencode_storage.py session adopt-global --dry-run

# 2. 停止 OpenCode（`opencode service stop`，以及所有自己运行 `opencode serve` 的应用），然后执行
python3 scripts/opencode_storage.py session adopt-global --backup
```

- 跳过已不存在的目录、带符号链接的路径，以及位于 git 或 hg 仓库内的目录（OpenCode 会自行处理这些）。
- 项目 id 是原生路径的 SHA-1，所以 `C:/Users/you/proj` 按 `C:\Users\you\proj` 计算，与 OpenCode 写入的形式一致。匹配已有项目时两种分隔符写法都接受。
- `--dir DIR`（可重复）只移动指定目录。
- 以下情况拒绝执行：OpenCode 正在运行；不是 V2 数据库；数据库中已有的目录项目不符合已知的 id 规则（说明 OpenCode 版本不受支持）。如果还没有任何目录项目，请先在任意非 git 目录中打开一次 OpenCode，再重试。
- audit 报告会显示受影响的会话数量。

### 隐私

审计过程绝不读取或打印 message、part、event、diff、tool-output 的内容——只使用元数据与字节长度。

### 说明

- 逻辑删除不等于物理缩小：SQLite 的 `DELETE` 只把页还给 freelist，只有 `VACUUM` 才能把空间真正还给文件系统。可回收空间 = `freelist + 事件行 + V1 残留行`。
- Windows 会独占被打开的文件，所以 OpenCode 运行时 `VACUUM` 必然失败；预检已经替你拦下。若有 diff 文件被其他进程占用而删不掉，命令会指出该文件、保留 zip 归档，并报告实际删掉了多少个。
- 这两道检查在 `VACUUM` 之前执行，不是在执行中。若正好在这个间隙里启动了进程，SQLite 会以 `database is locked` 拒绝，而不会损坏文件；停掉该进程后重跑即可。
- 备份建议保留一两天；待 `integrity_check` 为 `ok`、OpenCode 正常启动、保留的会话都能打开、且不再需要被删历史后，再删除备份。

### 许可

MIT，见 [LICENSE](LICENSE)。
